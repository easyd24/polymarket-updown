#!/usr/bin/env python3
"""Data models for the Up/Down bot."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import config
from typing import Optional


@dataclass
class Market:
    """Represents an active Up/Down market on Polymarket."""
    slug: str                    # e.g. "btc-updown-5m-1781484300"
    question: str                # e.g. "Will the price of Bitcoin go up or down?"
    series_slug: str             # e.g. "btc-up-or-down-5m"
    coin: str                    # "btc", "eth", "sol"
    timeframe: str               # "5m", "15m", "1h", "4h", "daily"
    condition_id: str            # Polymarket condition ID
    up_token_id: str             # CLOB token ID for "Up" outcome
    down_token_id: str           # CLOB token ID for "Down" outcome
    up_price: float = 0.0        # Current Up price (0-1)
    down_price: float = 0.0      # Current Down price (0-1)
    liquidity: float = 0.0       # Total liquidity in $
    volume_24h: float = 0.0      # 24h volume in $
    event_start_time: Optional[datetime] = None  # Window start (UTC)
    end_date: Optional[datetime] = None           # Window end (UTC)
    resolution_source: str = ""  # "chainlink_streams", "binance", or "unknown"
    price_to_beat: Optional[float] = None  # Price at window start
    active: bool = True

    @property
    def time_remaining_pct(self) -> float:
        """How much of the window is left (0-1)."""
        if not self.end_date:
            return 0.0
        now = datetime.now(timezone.utc)
        total = (self.end_date - self.event_start_time).total_seconds() if self.event_start_time else 0
        remaining = (self.end_date - now).total_seconds()
        if total <= 0:
            return 0.0
        return max(0.0, min(1.0, remaining / total))

    @property
    def time_remaining_str(self) -> str:
        """Human-readable time remaining."""
        if not self.end_date:
            return "unknown"
        remaining = (self.end_date - datetime.now(timezone.utc)).total_seconds()
        if remaining < 0:
            return "expired"
        if remaining < 60:
            return f"{int(remaining)}s"
        if remaining < 3600:
            return f"{int(remaining / 60)}m {int(remaining % 60)}s"
        hours = int(remaining / 3600)
        mins = int((remaining % 3600) / 60)
        return f"{hours}h {mins}m"

    @property
    def spread(self) -> float:
        """Bid-ask spread."""
        return abs(self.up_price + self.down_price - 1.0)


@dataclass
class EdgeResult:
    """Result of edge detection for a market."""
    market: Market
    direction: str               # "Up" or "Down"
    edge_pp: float               # Edge in percentage points
    confidence: float            # 0-1 confidence in the edge
    estimated_true_prob: float   # Our estimate of P(Up)
    market_implied_prob: float   # Polymarket's implied P(Up)
    reasoning: str = ""          # Human-readable explanation
    momentum_pct: float = 0.0   # Price change since window start
    momentum_direction: str = "flat"  # "up", "down", "flat"
    volatility: float = 0.0     # Realized volatility
    chainlink_divergence: float | None = None  # % diff between Chainlink and Binance

    @property
    def is_tradeable(self) -> bool:
        """Whether this edge meets minimum thresholds."""
        return self.edge_pp >= config.MIN_EDGE_PP and self.confidence >= config.MIN_CONFIDENCE


@dataclass
class TradeResult:
    """Result of a placed trade."""
    market_slug: str
    direction: str               # "Up" or "Down"
    price: float                 # Price paid per share
    amount_usd: float            # Total USD staked
    shares: int                 # Number of shares
    order_id: str = ""
    fill_status: str = "pending" # "pending", "filled", "partial", "failed"
    timestamp: Optional[datetime] = None
    edge: Optional[EdgeResult] = None
    paper_trade: bool = True     # If True, no real order placed

    def to_dict(self) -> dict:
        return {
            "market_slug": self.market_slug,
            "direction": self.direction,
            "price": self.price,
            "amount_usd": self.amount_usd,
            "shares": self.shares,
            "order_id": self.order_id,
            "fill_status": self.fill_status,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "paper_trade": self.paper_trade,
            "edge_pp": self.edge.edge_pp if self.edge else 0,
            "confidence": self.edge.confidence if self.edge else 0,
        }


@dataclass
class PriceSnapshot:
    """A snapshot of exchange price data."""
    symbol: str          # "btc", "eth", "sol"
    price: float         # Current price in USD
    timestamp: datetime  # When this price was recorded
    volume_1m: float = 0.0   # 1-minute trade volume
    bid: float = 0.0     # Best bid
    ask: float = 0.0     # Best ask
    high_1h: float = 0.0 # 1h high
    low_1h: float = 0.0  # 1h low