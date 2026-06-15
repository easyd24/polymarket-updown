#!/usr/bin/env python3
"""Edge detection — detect when Polymarket odds lag real exchange prices."""

import json
import math
import urllib.request
from datetime import datetime, timezone, timedelta
from models import Market, EdgeResult
import market_discovery
import price_feed
import config


def calculate_edge(market: Market) -> EdgeResult | None:
    """Calculate edge for a single Up/Down market.
    
    The core insight: if BTC is surging on Binance but Polymarket's "Up" 
    is still at 48¢, we buy Up. The edge = our estimated true probability 
    minus the market's implied probability.
    
    For longer timeframes (4h, daily), we also consider mean reversion — 
    if BTC has already moved 2%+ in the window, Down becomes more likely.
    """
    coin = market.coin
    timeframe = market.timeframe
    
    # ── Get exchange data ─────────────────────────────────────────────────
    current_price = price_feed.get_price(coin)
    if current_price <= 0:
        return None
    
    momentum = price_feed.get_momentum(coin, config.MOMENTUM_LOOKBACK)
    if momentum["direction"] == "flat" and abs(momentum["change_pct"]) < 0.03:
        # No meaningful movement — no edge
        return None
    
    # ── Get "price to beat" (price at window start) ──────────────────────
    price_to_beat = None
    if market.event_start_time:
        # Try to get the price we captured at window start
        window_start_ts = market.event_start_time.timestamp()
        price_to_beat = price_feed.get_window_start_price(coin, window_start_ts)
        
        if price_to_beat is None:
            # Fallback: use kline open price from Binance
            price_to_beat = _get_kline_open(coin, market.event_start_time)
    
    # ── Calculate price change since window start ─────────────────────────
    if price_to_beat and price_to_beat > 0:
        change_since_start_pct = (current_price - price_to_beat) / price_to_beat * 100
    else:
        # No reference point — use momentum lookback
        change_since_start_pct = momentum["change_pct"]
        price_to_beat = current_price / (1 + change_since_start_pct / 100)
    
    # ── Estimate true probability of "Up" ────────────────────────────────
    p_up = _estimate_p_up(
        change_pct=change_since_start_pct,
        momentum=momentum,
        timeframe=timeframe,
        time_remaining_pct=market.time_remaining_pct,
    )
    
    # ── Get market-implied probability ───────────────────────────────────
    market_p_up = market.up_price
    
    # ── Calculate edge ───────────────────────────────────────────────────
    edge_pp = (p_up - market_p_up) * 100  # in percentage points
    
    # Direction: buy Up if p_up > market_p_up, buy Down otherwise
    direction = "Up" if edge_pp > 0 else "Down"
    edge_magnitude = abs(edge_pp)
    
    # ── Confidence ────────────────────────────────────────────────────────
    confidence = _calculate_confidence(
        edge_pp=edge_pp,
        momentum=momentum,
        time_remaining_pct=market.time_remaining_pct,
        timeframe=timeframe,
        market=market,
    )
    
    # ── Reasoning ─────────────────────────────────────────────────────────
    reasoning = _build_reasoning(
        coin=coin, timeframe=timeframe, current_price=current_price,
        price_to_beat=price_to_beat, change_pct=change_since_start_pct,
        direction=direction, edge_pp=edge_magnitude, confidence=confidence,
        p_up=p_up, market_p_up=market_p_up, momentum=momentum,
    )
    
    return EdgeResult(
        market=market,
        direction=direction,
        edge_pp=round(edge_pp, 1),  # Signed: positive = buy Up
        confidence=round(confidence, 2),
        estimated_true_prob=round(p_up, 3),
        market_implied_prob=round(market_p_up, 3),
        reasoning=reasoning,
        momentum_pct=round(change_since_start_pct, 2),
        momentum_direction=momentum["direction"],
        volatility=momentum["volatility"],
    )


def _estimate_p_up(change_pct: float, momentum: dict, timeframe: str,
                    time_remaining_pct: float) -> float:
    """Estimate the true probability of "Up" winning.
    
    Model:
    - Base rate: 50% (no information)
    - Momentum adjustment: ±5-15pp based on trend strength and direction
    - Volatility adjustment: higher vol → more extreme probabilities
    - Time decay: edge shrinks as window nears end (less time for reversal)
    - Mean reversion: for windows > 1h, extreme moves tend to revert
    """
    # Base rate
    p = 0.50
    
    # ── Momentum adjustment ──────────────────────────────────────────────
    # Stronger momentum = more likely to continue in that direction
    # Scale: ±5-15pp based on change percentage and trend strength
    if momentum["direction"] == "up":
        # Upward momentum → P(Up) increases
        # Strong moves (>1%) get more weight than weak moves (<0.1%)
        momentum_adj = min(0.15, max(0.02, abs(change_pct) * 8)) * momentum["strength"]
        p += momentum_adj
    elif momentum["direction"] == "down":
        # Downward momentum → P(Up) decreases
        momentum_adj = min(0.15, max(0.02, abs(change_pct) * 8)) * momentum["strength"]
        p -= momentum_adj
    
    # ── Volatility adjustment ────────────────────────────────────────────
    # In high-vol environments, directional bets are more likely to win
    # because the price is more likely to move significantly
    vol = momentum.get("volatility", 0)
    if vol > 0.005:  # Significant volatility
        # Amplify the directional signal
        vol_multiplier = min(1.3, 1.0 + vol * 20)
        p = 0.50 + (p - 0.50) * vol_multiplier
    
    # ── Mean reversion (4h and daily only) ────────────────────────────────
    if timeframe in ("4h", "daily") and abs(change_pct) > config.MEAN_REVERSION_THRESHOLD * 100:
        # Extreme moves tend to partially revert
        # If BTC is already up 2%+, there's some chance it pulls back
        reversion_strength = 0.3 if timeframe == "daily" else 0.15
        if change_pct > 0:
            p -= abs(change_pct) * 0.02 * reversion_strength
        else:
            p += abs(change_pct) * 0.02 * reversion_strength
    
    # ── Time decay ────────────────────────────────────────────────────────
    # As window nears end, current trend is more likely to persist
    # But also, less time for new trends to develop
    if time_remaining_pct < 0.3:
        # Less than 30% of window remaining — current direction more likely to hold
        # Amplify the signal slightly
        p = 0.50 + (p - 0.50) * 1.2
    
    # Clamp to [0.01, 0.99]
    p = max(0.01, min(0.99, p))
    
    return p


