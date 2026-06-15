#!/usr/bin/env python3
"""Binance price feed — WebSocket real-time prices + REST fallback for klines."""

import json
import time
import threading
import urllib.request
from datetime import datetime, timezone, timedelta
from collections import deque
from models import PriceSnapshot
import config

# ── Symbol mappings ───────────────────────────────────────────────────────────
BINANCE_SYMBOLS = {
    "btc": "btcusdt",
    "eth": "ethusdt",
    "sol": "solusdt",
    "xrp": "xrpusdt",
    "doge": "dogeusdt",
    "bnb": "bnbusdt",
}

# ── Price Store ───────────────────────────────────────────────────────────────
# {symbol: deque of (timestamp, price, volume)}
_price_history = {coin: deque(maxlen=3600) for coin in BINANCE_SYMBOLS}
_latest_prices = {}  # {coin: PriceSnapshot}
_price_at_window_start = {}  # {key: price} where key = f"{coin}_{window_start_ts}"
_ws_thread = None
_ws_running = False


def _binance_rest_klines(symbol: str, interval: str = "1m", limit: int = 60) -> list:
    """Fetch recent klines from Binance REST API."""
    url = f"{config.BINANCE_REST_URL}/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-updown/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[price_feed] Binance klines error for {symbol}: {e}")
        return []


def _binance_rest_price(symbol: str) -> float:
    """Get current price from Binance REST API (fallback)."""
    binance_sym = BINANCE_SYMBOLS.get(symbol, symbol + "usdt").upper()
    url = f"{config.BINANCE_REST_URL}/ticker/price?symbol={binance_sym}"
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-updown/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return float(data.get("price", 0))
    except Exception as e:
        print(f"[price_feed] Binance price error for {symbol}: {e}")
        return 0.0


def _binance_rest_ticker(symbol: str) -> dict:
    """Get 24h ticker stats from Binance REST API."""
    binance_sym = BINANCE_SYMBOLS.get(symbol, symbol + "usdt").upper()
    url = f"{config.BINANCE_REST_URL}/ticker/24hr?symbol={binance_sym}"
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-updown/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[price_feed] Binance 24hr ticker error for {symbol}: {e}")
        return {}


def get_price(coin: str) -> float:
    """Get the latest price for a coin. Falls back to REST if WS not available."""
    if coin in _latest_prices:
        return _latest_prices[coin].price
    # Fallback to REST
    price = _binance_rest_price(coin)
    if price > 0:
        _latest_prices[coin] = PriceSnapshot(
            symbol=coin, price=price, timestamp=datetime.now(timezone.utc)
        )
    return price


def get_snapshot(coin: str) -> PriceSnapshot | None:
    """Get a full price snapshot for a coin."""
    if coin in _latest_prices:
        return _latest_prices[coin]
    # Fallback: fetch from REST
    price = _binance_rest_price(coin)
    if price > 0:
        ticker = _binance_rest_ticker(coin)
        snap = PriceSnapshot(
            symbol=coin, price=price,
            timestamp=datetime.now(timezone.utc),
            volume_1m=0,
            bid=float(ticker.get("bidPrice", 0)),
            ask=float(ticker.get("askPrice", 0)),
            high_1h=0, low_1h=0,
        )
        _latest_prices[coin] = snap
        return snap
    return None


def get_momentum(coin: str, lookback: int = 300) -> dict:
    """Calculate momentum from recent price history.
    
    Returns:
        dict with keys: trend, strength, volatility, direction, change_pct
    """
    history = _price_history.get(coin, deque())
    now = time.time()
    recent = [(ts, p) for ts, p, _ in history if now - ts < lookback]
    
    if len(recent) < 5:
        # Not enough WS data, use REST klines
        return _momentum_from_klines(coin, lookback)
    
    prices = [p for _, p in recent]
    if len(prices) < 2:
        return {"trend": 0.0, "strength": 0.0, "volatility": 0.0, "direction": "flat", "change_pct": 0.0}
    
    # Price change
    change_pct = (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] > 0 else 0.0
    
    # Trend: linear regression slope
    n = len(prices)
    x = list(range(n))
    x_mean = n / 2
    y_mean = sum(prices) / n
    numerator = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, prices))
    denominator = sum((xi - x_mean) ** 2 for xi in x)
    slope = numerator / denominator if denominator > 0 else 0.0
    
    # Normalize slope to %/second
    time_span = recent[-1][0] - recent[0][0] if len(recent) > 1 else 1
    trend_pct_per_sec = (slope * (prices[0] / y_mean)) / time_span * 100 if y_mean > 0 and time_span > 0 else 0.0
    
    # Strength: R² of the trend
    if y_mean == 0 or len(prices) < 3:
        strength = 0.0
    else:
        ss_res = sum((p - (slope * i + (y_mean - slope * x_mean))) ** 2 for i, p in enumerate(prices))
        ss_tot = sum((p - y_mean) ** 2 for p in prices)
        strength = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    
    # Volatility: std of returns
    returns = [(prices[i+1] - prices[i]) / prices[i] for i in range(len(prices)-1) if prices[i] > 0]
    volatility = (sum(r**2 for r in returns) / len(returns)) ** 0.5 if returns else 0.0
    
    # Direction
    if change_pct > 0.05:
        direction = "up"
    elif change_pct < -0.05:
        direction = "down"
    else:
        direction = "flat"
    
    return {
        "trend": trend_pct_per_sec,
        "strength": strength,
        "volatility": volatility,
        "direction": direction,
        "change_pct": change_pct,
    }


