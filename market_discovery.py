#!/usr/bin/env python3
"""Market discovery — find active Up/Down markets via the Gamma Series API."""

import json
import urllib.request
import urllib.error
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from models import Market
import config

# Coin name mappings
COIN_NAMES = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
    "xrp": "ripple-xrp", "doge": "dogecoin", "bnb": "binance-coin",
}

# Timeframe parsing from tags
TIMEFRAME_MAP = {
    "5M": "5m", "15M": "15m", "1H": "1h", "4H": "4h",
    "DAILY": "daily", "1D": "daily",
}

# Cache for discovered markets
_market_cache = {}
_cache_timestamp = 0
CACHE_TTL = 55  # Re-discover markets every 55 seconds


def _gamma_get(path: str, params: dict = None) -> dict | list:
    """Make a GET request to the Gamma API."""
    url = config.GAMMA_API + path
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-updown/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"[market_discovery] Gamma API HTTP {e.code}: {e.reason}")
        return [] if path.startswith("/markets") or path.startswith("/events") else {}
    except Exception as e:
        print(f"[market_discovery] Gamma API error: {e}")
        return [] if path.startswith("/markets") or path.startswith("/events") else {}


def _parse_timeframe(tags: list[str]) -> str:
    """Extract timeframe from CLOB tags like '5M', '15M', '1H'."""
    for tag in tags:
        tag_upper = tag.upper()
        if tag_upper in TIMEFRAME_MAP:
            return TIMEFRAME_MAP[tag_upper]
    return "unknown"


def _parse_coin(slug: str, tags: list[str] = None) -> str:
    """Extract coin from series slug or tags."""
    # From slug: "btc-up-or-down-5m" → "btc"
    slug_lower = slug.lower()
    for coin in COIN_NAMES:
        if slug_lower.startswith(coin):
            return coin
    # From tags
    if tags:
        for tag in tags:
            tag_lower = tag.lower()
            for coin in COIN_NAMES:
                if coin in tag_lower:
                    return coin
    return "unknown"


def _parse_resolution_source(slug: str, tags: list[str] = None) -> str:
    """Determine resolution source from timeframe."""
    tf = _parse_timeframe(tags or [])
    if tf in ("5m", "15m", "4h"):
        return "chainlink"
    elif tf in ("1h", "daily"):
        return "binance"
    return "unknown"


def get_series_events(series_slug: str) -> list[dict]:
    """Get all active events for a series."""
    events = _gamma_get("/events", {
        "series_slug": series_slug,
        "limit": "100",
        "active": "true",
        "closed": "false",
    })
    if not events:
        return []
    # Gamma API returns a list directly
    if isinstance(events, list):
        return events
    # Sometimes wrapped in a dict
    return events.get("data", [])


def get_market_detail(slug: str) -> Optional[dict]:
    """Get full market details from Gamma API."""
    markets = _gamma_get("/markets", {"slug": slug, "limit": "1"})
    if markets and isinstance(markets, list) and len(markets) > 0:
        return markets[0]
    return None


def discover_markets(force_refresh: bool = False) -> list[Market]:
    """Discover all active Up/Down markets from enabled series.
    
    Returns a list of Market objects for markets that are currently
    within their trading window and meet minimum liquidity requirements.
    """
    global _market_cache, _cache_timestamp
    
    now = datetime.now(timezone.utc)
    if not force_refresh and _market_cache and (now.timestamp() - _cache_timestamp) < CACHE_TTL:
        return list(_market_cache.values())
    
    markets = {}
    
    for series_slug, series_config in config.SERIES.items():
        if not series_config.get("enabled", False):
            continue
        
        min_liquidity = series_config.get("min_liquidity", 500)
        
        # Get events for this series
        events = get_series_events(series_slug)
        
        for event in events:
            event_markets = event.get("markets", [])
            
            for m in event_markets:
                try:
                    market = _parse_market(m, series_slug)
                    if market and market.active and market.liquidity >= min_liquidity:
                        # Only include markets that haven't expired
                        if market.end_date and market.end_date > now:
                            # Only include markets with enough time remaining
                            if market.time_remaining_pct > 0.05:  # At least 5% of window left
                                markets[market.slug] = market
                except Exception as e:
                    print(f"[market_discovery] Error parsing market: {e}")
                    continue
    
    _market_cache = markets
    _cache_timestamp = now.timestamp()
    print(f"[market_discovery] Found {len(markets)} active markets across {sum(1 for v in config.SERIES.values() if v.get('enabled'))} series")
    return list(markets.values())