def _calculate_confidence(edge_pp: float, momentum: dict, 
                          time_remaining_pct: float, timeframe: str,
                          market: Market) -> float:
    """Calculate confidence in the edge estimate.
    
    Higher confidence when:
    - Edge is large (strong signal)
    - Momentum is strong (clear direction)
    - Market is liquid (reliable pricing)
    - Time remaining is reasonable
    """
    confidence = 0.50
    
    # Edge magnitude → confidence
    abs_edge = abs(edge_pp)
    if abs_edge >= 15:
        confidence += 0.25
    elif abs_edge >= 10:
        confidence += 0.20
    elif abs_edge >= 5:
        confidence += 0.10
    else:
        confidence -= 0.05
    
    # Momentum strength → confidence
    strength = momentum.get("strength", 0)
    if strength > 0.7:
        confidence += 0.15
    elif strength > 0.5:
        confidence += 0.10
    elif strength < 0.3:
        confidence -= 0.05
    
    # Volatility → confidence (moderate vol is good, extreme is bad)
    vol = momentum.get("volatility", 0)
    if 0.001 < vol < 0.01:
        confidence += 0.05  # Healthy volatility
    elif vol >= 0.02:
        confidence -= 0.05  # Too volatile, hard to predict
    
    # Market liquidity → confidence
    if market.liquidity >= 5000:
        confidence += 0.05
    elif market.liquidity < 500:
        confidence -= 0.10  # Illiquid market
    
    # Time remaining → confidence
    if 0.2 < time_remaining_pct < 0.8:
        confidence += 0.05  # Sweet spot
    elif time_remaining_pct < 0.1:
        confidence -= 0.05  # Too late
    
    # Timeframe → confidence
    # Shorter timeframes = more predictable momentum
    tf_confidence = {"5m": 0.05, "15m": 0.08, "1h": 0.10, "4h": 0.05, "daily": 0.03}
    confidence += tf_confidence.get(timeframe, 0)
    
    return max(0.0, min(1.0, confidence))


def _get_kline_open(coin: str, start_time: datetime) -> float | None:
    """Get the opening price at a specific time from Binance klines."""
    binance_sym = price_feed.BINANCE_SYMBOLS.get(coin, coin + "usdt").upper()
    start_ms = int(start_time.timestamp() * 1000)
    
    # Fetch 1m kline at the start time
    url = (f"{config.BINANCE_REST_URL}/klines?symbol={binance_sym}"
           f"&interval=1m&startTime={start_ms}&limit=1")
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-updown/1.0"})
    try:
        import urllib.request as urllib2
        with urllib2.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data and len(data) > 0:
                return float(data[0][1])  # Open price
    except Exception as e:
        print(f"[edge_detector] Kline open error for {coin}: {e}")
    return None


def _build_reasoning(coin, timeframe, current_price, price_to_beat, change_pct,
                     direction, edge_pp, confidence, p_up, market_p_up, momentum) -> str:
    """Build human-readable reasoning string."""
    parts = []
    parts.append(f"{coin.upper()} {timeframe}: ${current_price:,.2f}")
    if price_to_beat:
        parts.append(f"Price to beat: ${price_to_beat:,.2f}")
    parts.append(f"Move: {change_pct:+.2f}%")
    parts.append(f"Trend: {momentum['direction']} (strength {momentum['strength']:.0%})")
    parts.append(f"Est P(Up)={p_up:.1%} vs Market={market_p_up:.1%}")
    parts.append(f"Edge: {edge_pp:+.1f}pp → Buy {direction}")
    parts.append(f"Confidence: {confidence:.0%}")
    return " | ".join(parts)


if __name__ == "__main__":
    # Test edge detection with mock data
    print("Testing edge detection (using live Binance prices)...\n")
    
    # Discover markets
    markets = market_discovery.discover_markets(force_refresh=True)
    if not markets:
        print("No active markets found. Make sure series are enabled in config.")
    else:
        for m in sorted(markets, key=lambda x: x.volume_24h, reverse=True)[:5]:
            # Refresh prices
            market_discovery.refresh_market_prices(m)
            edge = calculate_edge(m)
            if edge:
                print(f"  {edge.reasoning}")
                print(f"  Tradeable: {'✓' if edge.is_tradeable else '✗'}")
                print()