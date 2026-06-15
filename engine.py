#!/usr/bin/env python3
"""Main orchestration engine — discovers markets, detects edges, places trades."""

import json
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config
from models import Market, EdgeResult, TradeResult
import market_discovery
import price_feed
import edge_detector
import trader

# ── State ──────────────────────────────────────────────────────────────────────
_stats = {
    "opportunities_found": 0,
    "trades_placed": 0,
    "trades_won": 0,
    "trades_lost": 0,
    "total_pnl": 0.0,
    "start_time": datetime.now(timezone.utc).isoformat(),
}
_open_positions = {}  # slug → dict
_trade_history = []   # list of dicts
_seen_opportunities = set()  # set of slug+direction keys
_last_scan_time = 0
_daily_pnl = 0.0
_daily_trades = 0
_daily_reset = datetime.now(timezone.utc).date()
_auto_trade_enabled = config.AUTO_TRADE_ENABLED
_paper_trade = config.PAPER_TRADE
_alert_callback = None  # Set by telegram_bot to avoid circular import
_scan_paused = False

# ── Data Persistence ───────────────────────────────────────────────────────────
DATA_DIR = config.DATA_DIR
DATA_DIR.mkdir(exist_ok=True)


def _load_trade_history():
    """Load trade history from JSON file."""
    global _trade_history
    path = config.TRADE_HISTORY_FILE
    if path.exists():
        try:
            with open(path) as f:
                _trade_history = json.load(f)
        except:
            _trade_history = []


def _save_trade_history():
    """Save trade history to JSON file."""
    path = config.TRADE_HISTORY_FILE
    with open(path, 'w') as f:
        json.dump(_trade_history, f, indent=2, default=str)


def _load_positions():
    """Load open positions from JSON file."""
    global _open_positions
    path = config.POSITIONS_FILE
    if path.exists():
        try:
            with open(path) as f:
                _open_positions = json.load(f)
        except:
            _open_positions = {}


def _save_positions():
    """Save open positions to JSON file."""
    path = config.POSITIONS_FILE
    with open(path, 'w') as f:
        json.dump(_open_positions, f, indent=2, default=str)


def get_open_positions() -> list[dict]:
    """Get list of open positions."""
    return list(_open_positions.values())


def get_recent_trades(limit: int = 10) -> list[dict]:
    """Get recent trades."""
    return _trade_history[-limit:]


# ── Risk Checks ────────────────────────────────────────────────────────────────

def _check_risk_limits() -> bool:
    """Check if we're within risk limits. Returns True if OK to trade."""
    global _daily_pnl, _daily_trades, _daily_reset
    
    now = datetime.now(timezone.utc)
    if now.date() != _daily_reset:
        # Reset daily counters
        _daily_pnl = 0.0
        _daily_trades = 0
        _daily_reset = now.date()
    
    # Daily loss limit
    balance = trader.get_usdc_balance()
    if balance > 0 and _daily_pnl < -(balance * config.MAX_DAILY_LOSS_PCT / 100):
        print(f"[engine] Daily loss limit reached: {_daily_pnl:.2f} < -{balance * config.MAX_DAILY_LOSS_PCT / 100:.2f}")
        return False
    
    # Hourly trade limit
    recent_trades = sum(1 for t in _trade_history 
                       if t.get("timestamp") and 
                       (now - datetime.fromisoformat(t["timestamp"])).total_seconds() < 3600)
    if recent_trades >= config.MAX_TRADES_PER_HOUR:
        print(f"[engine] Hourly trade limit reached: {recent_trades}/{config.MAX_TRADES_PER_HOUR}")
        return False
    
    return True