def _parse_market(m: dict, series_slug: str) -> Optional[Market]:
    """Parse a Gamma API market dict into a Market object."""
    try:
        slug = m.get("slug", "")
        question = m.get("question", "")
        
        # Parse outcomes — should be ["Up", "Down"] for Up/Down markets
        outcomes_raw = m.get("outcomes", "")
        if isinstance(outcomes_raw, str):
            outcomes = json.loads(outcomes_raw)
        else:
            outcomes = outcomes_raw
        
        if len(outcomes) < 2:
            return None
        
        # Parse outcome prices
        prices_raw = m.get("outcomePrices", "")
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw) if prices_raw else [0, 0]
        else:
            prices = prices_raw or [0, 0]
        
        # Parse CLOB token IDs
        tokens_raw = m.get("clobTokenIds", "")
        if isinstance(tokens_raw, str):
            tokens = json.loads(tokens_raw) if tokens_raw else ["", ""]
        else:
            tokens = tokens_raw or ["", ""]
        
        # Determine which outcome is "Up" and which is "Down"
        up_idx = 0
        down_idx = 1
        if outcomes[0] == "Up":
            up_idx, down_idx = 0, 1
        elif outcomes[0] == "Down":
            up_idx, down_idx = 1, 0
        # If outcomes are ["Yes", "No"], "Yes" = Up for up/down markets
        elif outcomes[0] == "Yes":
            up_idx, down_idx = 0, 1
        
        up_price = float(prices[up_idx]) if len(prices) > up_idx else 0.0
        down_price = float(prices[down_idx]) if len(prices) > down_idx else 0.0
        
        # Parse tags
        tags = m.get("tags", [])
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except:
                tags = []
        
        # Parse dates
        end_date_str = m.get("endDate", m.get("end_date_iso", ""))
        end_date = None
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except:
                pass
        
        event_start_str = m.get("eventStartTime", "")
        event_start_time = None
        if event_start_str:
            try:
                event_start_time = datetime.fromisoformat(event_start_str.replace("Z", "+00:00"))
            except:
                pass
        
        # Extract condition ID
        condition_id = m.get("conditionId", m.get("condition_id", ""))
        
        # Calculate coin and timeframe
        coin = _parse_coin(series_slug, tags)
        timeframe = _parse_timeframe(tags)
        resolution_source = _parse_resolution_source(series_slug, tags)
        
        # If timeframe unknown from tags, try from slug
        if timeframe == "unknown":
            if "-5m" in slug or "-5m" in series_slug:
                timeframe = "5m"
            elif "-15m" in slug or "-15m" in series_slug:
                timeframe = "15m"
            elif "-hourly" in slug or "-hourly" in series_slug:
                timeframe = "1h"
            elif "-4h" in slug or "-4h" in series_slug:
                timeframe = "4h"
            elif "-daily" in slug or "-daily" in series_slug:
                timeframe = "daily"
        
        return Market(
            slug=slug,
            question=question,
            series_slug=series_slug,
            coin=coin,
            timeframe=timeframe,
            condition_id=condition_id,
            up_token_id=tokens[up_idx] if len(tokens) > up_idx else "",
            down_token_id=tokens[down_idx] if len(tokens) > down_idx else "",
            up_price=up_price,
            down_price=down_price,
            liquidity=float(m.get("liquidity", 0) or 0),
            volume_24h=float(m.get("volume", 0) or 0),
            event_start_time=event_start_time,
            end_date=end_date,
            resolution_source=resolution_source,
            active=m.get("active", True),
        )
    except Exception as e:
        print(f"[market_discovery] Parse error for market {m.get('slug', '?')}: {e}")
        return None


def refresh_market_prices(market: Market) -> Optional[Market]:
    """Refresh a market's prices from the CLOB API."""
    try:
        # Get midpoint prices
        for token_id, attr in [(market.up_token_id, "up_price"), (market.down_token_id, "down_price")]:
            if not token_id:
                continue
            url = f"{config.CLOB_API}/midpoint?token_id={token_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "polymarket-updown/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                price = float(data.get("mid", 0))
                setattr(market, attr, price)
        return market
    except Exception as e:
        print(f"[market_discovery] Price refresh error for {market.slug}: {e}")
        return None


if __name__ == "__main__":
    # Test market discovery
    print("Discovering active Up/Down markets...")
    markets = discover_markets(force_refresh=True)
    print(f"\nFound {len(markets)} markets:")
    for m in sorted(markets, key=lambda x: x.volume_24h, reverse=True)[:10]:
        print(f"  {m.coin.upper()} {m.timeframe:>5} | Up={m.up_price:.2f} Down={m.down_price:.2f} "
              f"| Vol=${m.volume_24h:,.0f} Liq=${m.liquidity:,.0f} "
              f"| {m.time_remaining_str} left | {m.slug[:50]}")