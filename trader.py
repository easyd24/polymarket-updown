#!/usr/bin/env python3
"""Polymarket CLOB trader — places real orders via the CLOB API.

Reuses the deposit wallet flow (POLY_1271) from the weather bot.
For the UpDown bot, we use LIMIT orders (maker side) to avoid the 7% taker fee.
"""

import os
import json
import time
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv(Path(__file__).parent / '.env')

from py_clob_client_v2 import ClobClient, SignatureTypeV2, ApiCreds

# ── Wallet Configuration ──────────────────────────────────────────────────────
DEPOSIT_WALLET = os.environ.get('DEPOSIT_WALLET', '')
EOA_WALLET = os.environ.get('EOA_WALLET', '')

# ── Client Singleton ──────────────────────────────────────────────────────────
_client = None


def get_client():
    """Get or create the Polymarket CLOB client."""
    global _client
    if _client is not None:
        return _client
    
    key = os.getenv('POLYMARKET_PRIVATE_KEY')
    api_key = os.getenv('POLYMARKET_API_KEY')
    api_secret = os.getenv('POLYMARKET_API_SECRET')
    api_passphrase = os.getenv('POLYMARKET_API_PASSPHRASE')
    
    if not key:
        raise ValueError("POLYMARKET_PRIVATE_KEY not set in .env")
    
    if api_key and api_secret and api_passphrase:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase
        )
        _client = ClobClient(
            'https://clob.polymarket.com',
            key=key,
            chain_id=137,
            creds=creds,
            signature_type=SignatureTypeV2.POLY_1271,
            funder=DEPOSIT_WALLET,
        )
    else:
        temp_client = ClobClient('https://clob.polymarket.com', key=key, chain_id=137)
        creds = temp_client.derive_api_key()
        _client = ClobClient(
            'https://clob.polymarket.com',
            key=key,
            chain_id=137,
            creds=creds,
            signature_type=SignatureTypeV2.POLY_1271,
            funder=DEPOSIT_WALLET,
        )
    
    return _client


def get_usdc_balance():
    """Get USDC balance on Polymarket."""
    client = get_client()
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams
        params = BalanceAllowanceParams(asset_type='COLLATERAL')
        result = client.get_balance_allowance(params)
        if isinstance(result, dict) and 'balance' in result:
            return float(result['balance']) / 1e6
        return -1
    except Exception as e:
        print(f"[trader] Balance check error: {e}")
        return -1


def get_live_price(token_id, slug=None, direction=None):
    """Get the live midpoint price from the CLOB orderbook.
    
    Returns (mid_price, best_bid, best_ask, spread_pct) or (None, ...) on error.
    Falls back to Gamma API when orderbook spread > 50% (thin markets).
    Pass slug + direction for accurate Gamma fallback (Up=index 0, Down=index 1).
    """
    import httpx
    try:
        resp = httpx.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=10,
        )
        data = resp.json()
        
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        
        if best_bid and best_ask:
            mid = (best_bid + best_ask) / 2
            spread_pct = (best_ask - best_bid) / mid * 100 if mid > 0 else 999
            
            # Thin market fallback: spread > 50% means orderbook is unreliable
            if spread_pct > 50:
                gamma_mid, gamma_bid, gamma_ask = _gamma_fallback_price(token_id, slug=slug, direction=direction)
                if gamma_mid is not None:
                    return gamma_mid, gamma_bid, gamma_ask, 0.0
            
            return mid, best_bid, best_ask, spread_pct
        
        if best_bid:
            return best_bid, best_bid, None, 999
        if best_ask:
            return best_ask, None, best_ask, 999
        
        # No bids or asks — try Gamma fallback
        gamma_mid, gamma_bid, gamma_ask = _gamma_fallback_price(token_id, slug=slug, direction=direction)
        if gamma_mid is not None:
            return gamma_mid, gamma_bid, gamma_ask, 0.0
        
        return None, None, None, None
    except Exception as e:
        print(f"[trader] Price fetch error: {e}")
        return None, None, None, None