def _calculate_position_size(edge: EdgeResult, balance: float) -> float:
    """Calculate position size using Kelly criterion with safety margin."""
    if balance <= 0:
        return config.MIN_ORDER_SIZE
    
    p = edge.estimated_true_prob
    if edge.direction == "Down":
        p = 1 - p
    
    # Kelly fraction: f = (bp - q) / b where b = 1/price - 1
    price = edge.market.up_price if edge.direction == "Up" else edge.market.down_price
    if price <= 0 or price >= 1:
        return config.MIN_ORDER_SIZE
    
    b = (1 / price) - 1  # Odds ratio
    q = 1 - p
    kelly = (b * p - q) / b if b > 0 else 0
    kelly = max(0, kelly)
    
    # Half-Kelly for safety
    half_kelly = kelly / 2
    
    # Position size
    series_config = config.SERIES.get(edge.market.series_slug, {})
    max_pct = series_config.get("max_stake_pct", config.MAX_POSITION_PCT) / 100
    max_by_kelly = balance * half_kelly
    max_by_pct = balance * max_pct
    
    position = min(max_by_kelly, max_by_pct, balance * 0.10)  # Never more than 10% of bankroll
    position = max(config.MIN_ORDER_SIZE, position)
    
    return round(position, 2)


# ── Main Scan Loop ─────────────────────────────────────────────────────────────

