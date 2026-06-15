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
                if not edge or not edge.is_tradeable:
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
            "time_remaining": market.time_remaining_str,
            "order_id": result.get("order_id", ""),
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_positions()
        
        _stats_increment("trades_placed")
        print(f"[engine] ✅ Trade placed: {market.coin.upper()} {market.timeframe} "
              f"{edge.direction} @ {price:.2f} for ${amount:.2f}")


def _check_positions(markets: list[Market]):
    """Check open positions for resolution."""
    global _daily_pnl
    
    resolved = []
    for slug, pos in list(_open_positions.items()):
        # Find matching market
        market = next((m for m in markets if m.slug == slug), None)
        if not market:
            continue
        
        # Check if market has expired
        if market.end_date and market.end_date < datetime.now(timezone.utc):
            # Market resolved — determine outcome
            up_won = market.up_price > market.down_price  # Simplified
            
            if pos["direction"] == "Up" and up_won:
                pnl = pos["amount_usd"] / pos["price"] - pos["amount_usd"]
            elif pos["direction"] == "Down" and not up_won:
                pnl = pos["amount_usd"] / pos["price"] - pos["amount_usd"]
            else:
                pnl = -pos["amount_usd"]
            
            # Update trade history
            for t in _trade_history:
                if t.get("market_slug") == slug and not t.get("result"):
                    t["result"] = "won" if pnl > 0 else "lost"
                    t["pnl"] = pnl
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