def _gamma_fallback_price(token_id, slug=None, direction=None):
    """Fallback: get price from Gamma API when CLOB orderbook is thin.
    
    Uses slug-based lookup (more reliable than token_id matching).
    Direction determines which outcome index to use: Up=0, Down=1.
    
    Returns (mid, bid, ask) or (None, None, None).
    """
    import httpx
    try:
        # Prefer slug-based lookup — token IDs may be truncated
        params = {"limit": 1}
        if slug:
            params["slug"] = slug
        else:
            params["clob_token_ids"] = token_id
        
        resp = httpx.get(
            "https://gamma-api.polymarket.com/markets",
            params=params,
            timeout=10,
        )
        markets = resp.json()
        if not markets:
            return None, None, None
        
        m = markets[0]
        outcome_prices = m.get("outcomePrices")
        if outcome_prices:
            # outcomePrices is a JSON string list like '["0.63", "0.37"]'
            if isinstance(outcome_prices, str):
                import json
                outcome_prices = json.loads(outcome_prices)
            
            # Determine outcome index: Up=0, Down=1
            idx = 0 if (direction or "Up") == "Up" else 1
            
            price = float(outcome_prices[idx])
            
            # bestBid/bestAsk from Gamma are for the full market, not per-outcome
            # Use outcomePrices directly as the most reliable source
            bid = float(m.get("bestBid", price)) if idx == 0 else float(m.get("bestAsk", price))
            ask = float(m.get("bestAsk", price)) if idx == 0 else float(m.get("bestBid", price))
            return price, bid, ask
        
        return None, None, None
    except Exception as e:
        print(f"[trader] Gamma fallback error: {e}")
        return None, None, None


def place_order(token_id, side, price, amount_usd, max_slippage_pct=3.0, 
                max_spread_pct=15.0, paper_trade=True):
    """Place a limit order on the CLOB (maker side to avoid 7% taker fee).
    
    For Up/Down markets:
    - side: "BUY" to buy Up or Down tokens
    - price: price per share (0.01-0.99)
    - amount_usd: total USD to spend
    
    Returns dict with order details.
    """
    if paper_trade:
        shares = int(amount_usd / price) if price > 0 else 0
        print(f"[trader] PAPER TRADE: {side} {shares} shares at {price:.2f}¢ = ${amount_usd:.2f}")
        return {
            "order_id": f"PAPER-{int(time.time())}",
            "status": "paper",
            "side": side,
            "price": price,
            "amount_usd": amount_usd,
            "shares": shares,
            "token_id": token_id,
        }
    
    # Real trading
    client = get_client()
    
    # Check spread first
    mid, best_bid, best_ask, spread_pct = get_live_price(token_id)
    if spread_pct > max_spread_pct:
        print(f"[trader] Spread too wide ({spread_pct:.1f}%), skipping order")
        return None
    
    # Calculate shares
    shares = int(amount_usd / price) if price > 0 else 0
    if shares < 1:
        print(f"[trader] Order too small: ${amount_usd:.2f} at {price:.2f} = {shares} shares")
        return None
    
    # Place as LIMIT order (maker side — 0% fee)
    from py_clob_client_v2.clob_types import OrderArgsV2
    
    try:
        order_args = OrderArgsV2(
            token_id=token_id,
            price=price,
            size=shares,
            side=side,
        )
        resp = client.create_and_post_order(order_args)
        print(f"[trader] Order placed: {side} {shares} @ {price:.2f} — {resp}")
        return {
            "order_id": resp,
            "status": "submitted",
            "side": side,
            "price": price,
            "amount_usd": amount_usd,
            "shares": shares,
            "token_id": token_id,
        }
    except Exception as e:
        print(f"[trader] Order error: {e}")
        return None


def cancel_order(order_id):
    """Cancel an open order."""
    if order_id and order_id.startswith("PAPER"):
        print(f"[trader] PAPER CANCEL: {order_id}")
        return True
    
    client = get_client()
    try:
        resp = client.cancel_order(order_id)
        print(f"[trader] Cancelled {order_id}: {resp}")
        return True
    except Exception as e:
        print(f"[trader] Cancel error: {e}")
        return False


def get_open_orders():
    """Get all open orders."""
    client = get_client()
    try:
        return client.get_orders()
    except Exception as e:
        print(f"[trader] Get orders error: {e}")
        return []


if __name__ == "__main__":
    print("Testing trader module...")
    balance = get_usdc_balance()
    print(f"  USDC Balance: ${balance:.2f}" if balance >= 0 else "  Balance: Error")
    
    # Test price fetch on a known token
    print("\nTesting price fetch...")
    test_token = "69221646425312202965647318240892379045673918421796898396388002846339028162"  # BTC up/down
    mid, bid, ask, spread = get_live_price(test_token)
    if mid:
        print(f"  Mid: {mid:.4f}, Bid: {bid:.4f}, Ask: {ask:.4f}, Spread: {spread:.1f}%")