def _momentum_from_klines(coin: str, lookback: int) -> dict:
    """Fallback: calculate momentum from Binance REST klines."""
    binance_sym = BINANCE_SYMBOLS.get(coin, coin + "usdt")
    interval = "1m" if lookback <= 300 else "5m"
    limit = min(lookback // 60 + 1, 60)
    
    klines = _binance_rest_klines(binance_sym, interval, limit)
    if not klines or len(klines) < 2:
        return {"trend": 0.0, "strength": 0.0, "volatility": 0.0, "direction": "flat", "change_pct": 0.0}
    
    prices = [float(k[4]) for k in klines]  # Close prices
    
    if len(prices) < 2:
        return {"trend": 0.0, "strength": 0.0, "volatility": 0.0, "direction": "flat", "change_pct": 0.0}
    
    change_pct = (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] > 0 else 0.0
    returns = [(prices[i+1] - prices[i]) / prices[i] for i in range(len(prices)-1) if prices[i] > 0]
    volatility = (sum(r**2 for r in returns) / len(returns)) ** 0.5 if returns else 0.0
    
    direction = "up" if change_pct > 0.05 else "down" if change_pct < -0.05 else "flat"
    
    # Simple strength: ratio of directional candles
    up_candles = sum(1 for i in range(len(prices)-1) if prices[i+1] > prices[i])
    strength = up_candles / (len(prices) - 1) if len(prices) > 1 else 0.5
    
    # Trend: average return per minute
    avg_return = change_pct / max(len(prices) - 1, 1)
    
    return {
        "trend": avg_return,
        "strength": strength,
        "volatility": volatility,
        "direction": direction,
        "change_pct": change_pct,
    }


def capture_window_start_price(coin: str, window_start_ts: float) -> None:
    """Capture the current price as the 'price to beat' for a window."""
    price = get_price(coin)
    if price > 0:
        key = f"{coin}_{int(window_start_ts)}"
        _price_at_window_start[key] = price
        print(f"[price_feed] Captured {coin.upper()} price ${price:,.2f} at window start {int(window_start_ts)}")


def get_window_start_price(coin: str, window_start_ts: float) -> float | None:
    """Get the price at window start (the 'price to beat')."""
    key = f"{coin}_{int(window_start_ts)}"
    return _price_at_window_start.get(key)


def _ws_loop():
    """Background thread: connect to Binance WebSocket for real-time prices."""
    global _ws_running
    try:
        import websockets
        import asyncio
    except ImportError:
        print("[price_feed] websockets not available, using REST fallback only")
        return
    
    async def _connect():
        global _ws_running
        # Subscribe to trade streams for all enabled coins
        enabled_coins = [c for c, s in config.SERIES.items() if s.get("enabled") and _parse_coin_from_series(c) in BINANCE_SYMBOLS]
        coins = set(_parse_coin_from_series(s) for s in enabled_coins if _parse_coin_from_series(s) in BINANCE_SYMBOLS)
        
        if not coins:
            coins = {"btc", "eth"}  # Default
        
        streams = "/".join(f"{BINANCE_SYMBOLS[c]}@trade" for c in coins)
        url = f"{config.BINANCE_WS_URL}?streams={streams}"
        
        while _ws_running:
            try:
                async with websockets.connect(url) as ws:
                    print(f"[price_feed] Connected to Binance WS for {', '.join(c.upper() for c in coins)}")
                    async for msg in ws:
                        if not _ws_running:
                            break
                        try:
                            data = json.loads(msg)
                            # Combined stream format: {"stream": "btcusdt@trade", "data": {...}}
                            trade_data = data.get("data", data)
                            symbol = trade_data.get("s", "").lower()
                            price = float(trade_data.get("p", 0))
                            qty = float(trade_data.get("q", 0))
                            ts = trade_data.get("T", 0) / 1000  # ms to seconds
                            
                            # Map back to our coin names
                            coin = None
                            for c, bsym in BINANCE_SYMBOLS.items():
                                if bsym == symbol:
                                    coin = c
                                    break
                            
                            if coin and price > 0:
                                _price_history[coin].append((ts, price, qty))
                                _latest_prices[coin] = PriceSnapshot(
                                    symbol=coin, price=price,
                                    timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                                    volume_1m=0, bid=0, ask=0,
                                )
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                print(f"[price_feed] WS error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_connect())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


def _parse_coin_from_series(series_slug: str) -> str:
    """Extract coin from series slug like 'btc-up-or-down-5m'."""
    slug_lower = series_slug.lower()
    for coin in BINANCE_SYMBOLS:
        if slug_lower.startswith(coin):
            return coin
    return "unknown"


def start_price_feed():
    """Start the Binance WebSocket price feed in a background thread."""
    global _ws_thread, _ws_running
    _ws_running = True
    _ws_thread = threading.Thread(target=_ws_loop, daemon=True)
    _ws_thread.start()
    print("[price_feed] Binance WebSocket price feed started")


def stop_price_feed():
    """Stop the Binance WebSocket price feed."""
    global _ws_running
    _ws_running = False
    print("[price_feed] Binance WebSocket price feed stopped")


if __name__ == "__main__":
    # Test price feed
    print("Testing Binance price feed...")
    for coin in ["btc", "eth", "sol"]:
        price = get_price(coin)
        momentum = get_momentum(coin, 300)
        snap = get_snapshot(coin)
        print(f"  {coin.upper()}: ${price:,.2f} | "
              f"Change: {momentum['change_pct']:+.2f}% | "
              f"Direction: {momentum['direction']} | "
              f"Vol: {momentum['volatility']:.4f}")
    
    print("\nStarting WebSocket feed (5 seconds)...")
    start_price_feed()
    time.sleep(5)
    stop_price_feed()
    
    for coin in ["btc", "eth", "sol"]:
        if coin in _latest_prices:
            snap = _latest_prices[coin]
            print(f"  {coin.upper()}: ${snap.price:,.2f} (WS)")