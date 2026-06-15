#!/usr/bin/env python3
"""Edge detection — detect when Polymarket odds lag real exchange prices.

The core insight: crypto Up/Down markets are predictable when there's a
clear, significant price move since the window opened. Small moves (<0.5%)
are noise. The model only fires when:
1. Price has moved significantly since window start (minimum move threshold)
2. The Polymarket odds haven't caught up yet (edge > threshold)
3. There's enough liquidity and volume to actually trade

Confidence is deliberately conservative — no signal should ever be 100%.
"""

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
    
    Returns None if no tradeable edge is found.
    """
    coin = market.coin
    timeframe = market.timeframe
    
    # ── Skip illiquid markets ──────────────────────────────────────────────
    # For Up/Down markets, liquidity is a better filter than volume
    # (future windows have $0 volume but $10k+ liquidity from AMMs)
    min_liq = config.SERIES.get(market.series_slug, {}).get("min_liquidity", 500)
    if market.liquidity < min_liq:
        return None
    if market.volume_24h < 100 and market.liquidity < 1000:
        return None  # Truly dead market — no volume AND no liquidity
    
    # ── Get exchange data ─────────────────────────────────────────────────
    current_price = price_feed.get_price(coin)
    if current_price <= 0:
        return None
    
    momentum = price_feed.get_momentum(coin, config.MOMENTUM_LOOKBACK)
    
    # ── Get "price to beat" (price at window start) ──────────────────────
    price_to_beat = None
    if market.event_start_time:
        window_start_ts = market.event_start_time.timestamp()
        price_to_beat = price_feed.get_window_start_price(coin, window_start_ts)
        
        if price_to_beat is None:
            price_to_beat = _get_kline_open(coin, market.event_start_time)
    
    # ── Calculate price change since window start ─────────────────────────
    if price_to_beat and price_to_beat > 0:
        change_since_start_pct = (current_price - price_to_beat) / price_to_beat * 100
    else:
        # No reference point — use momentum lookback
        change_since_start_pct = momentum["change_pct"]
        price_to_beat = current_price / (1 + change_since_start_pct / 100)
    
    # ── Chainlink oracle divergence ────────────────────────────────────────
    # 15m & 1h markets resolve via Chainlink, not Binance.
    # If Chainlink and Binance diverge, our edge calculation may be wrong.
    chainlink_result = price_feed.get_chainlink_price(coin)
    chainlink_divergence = 0.0  # % difference: positive = Chainlink > Binance
    
    if chainlink_result:
        chainlink_price, chainlink_divergence = chainlink_result
    
    # ── Minimum move threshold ────────────────────────────────────────────
    # Small moves are noise, not signal. Don't fire on sub-0.5% drift.
    MIN_MOVE_PCT = {
        "5m":  0.15,   # 5m windows: need 0.15% move
        "15m": 0.25,   # 15m windows: need 0.25% move
        "1h":  0.40,   # 1h windows: need 0.40% move
        "4h":  0.60,   # 4h windows: need 0.60% move
        "daily": 1.00, # Daily windows: need 1% move
    }
    min_move = MIN_MOVE_PCT.get(timeframe, 0.30)
    if abs(change_since_start_pct) < min_move:
        return None  # Move too small — noise, not signal
    
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
    
    # ── Confidence ─────────────────────────────────────────────────────────
    confidence = _calculate_confidence(
        edge_pp=edge_pp,
        momentum=momentum,
        time_remaining_pct=market.time_remaining_pct,
        timeframe=timeframe,
        market=market,
        change_pct=change_since_start_pct,
        chainlink_divergence=chainlink_divergence,
    )
    
    # ── Don't fire if below minimum confidence ────────────────────────────
    if confidence < config.MIN_CONFIDENCE:
        return None
    
    # ── Don't fire if edge is below minimum ──────────────────────────────
    if edge_magnitude < config.MIN_EDGE_PP:
        return None
    
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
        edge_pp=round(edge_pp, 1),
        confidence=round(confidence, 2),
        estimated_true_prob=round(p_up, 3),
        market_implied_prob=round(market_p_up, 3),
        reasoning=reasoning,
        momentum_pct=round(change_since_start_pct, 2),
        momentum_direction=momentum["direction"],
        volatility=momentum["volatility"],
        chainlink_divergence=round(chainlink_divergence, 4),
    )


def _estimate_p_up(change_pct: float, momentum: dict, timeframe: str,
                    time_remaining_pct: float) -> float:
    """Estimate the true probability of "Up" winning.
    
    The model is deliberately conservative:
    - Small moves (<1%) barely shift probability from 50%
    - Only large, sustained moves (>1.5%) create strong signals
    - 4h+ timeframes apply mean reversion for extreme moves
    """
    # Base rate
    p = 0.50
    
    # ── Move-based probability ──────────────────────────────────────────────
    # The actual price change since window start is the primary signal.
    # Scale: tiny moves barely move P, large moves shift it significantly.
    # Using a logistic-style curve that maxes out at ~85% for 3%+ moves.
    abs_move = abs(change_pct)
    direction_sign = 1.0 if change_pct > 0 else -1.0
    
    if abs_move < 0.5:
        # Sub-0.5% move: barely shifts from 50/50 — max ±3pp
        move_adj = abs_move * 0.06  # 0.3% move → ±1.8pp
    elif abs_move < 1.0:
        # 0.5-1% move: moderate signal — ±3 to ±8pp
        move_adj = 0.03 + (abs_move - 0.5) * 0.10  # 0.5% → 3pp, 1% → 8pp
    elif abs_move < 2.0:
        # 1-2% move: strong signal — ±8 to ±18pp
        move_adj = 0.08 + (abs_move - 1.0) * 0.10  # 1% → 8pp, 2% → 18pp
    else:
        # 2%+ move: very strong signal — ±18 to ±30pp (capped)
        move_adj = 0.18 + min(0.12, (abs_move - 2.0) * 0.06)  # 2% → 18pp, 4% → 30pp
    
    p += direction_sign * move_adj
    
    # ── Momentum reinforcement ──────────────────────────────────────────────
    # If recent momentum agrees with the move, boost it slightly.
    # If momentum disagrees (mean reversion territory), reduce it.
    momentum_dir = momentum.get("direction", "flat")
    momentum_strength = momentum.get("strength", 0)
    
    if momentum_dir == "up" and change_pct > 0:
        p += 0.02 * momentum_strength  # Small boost for confirming momentum
    elif momentum_dir == "down" and change_pct < 0:
        p += 0.02 * momentum_strength
    elif momentum_dir in ("up", "down") and direction_sign != (1.0 if momentum_dir == "up" else -1.0):
        # Momentum disagrees with the move — reduce confidence
        p -= 0.01 * momentum_strength
    
    # ── Mean reversion (4h and daily only) ────────────────────────────────
    if timeframe in ("4h", "daily") and abs_move > config.MEAN_REVERSION_THRESHOLD * 100:
        # Extreme moves tend to partially revert in longer windows
        reversion_strength = 0.4 if timeframe == "daily" else 0.2
        # Pull back toward 50%
        overshoot = abs(p - 0.50)
        p = 0.50 + (p - 0.50) * (1 - reversion_strength * min(1.0, abs_move / 4.0))
    
    # ── Time decay ────────────────────────────────────────────────────────
    # With <20% of window remaining, current direction is more likely to hold
    if time_remaining_pct < 0.2:
        p = 0.50 + (p - 0.50) * 1.15  # Slight amplification
    # With >80% remaining, early moves can reverse — dampen slightly
    elif time_remaining_pct > 0.8:
        p = 0.50 + (p - 0.50) * 0.85
    
    # ── Clamp ──────────────────────────────────────────────────────────────
    p = max(0.10, min(0.90, p))
    
    return p


def _calculate_confidence(edge_pp: float, momentum: dict, 
                          time_remaining_pct: float, timeframe: str,
                          market: Market, change_pct: float,
                          chainlink_divergence: float = 0.0) -> float:
    """Calculate confidence in the edge estimate.
    
    Confidence is deliberately capped at 85% — we should never be 100%
    certain about a crypto prediction.
    
    Key factors:
    - Size of the actual price move (bigger = more confident)
    - Whether momentum confirms the direction
    - Market liquidity and volume
    - Time remaining in window
    """
    confidence = 0.40  # Start lower — base uncertainty
    
    # ── Move size → confidence (the biggest factor) ───────────────────────
    abs_move = abs(change_pct)
    if abs_move >= 2.0:
        confidence += 0.25  # 2%+ move: strong signal
    elif abs_move >= 1.0:
        confidence += 0.15  # 1-2%: moderate signal
    elif abs_move >= 0.5:
        confidence += 0.05  # 0.5-1%: weak signal
    # Sub-0.5%: no bonus (already filtered by MIN_MOVE threshold)
    
    # ── Edge magnitude → confidence ──────────────────────────────────────
    abs_edge = abs(edge_pp)
    if abs_edge >= 20:
        confidence += 0.15  # Massive edge
    elif abs_edge >= 12:
        confidence += 0.10  # Strong edge
    elif abs_edge >= 8:
        confidence += 0.05  # Decent edge
    # Sub-8pp: no bonus (already filtered by MIN_EDGE_PP)
    
    # ── Momentum strength → confidence ────────────────────────────────────
    strength = momentum.get("strength", 0)
    if strength > 0.7:
        confidence += 0.05  # Strong momentum confirms
    elif strength < 0.3:
        confidence -= 0.05  # Weak momentum = uncertain
    
    # ── Volatility → confidence ───────────────────────────────────────────
    vol = momentum.get("volatility", 0)
    if 0.001 < vol < 0.008:
        confidence += 0.03  # Healthy, moderate volatility
    elif vol >= 0.02:
        confidence -= 0.08  # Wild volatility — very uncertain
    
    # ── Chainlink oracle divergence → confidence ──────────────────────────
    # If Binance and Chainlink disagree, our edge is less reliable.
    # 15m/1h markets resolve via Chainlink — divergence = risk.
    if abs(chainlink_divergence) > 0.15:
        # >0.15% divergence: significant risk that resolution won't match our signal
        confidence -= 0.15
    elif abs(chainlink_divergence) > 0.08:
        # 0.08-0.15%: moderate risk
        confidence -= 0.08
    elif abs(chainlink_divergence) > 0.03:
        # 0.03-0.08%: small risk
        confidence -= 0.03
    
    # ── Market liquidity → confidence ─────────────────────────────────────
    if market.liquidity >= 5000:
        confidence += 0.03  # Deep book
    elif market.liquidity < 500:
        confidence -= 0.08  # Illiquid — unreliable pricing
    
    # ── Volume → confidence ───────────────────────────────────────────────
    if market.volume_24h >= 5000:
        confidence += 0.03
    elif market.volume_24h < 2000:
        confidence -= 0.03  # Low volume
    
    # ── Time remaining → confidence ──────────────────────────────────────
    if 0.3 < time_remaining_pct < 0.7:
        confidence += 0.03  # Sweet spot — enough time, not too early
    elif time_remaining_pct < 0.1:
        confidence -= 0.05  # Too late — slippage risk
    elif time_remaining_pct > 0.9:
        confidence -= 0.03  # Too early — plenty of time for reversal
    
    # ── Timeframe → confidence ────────────────────────────────────────────
    # 15m and 1h are the sweet spots for momentum-based prediction
    tf_confidence = {"5m": -0.02, "15m": 0.02, "1h": 0.03, "4h": 0.01, "daily": -0.02}
    confidence += tf_confidence.get(timeframe, 0)
    
    # ── Cap at 0.85 — never be 100% certain ──────────────────────────────
    return max(0.10, min(0.85, confidence))


def _get_kline_open(coin: str, start_time: datetime) -> float | None:
    """Get the opening price at a specific time from Binance klines."""
    binance_sym = price_feed.BINANCE_SYMBOLS.get(coin, coin + "usdt").upper()
    start_ms = int(start_time.timestamp() * 1000)
    
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
        parts.append(f"Start: ${price_to_beat:,.2f}")
    parts.append(f"Move: {change_pct:+.2f}%")
    parts.append(f"Trend: {momentum['direction']} (str {momentum['strength']:.0%})")
    parts.append(f"P(Up)={p_up:.1%} vs Market={market_p_up:.1%}")
    parts.append(f"Edge: {edge_pp:+.1f}pp → {direction}")
    parts.append(f"Conf: {confidence:.0%}")
    return " | ".join(parts)


if __name__ == "__main__":
    # Test edge detection with live data
    print("Testing edge detection (using live Binance prices)...\n")
    
    markets = market_discovery.discover_markets(force_refresh=True)
    if not markets:
        print("No active markets found. Make sure series are enabled in config.")
    else:
        edges_found = 0
        for m in sorted(markets, key=lambda x: x.volume_24h, reverse=True)[:20]:
            market_discovery.refresh_market_prices(m)
            edge = calculate_edge(m)
            if edge:
                print(f"  {edge.reasoning}")
                print(f"  Tradeable: {'✓' if edge.is_tradeable else '✗'}")
                print()
                edges_found += 1
        print(f"\n{edges_found} edges found out of {len(markets)} markets")