def scan_loop():
    """Main scanning loop — runs forever."""
    print("[engine] Starting scan loop...")
    
    # Load state
    _load_trade_history()
    _load_positions()
    
    # Start price feed
    price_feed.start_price_feed()
    time.sleep(2)  # Let WS connect
    
    last_market_refresh = 0
    markets = []
    
    while True:
        try:
            # ── Check pause state ────────────────────────────────────────────
            if _scan_paused:
                time.sleep(5)
                continue
            
            now = time.time()
            
            # ── Refresh market list periodically ────────────────────────────
            if now - last_market_refresh > config.MARKET_SCAN_INTERVAL:
                print("[engine] Discovering markets...")
                markets = market_discovery.discover_markets(force_refresh=True)
                last_market_refresh = now
                print(f"[engine] Found {len(markets)} active markets")
            
            # ── Scan each market for edge ────────────────────────────────────
            for market in markets:
                series_config = config.SERIES.get(market.series_slug, {})
                if not series_config.get("enabled", False):
                    continue
                
                # Check if market is still within trading window
                if market.time_remaining_pct < 0.05:
                    continue
                
                # Check if within the trade window portion
                tf = market.timeframe
                max_pct = config.TRADE_WINDOW_PCT.get(tf, 0.5)
                if market.time_remaining_pct < (1 - max_pct):
                    continue  # Too late in the window
                
                # ── Detect edge ─────────────────────────────────────────────
                edge = edge_detector.calculate_edge(market)
                if not edge:
                    continue
                if not edge.is_tradeable:
                    print(f"[engine] Edge found but not tradeable: {market.coin} {market.timeframe} "
                          f"{edge.direction} edge={edge.edge_pp:+.1f}pp conf={edge.confidence:.0%}")
                    continue
                
                # ── Deduplicate opportunities ────────────────────────────────
                opp_key = f"{market.slug}_{edge.direction}"
                if opp_key in _seen_opportunities:
                    continue
                
                _seen_opportunities.add(opp_key)
                config_obj = config  # avoid shadowing
                config_obj_obj = _stats_increment("opportunities_found")
                
                print(f"[engine] 🎯 Edge found: {market.coin.upper()} {market.timeframe} "
                      f"→ Buy {edge.direction} @ {getattr(market, f'{edge.direction.lower()}_price'):.2f} "
                      f"(edge: {edge.edge_pp:+.1f}pp, conf: {edge.confidence:.0%})")
                
                # ── Send alert ───────────────────────────────────────────────
                _send_alert(edge)
                
                # ── Auto-trade if enabled ───────────────────────────────────
                if _auto_trade_enabled and edge.is_tradeable and _check_risk_limits():
                    _place_trade(edge)
            
            # ── Prune seen opportunities ──────────────────────────────────────
            # Remove keys for markets that have expired
            expired_keys = [k for k in _seen_opportunities 
                           if any(m.slug in k and m.time_remaining_pct < 0.02 
                                 for m in markets)]
            for k in expired_keys:
                _seen_opportunities.discard(k)
            
            # ── Check open positions for resolution ─────────────────────────
            _check_positions(markets)
            
            # ── Sleep based on most aggressive enabled series ───────────────
            min_interval = min(s.get("scan_interval", 60) 
                             for s in config.SERIES.values() 
                             if s.get("enabled", False))
            time.sleep(max(5, min_interval))
            
        except KeyboardInterrupt:
            print("[engine] Shutting down...")
            break
        except Exception as e:
            print(f"[engine] Error in scan loop: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)
    
    price_feed.stop_price_feed()


def _send_alert(edge: EdgeResult):
    """Send an alert via the registered callback (avoids circular import)."""
    if _alert_callback:
        try:
            _alert_callback(edge)
        except Exception as e:
            print(f"[engine] Alert callback error: {e}")


def _stats_increment(key: str):
    """Increment a stats counter."""
    global _stats
    _stats[key] = _stats.get(key, 0) + 1


def _place_trade(edge: EdgeResult):
    """Place a trade based on edge detection result."""
    market = edge.market
    
    # Calculate position size
    if _paper_trade:
        # Paper mode: use a mock balance so API auth failures don't block trades
        balance = 100.0  # $100 mock balance for paper trading
    else:
        balance = trader.get_usdc_balance()
        if balance <= 0:
            print("[engine] Cannot trade — balance unavailable")
            return
    
    amount = _calculate_position_size(edge, balance)
    
    # Get the right token ID
    if edge.direction == "Up":
        token_id = market.up_token_id
        price = market.up_price
    else:
        token_id = market.down_token_id
        price = market.down_price
    
    if not token_id or price <= 0:
        print(f"[engine] Cannot trade — invalid token or price for {market.slug}")
        return
    
    # Place the order
    is_paper = _paper_trade
    result = trader.place_order(
        token_id=token_id,
        side="BUY",
        price=price,
        amount_usd=amount,
        paper_trade=is_paper,
    )
    
    if result:
        # Record trade
        trade_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_slug": market.slug,
            "coin": market.coin,
            "timeframe": market.timeframe,
            "direction": edge.direction,
            "price": price,
            "amount_usd": amount,
            "shares": result.get("shares", int(amount / price)),
            "order_id": result.get("order_id", ""),
            "fill_status": result.get("status", "paper" if is_paper else "pending"),
            "paper_trade": is_paper,
            "edge_pp": edge.edge_pp,
            "confidence": edge.confidence,
            "estimated_true_prob": edge.estimated_true_prob,
        }
        _trade_history.append(trade_record)
        _save_trade_history()
        
        # Track position
        _open_positions[market.slug] = {
            "coin": market.coin,
            "timeframe": market.timeframe,
            "direction": edge.direction,
            "price": price,
            "amount_usd": amount,
            "shares": result.get("shares", int(amount / price) if price > 0 else 0),
            "token_id": market.up_token_id if edge.direction == "Up" else market.down_token_id,
            "end_date": market.end_date.isoformat() if market.end_date else None,
            "order_id": result.get("order_id", ""),
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_positions()
        
        _stats_increment("trades_placed")
        print(f"[engine] ✅ Trade placed: {market.coin.upper()} {market.timeframe} "
              f"{edge.direction} @ {price:.2f} for ${amount:.2f}")
        
        # Send trade confirmation to Telegram
        if _alert_callback:
            try:
                from telegram_bot import send_trade_confirmation
                send_trade_confirmation(edge, amount, price, is_paper, result)
            except Exception as e:
                print(f"[engine] Trade confirmation error: {e}")


def get_live_pnl():
    """Calculate unrealized PnL for all open positions using live orderbook prices.
    
    Returns list of dicts with position info + live PnL.
    """
    from trader import get_live_price
    results = []
    
    for slug, pos in _open_positions.items():
        entry_price = pos.get("price", 0)
        amount_usd = pos.get("amount_usd", 0)
        shares = pos.get("shares", 0)
        token_id = pos.get("token_id", "")
        direction = pos.get("direction", "Up")
        
        # Calculate live time remaining
        time_remaining_str = "?"
        end_date_iso = pos.get("end_date")
        likely_resolved = False
        if end_date_iso:
            try:
                end_dt = datetime.fromisoformat(end_date_iso)
                remaining = (end_dt - datetime.now(timezone.utc)).total_seconds()
                if remaining > 0:
                    h = int(remaining // 3600)
                    m = int((remaining % 3600) // 60)
                    s = int(remaining % 60)
                    if h > 0:
                        time_remaining_str = f"{h}h {m}m"
                    elif m > 0:
                        time_remaining_str = f"{m}m {s}s"
                    else:
                        time_remaining_str = f"{s}s"
                else:
                    time_remaining_str = "resolved"
                    likely_resolved = True
            except (ValueError, TypeError):
                pass
        # Fallback to legacy static field
        if time_remaining_str == "?" and "time_remaining" in pos:
            time_remaining_str = pos["time_remaining"]
        
        current_price = None
        unrealized_pnl = None
        pnl_pct = None
        
        if token_id:
            mid, bid, ask, spread = get_live_price(token_id, slug=slug, direction=direction)
            if mid is not None:
                # Detect likely resolution: price at 95¢+ with no bids = resolved market
                if mid >= 0.95 and bid is None:
                    likely_resolved = True
                current_price = mid
                # PnL = (current_price - entry_price) * shares
                # For paper: shares = amount_usd / entry_price
                if shares <= 0 and entry_price > 0:
                    shares = int(amount_usd / entry_price)
                unrealized_pnl = (current_price - entry_price) * shares
                if amount_usd > 0:
                    pnl_pct = (unrealized_pnl / amount_usd) * 100
        
        results.append({
            "slug": slug,
            "coin": pos.get("coin", "?"),
            "timeframe": pos.get("timeframe", "?"),
            "direction": pos.get("direction", "?"),
            "entry_price": entry_price,
            "current_price": current_price,
            "amount_usd": amount_usd,
            "unrealized_pnl": unrealized_pnl,
            "pnl_pct": pnl_pct,
            "time_remaining": time_remaining_str,
            "paper_trade": pos.get("order_id", "").startswith("PAPER"),
            "likely_resolved": likely_resolved,
        })
    
    return results


def _check_positions(markets: list[Market]):
    """Check open positions for resolution.
    
    Uses stored end_date if market is no longer in discovered list
    (resolved markets get dropped from the scan). Uses live price
    to determine outcome for better accuracy.
    """
    global _daily_pnl
    
    resolved = []
    for slug, pos in list(_open_positions.items()):
        # Get end_date from stored position or discovered market
        end_date = None
        market = next((m for m in markets if m.slug == slug), None)
        if market and market.end_date:
            end_date = market.end_date
        elif pos.get("end_date"):
            try:
                end_date = datetime.fromisoformat(pos["end_date"])
            except (ValueError, TypeError):
                pass
        
        if not end_date:
            continue
        
        # Check if market has expired
        if end_date <= datetime.now(timezone.utc):
            # Market resolved — determine outcome from live price
            # Use the last known price from the market or position
            direction = pos.get("direction", "Up")
            entry_price = pos.get("price", 0)
            amount_usd = pos.get("amount_usd", 0)
            shares = pos.get("shares", 0) or (int(amount_usd / entry_price) if entry_price > 0 else 0)
            
            # Try to get final price from market or Gamma API
            final_price = None
            if market:
                final_price = market.up_price if direction == "Up" else market.down_price
            
            # Use Gamma API for resolved markets not in current scan
            if final_price is None:
                from trader import get_live_price
                token_id = pos.get("token_id", "")
                if token_id:
                    mid, _, _, _ = get_live_price(token_id, slug=slug, direction=direction)
                    if mid is not None:
                        final_price = mid
            
            # Determine win/loss: price >= 0.90 means our side won
            won = final_price is not None and final_price >= 0.90
            
            if won:
                pnl = (final_price or 1.0) * shares - amount_usd
            else:
                pnl = -amount_usd
            
            # Update trade history
            for t in _trade_history:
                if t.get("market_slug") == slug and not t.get("result"):
                    t["result"] = "won" if won else "lost"
                    t["pnl"] = round(pnl, 2)
                    t["resolved_at"] = datetime.now(timezone.utc).isoformat()
            
            _daily_pnl += pnl
            _save_trade_history()
            
            # Remove from open positions
            resolved.append(slug)
    
    for slug in resolved:
        del _open_positions[slug]
    if resolved:
        _save_positions()


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    """Start the UpDown bot."""
    import telegram_bot as tb
    
    # Load state
    _load_trade_history()
    _load_positions()
    
    # Start scan loop in background thread
    scan_thread = threading.Thread(target=scan_loop, daemon=True)
    scan_thread.start()
    
    # Start Telegram bot (blocking)
    tb.run_bot()


if __name__ == "__main__":
    main()