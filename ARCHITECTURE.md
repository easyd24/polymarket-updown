# Polymarket Up/Down Crypto Bot — Architecture

> **Separate repo.** No code changes to the existing weather/polymarket bot at `/home/openclaw/polymarket-bot/`.

---

## 1. Overview

A bot that trades Polymarket's crypto Up/Down markets by detecting when the market odds lag real-time exchange prices. The edge is a **latency/mispricing gap** — when BTC is surging on Binance but Polymarket's "Up" is still at 48¢, we buy Up.

### Market Structure
| Timeframe | BTC 24h Vol | ETH 24h Vol | Resolution Source |
|-----------|-------------|-------------|-------------------|
| 5m | $17M | $744K | Chainlink BTC/USD stream |
| 15m | $2.5M | $399K | Chainlink BTC/USD stream |
| 1h | $855K | $233K | Binance 1H candle |
| 4h | $149K | $42K | Chainlink BTC/USD stream |
| Daily | $335K | $83K | Binance daily candle |

Also available: SOL, XRP, DOGE, BNB (lower volume).

### How Resolution Works
- **5m/15m/4h (Chainlink)**: Price at window end ≥ Price at window start → "Up" wins. The "Price to Beat" is the Chainlink price at `eventStartTime`.
- **1h/Daily (Binance)**: Candle close ≥ Candle open → "Up" wins.

---

## 2. Edge Detection Strategy

### Primary Edge: Momentum Mispricing
When a crypto asset is trending strongly in one direction and Polymarket hasn't repriced yet:

1. **Get live exchange price** from Binance WebSocket (BTC/USDT, ETH/USDT, SOL/USDT)
2. **Get Polymarket odds** from CLOB REST API (midpoint of Up/Down order book)
3. **Compute implied probability** from odds (Up price ≈ P(Up))
4. **Estimate true probability** using momentum signals:
   - Short-term trend (5m, 15m, 1h returns)
   - Order flow imbalance (Binance bid/ask volume ratio)
   - Volatility-adjusted drift
5. **Edge = estimated_true_prob - market_price**
6. **If edge > threshold (e.g., 5pp), place trade**

### Secondary Edge: Volatility Skew
Higher volatility → more likely to deviate from 50/50. When implied vol is low but realized vol is high:
- Calculate realized vol from recent price data
- If realized vol > implied vol (from option-like pricing), directional bets have positive EV

### Tertiary Edge: Mean Reversion
For 4h/daily markets, extreme moves tend to revert:
- If Up is at 70¢+ but BTC has already moved 2%+ in the window → buy Down (mean reversion)
- Requires careful calibration — only trade when the move is statistically extreme

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        UPDOWN BOT                            │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ Binance   │  │ Gamma API│  │ CLOB API │  │ Chainlink │   │
│  │ WebSocket │  │ Scanner   │  │ Trader   │  │ Monitor  │   │
│  │ (Prices)  │  │ (Markets) │  │ (Orders) │  │ (Prices) │   │
│  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘  └─────┬────┘   │
│        │               │              │              │        │
│        └───────────┬───┴──────────────┴──────────────┘        │
│                    │                                           │
│              ┌─────▼─────┐                                    │
│              │   Engine   │                                    │
│              │ (Edge      │                                    │
│              │  Detector) │                                    │
│              └─────┬─────┘                                    │
│                    │                                           │
│        ┌───────────┼───────────┐                              │
│        │           │           │                               │
│  ┌─────▼────┐ ┌───▼────┐ ┌───▼────┐                        │
│  │ Telegram  │ │ Logger │ │ Config │                        │
│  │ Bot       │ │        │ │        │                        │
│  └───────────┘ └────────┘ └────────┘                        │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. Module Design

### 4.1 `config.py` — Configuration
```python
# Series to trade (enable/disable per series)
SERIES = {
    "btc-up-or-down-5m":  {"enabled": True,  "max_stake_pct": 2},
    "btc-up-or-down-15m": {"enabled": True,  "max_stake_pct": 3},
    "btc-up-or-down-hourly": {"enabled": True, "max_stake_pct": 5},
    "btc-up-or-down-4h":  {"enabled": True,  "max_stake_pct": 5},
    "btc-up-or-down-daily": {"enabled": False, "max_stake_pct": 5},
    "eth-up-or-down-5m":  {"enabled": True,  "max_stake_pct": 2},
    "eth-up-or-down-15m": {"enabled": True,  "max_stake_pct": 3},
    "eth-up-or-down-hourly": {"enabled": True, "max_stake_pct": 5},
    # SOL, XRP, DOGE, BNB can be added later
}

# Edge thresholds
MIN_EDGE_PP = 5          # Minimum edge in percentage points
MIN_CONFIDENCE = 0.85    # Minimum confidence in edge estimate
MAX_POSITION_PCT = 5     # Max % of bankroll per trade
MIN_ORDER_SIZE = 5       # Polymarket minimum order ($5)

# Momentum parameters
MOMENTUM_LOOKBACK = 300   # Seconds of lookback for momentum calc
MOMENTUM_DECAY = 0.95    # Exponential decay for older data

# Binance WebSocket
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"

# Polymarket API
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Scan intervals (seconds)
MARKET_SCAN_INTERVAL = 60     # How often to discover new markets
EDGE_SCAN_INTERVAL = 5         # How often to check for edge (5m markets)
EDGE_SCAN_INTERVAL_SLOW = 30   # For 15m+ markets

# Risk management
MAX_TRADES_PER_HOUR = 6        # Rate limit trades
MAX_DAILY_LOSS_PCT = 10       # Stop trading if down 10% in a day
COOLDOWN_AFTER_LOSS = 300     # Seconds to wait after a loss

# Telegram
TELEGRAM_BOT_TOKEN = ""       # New bot token (separate from weather bot)
TELEGRAM_CHAT_ID = 6104346726 # Hamez's chat ID
```

### 4.2 `market_discovery.py` — Find Active Markets
```python
"""Discover active Up/Down markets using the Gamma Series API."""

SERIES_SLUGS = [
    "btc-up-or-down-5m", "btc-up-or-down-15m", "btc-up-or-down-hourly",
    "btc-up-or-down-4h", "btc-up-or-down-daily",
    "eth-up-or-down-5m", "eth-up-or-down-15m", "eth-up-or-down-hourly",
    "eth-up-or-down-4h", "eth-up-or-down-daily",
    # SOL, XRP, DOGE, BNB
]

async def get_active_markets(series_slug: str) -> list[Market]:
    """Get all active markets for a series."""
    # GET /events?series_slug={slug}&limit=100&active=true&closed=false
    # Parse eventStartTime, endDate, outcomes, clobTokenIds, conditionId
    # Return list of Market objects

async def get_market_detail(slug: str) -> Market:
    """Get full market details including order book."""
    # GET /markets?slug={slug}&limit=1
    # GET /book?token_id={up_token_id}  (order book)
    # Return Market with current prices and liquidity

def parse_timeframe(tags: list[str]) -> str:
    """Extract timeframe from CLOB tags: '5M', '15M', '1H', '4H', 'Daily'."""
    
def parse_coin(slug: str) -> str:
    """Extract coin from series slug: 'btc', 'eth', 'sol', etc."""
```

### 4.3 `price_feed.py` — Real-Time Exchange Prices
```python
"""Binance WebSocket price feed with REST fallback."""

import asyncio
import websockets
import json

SYMBOLS = {
    "btc": "btcusdt",
    "eth": "ethusdt",
    "sol": "solusdt",
    "xrp": "xrpusdt",
}

class BinancePriceFeed:
    """Maintains real-time prices via Binance WebSocket."""
    
    async def connect(self):
        """Connect to combined stream: wss://stream.binance.com:9443/ws/btcusdt@trade"""
        # Subscribe to @trade streams for each symbol
        # On each trade, update self.prices[symbol] = price
        
    async def get_price(self, symbol: str) -> float:
        """Get latest price for a symbol."""
        return self.prices.get(symbol)
    
    async def get_momentum(self, symbol: str, lookback: int = 300) -> dict:
        """Calculate momentum from recent trade history.
        
        Returns:
            - trend: float (avg price change per second)
            - strength: float (0-1, how consistent the trend is)
            - volatility: float (std of returns over lookback)
            - direction: "up" | "down" | "flat"
        """
        # Use stored trade history to compute returns
        # EWM trend, directional strength, realized vol
        
    async def close(self):
        """Close WebSocket connections."""
```

### 4.4 `edge_detector.py` — Find Mispriced Markets
```python
"""Detect when Polymarket odds lag real exchange prices."""

class EdgeDetector:
    
    def calculate_edge(self, market: Market, exchange_data: dict) -> EdgeResult:
        """Calculate edge for a single Up/Down market.
        
        Args:
            market: Polymarket market data (Up price, Down price, timeframe)
            exchange_data: Binance price feed data (current price, momentum, vol)
            
        Returns:
            EdgeResult with:
                - direction: "Up" or "Down"
                - edge_pp: edge in percentage points
                - confidence: 0-1 confidence in the edge
                - estimated_true_prob: our estimate of P(Up)
                - market_implied_prob: Polymarket's implied P(Up)
                - reasoning: human-readable explanation
        """
        # 1. Get current exchange price
        # 2. Get "price to beat" (price at window start from Chainlink/Binance)
        # 3. Calculate price change since window start
        # 4. Estimate P(Up) based on:
        #    a. Current trend direction and strength
        #    b. Time remaining in window
        #    c. Volatility regime
        #    d. Mean reversion signal (for longer windows)
        # 5. Compare to market price
        # 6. Return edge calculation
    
    def estimate_directional_prob(self, momentum: dict, time_remaining_pct: float, 
                                   vol: float) -> float:
        """Estimate P(Up) given current market conditions.
        
        Key insight: For short windows (5m, 15m), momentum is king.
        For longer windows (1h, 4h), mean reversion matters more.
        
        Model:
        - Base rate: 50% (no information)
        - Momentum adjustment: ±5-15pp based on trend strength
        - Volatility adjustment: higher vol → more extreme probabilities
        - Time decay: edge shrinks as window nears end (uncertainty reduces)
        - Mean reversion: for windows > 1h, large moves tend to revert
        """
```

### 4.5 `trader.py` — Order Placement
```python
"""Place orders on Polymarket CLOB. Reuses patterns from existing bot."""

from py_clob_client_v2 import ClobClient, SignatureTypeV2, ApiCreds

# Same wallet/auth as existing bot — shared deposit wallet
# (or separate wallet if we want isolation)

class UpDownTrader:
    def __init__(self, config):
        self.client = ClobClient(
            'https://clob.polymarket.com',
            key=config.POLYMARKET_PRIVATE_KEY,
            chain_id=137,
            creds=ApiCreds(...),
            signature_type=SignatureTypeV2.POLY_1271,
            funder=config.DEPOSIT_WALLET,
        )
    
    async def place_trade(self, market: Market, direction: str, 
                          edge: EdgeResult, bankroll: float) -> TradeResult:
        """Place a trade on an Up/Down market.
        
        Steps:
        1. Calculate position size (Kelly-based, with edge confidence modifier)
        2. Check bankroll and max position limits
        3. Get current order book (best bid/ask)
        4. Place limit order at best available price
        5. Wait for fill or timeout
        6. Return trade result
        
        Risk controls:
        - Max position: 5% of bankroll
        - Min edge: 5pp
        - Min confidence: 0.85
        - Skip if spread > 5¢ (illiquid)
        - Skip if volume < $500 (thin market)
        """
    
    async def close_position(self, trade: TradeResult, current_price: float):
        """Sell position before window ends if edge reverses.
        
        For 5m/15m markets, positions resolve quickly so early close
        is only useful if the trend reverses significantly.
        For 1h/4h markets, closing early can lock in profits.
        """
```

### 4.6 `engine.py` — Main Loop
```python
"""Main orchestration loop — discovers markets, detects edges, places trades."""

class UpDownEngine:
    def __init__(self, config):
        self.price_feed = BinancePriceFeed()
        self.market_scanner = MarketDiscovery()
        self.edge_detector = EdgeDetector()
        self.trader = UpDownTrader(config)
        self.notifier = TelegramNotifier(config)
    
    async def run(self):
        """Main loop — runs forever."""
        await self.price_feed.connect()
        
        while True:
            try:
                # 1. Discover active markets
                markets = await self.discover_markets()
                
                # 2. For each active market, check for edge
                for market in markets:
                    edge = self.edge_detector.calculate_edge(market, self.price_feed)
                    
                    if edge.edge_pp >= MIN_EDGE_PP and edge.confidence >= MIN_CONFIDENCE:
                        # 3. Place trade
                        trade = await self.trader.place_trade(market, edge)
                        
                        # 4. Notify via Telegram
                        await self.notifier.send_trade_alert(market, edge, trade)
                
                # 5. Monitor open positions for early close
                await self.monitor_positions()
                
                # 6. Sleep based on timeframe
                await asyncio.sleep(self.get_scan_interval())
                
            except Exception as e:
                await self.notifier.send_error(str(e))
                await asyncio.sleep(10)
    
    async def discover_markets(self) -> list[Market]:
        """Refresh market list periodically."""
        # Cache markets, refresh every MARKET_SCAN_INTERVAL
        # Only return markets that are currently within their trading window
        
    async def monitor_positions(self):
        """Check open positions for early close opportunities."""
        # If the edge has reversed significantly, close the position
        # Lock in profits rather than letting it ride to resolution
```

### 4.7 `telegram_bot.py` — Notifications & Control
```python
"""Telegram bot for trade alerts and manual control."""

# Separate bot token from weather/polymarket bot
# Commands:
#   /start — Show status
#   /menu — Toggle settings (same UX as existing bot)
#   /status — Show open positions, P&L, win rate
#   /pause — Pause trading (keep monitoring)
#   /resume — Resume trading
#   /history — Show recent trade history

# Inline buttons for:
#   - Enable/disable per series (BTC 5m, BTC 15m, etc.)
#   - Toggle auto-trade vs alerts-only
#   - Adjust max position size
#   - Adjust min edge threshold
```

---

## 5. Data Flow

### Market Discovery Flow
```
Every 60s:
  Gamma API → GET /series?slug=btc-up-or-down-5m → series metadata
  Gamma API → GET /events?series_slug=btc-up-or-down-5m&active=true → active markets
  For each market:
    Parse: conditionId, clobTokenIds, outcomes, eventStartTime, endDate
    CLOB API → GET /book?token_id={up_token_id} → order book
    Calculate: time_remaining = endDate - now
    Filter: only markets with > 60s remaining and > $500 liquidity
```

### Edge Detection Flow (every 5s for 5m markets)
```
For each active market:
  1. Binance WS → current_price (BTC: $65,362)
  2. Binance WS → momentum (trend: +0.02%/s, strength: 0.7)
  3. Binance WS → volatility (1m realized vol: 0.15%)
  4. CLOB API → market_up_price (0.48)
  5. Calculate:
     - price_change_pct = (current - price_to_beat) / price_to_beat * 100
     - trend_direction = "up" if momentum > 0 else "down"
     - P(Up) = base_rate + momentum_adj + vol_adj + time_decay
     - Example: P(Up) = 0.50 + 0.08 (strong uptrend) = 0.58
     - Edge = 0.58 - 0.48 = 10pp ✓ (above 5pp threshold)
  6. Decision: BUY UP at 48¢ with estimated true probability 58%
```

### Trade Execution Flow
```
Edge detected → Kelly sizing:
  kelly_fraction = (p * b - q) / b  where b = 1/price - 1, p = true_prob, q = 1-p
  position_size = min(kelly_fraction * bankroll, MAX_POSITION_PCT * bankroll)
  
  Example: p=0.58, price=0.48, b=1.083
  kelly = (0.58 * 1.083 - 0.42) / 1.083 = 0.193 (19.3%)
  With $200 bankroll: position = min(0.193 * 200, 0.05 * 200) = $10
  
Order placement:
  GET /book?token_id={up_token_id} → best_ask = $0.49
  Place limit order at $0.49 for $10 (≈ 20 shares)
  Wait up to 30s for fill
  If not filled, cancel and retry at new best_ask
```

---

## 6. Key Technical Details

### Fee Structure
- **Taker fee**: 7% (0.07) — this is HIGH. We pay this on every buy.
- **Maker fee**: 0% (with 0.2% rebate from taker fees)
- **Minimum order**: $5
- **Tick size**: $0.01

**Critical implication**: The 7% taker fee means we need **≥7pp edge just to break even**. Our 5pp minimum edge threshold should actually be **8-10pp** to account for fees. Alternatively, we should place **limit orders** (maker side) whenever possible to avoid the taker fee entirely.

### Latency Requirements
| Timeframe | Acceptable Latency | Infrastructure |
|-----------|-------------------|----------------|
| 5m | <1s | AWS us-east-1, co-located |
| 15m | <5s | AWS us-east-1 |
| 1h | <30s | Any cloud, Pi OK |
| 4h | <60s | Any cloud, Pi OK |
| Daily | <5min | Pi OK |

### Binance WebSocket Streams
```python
# Combined stream URL
wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade/solusdt@trade

# Trade event format
{
  "e": "trade",
  "E": 1781484300000,  # event time
  "s": "BTCUSDT",
  "t": 1781484300,      # trade ID
  "p": "65362.24",      # price
  "q": "0.001",         # quantity
  "b": 12345,           # buyer order ID
  "a": 67890,           # seller order ID
  "T": 1781484300000,   # trade time
  "m": true             # is buyer market maker?
}

# Book ticker (for bid/ask spread)
wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker

# Kline/candlestick (for momentum calculation)
wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m
```

### Polymarket CLOB Endpoints
```python
# Order book
GET https://clob.polymarket.com/book?token_id={token_id}

# Midpoint price
GET https://clob.polymarket.com/midpoint?token_id={token_id}

# Last trade price
GET https://clob.polymarket.com/last-trade-price?token_id={token_id}

# Place order (via py_clob_client_v2)
client.create_and_post_order(OrderArgsV2(...))

# Cancel order
client.cancel_order(order_id)

# Get open orders
client.get_orders()
```

### Price to Beat Calculation
```python
def get_price_to_beat(market: Market, feed: BinancePriceFeed) -> float:
    """Get the reference price at the start of the trading window.
    
    For Chainlink markets (5m, 15m, 4h):
        - The price at eventStartTime according to Chainlink BTC/USD data stream
        - In practice, Binance price at that timestamp is a close proxy
        
    For Binance candle markets (1h, daily):
        - The open price of the candle starting at eventStartTime
        - Exactly what Binance reports as candle open
    """
    if market.resolution_source == "chainlink":
        # Use the price we captured at eventStartTime
        return feed.price_at(market.coin, market.event_start_time)
    else:
        # Use Binance candle open price
        return feed.candle_open(market.coin, market.event_start_time)
```

---

## 7. Risk Management

### Position Sizing
- **Kelly criterion** with 50% Kelly fraction (half-Kelly for safety)
- **Bankroll tracking**: total USDC balance + unrealized P&L
- **Max position**: 5% of bankroll per trade (adjustable)
- **Min order**: $5 (Polymarket requirement)

### Stop-Loss Rules
1. **Daily loss limit**: Stop trading if down 10% of bankroll in a day
2. **Cooldown after loss**: 5-minute pause after any losing trade
3. **Max trades per hour**: 6 (prevents runaway trading)
4. **Edge reversal**: Close position if edge reverses by >50% (e.g., bought Up at 48¢ with 58% true prob, now true prob is <50%)

### Time-Based Rules
- **5m markets**: Only trade in first 3 minutes of the 5-minute window (edge decays rapidly)
- **15m markets**: Only trade in first 10 minutes
- **1h markets**: Trade in first 30 minutes
- **4h markets**: Trade in first 2 hours
- **Daily markets**: Trade in first 12 hours

### Market Quality Filters
- **Skip if liquidity < $500**: Thin order books mean bad fills
- **Skip if spread > 5¢**: Market maker isn't present, prices unreliable
- **Skip if volume_24h < $1000**: Not enough market interest
- **Skip if Polymarket price is 90¢+ or 10¢-**: Edge is too small at extremes

---

## 8. Project Structure

```
polymarket-updown/
├── ARCHITECTURE.md          # This document
├── README.md                # Setup and usage guide
├── .env                     # API keys (Polymarket, Binance, Telegram)
├── config.py                # All configuration constants
├── main.py                  # Entry point — starts engine
├── market_discovery.py      # Series API → find active markets
├── price_feed.py            # Binance WebSocket price feed
├── edge_detector.py         # Edge calculation logic
├── trader.py                # CLOB order placement
├── engine.py                # Main orchestration loop
├── telegram_bot.py          # Telegram notifications & control
├── risk_manager.py          # Position sizing, stop-losses
├── trade_logger.py          # Trade history logging (JSON)
├── models.py                # Data classes (Market, EdgeResult, TradeResult)
├── requirements.txt         # Python dependencies
├── data/
│   └── trade_history.json   # Persistent trade log
├── tests/
│   ├── test_edge_detector.py
│   ├── test_price_feed.py
│   ├── test_market_discovery.py
│   └── test_risk_manager.py
└── systemd/
    └── polymarket-updown.service
```

---

## 9. Deployment Plan

### Phase 1: Paper Trading (Pi, local)
- Run on the Pi with alerts-only mode (no real trades)
- Validate edge detection on 15m, 1h, and 4h markets
- Track hypothetical P&L for 1-2 weeks
- **Goal**: Prove the edge is real before risking capital

### Phase 2: Live Trading — Conservative (AWS us-east-1)
- Deploy on AWS t3.micro ($10/mo)
- Start with BTC 15m and 1h markets only
- $200 starting capital (separate from weather bot wallet)
- Max $5 per trade, max 3% of bankroll
- **Goal**: Validate profitability with real money

### Phase 3: Scale Up (AWS us-east-1)
- Add BTC 5m markets (requires low latency)
- Add ETH markets (lower volume but more mispricing)
- Increase position sizes based on P&L
- Add auto-sell (close position early when edge reverses)
- **Goal**: Scale what works

### Phase 4: Full Automation
- Add SOL, XRP markets
- Multi-timeframe hedging (e.g., buy Up on 15m, hedge with Down on 5m)
- Parity smart money integration (from existing bot)
- **Goal**: Maximum extraction

---

## 10. Telegram Bot — Menu Design

```
┌─────────────────────────────────┐
│  📊 UpDown Bot                  │
│                                 │
│  🔴 Auto-Trade: OFF             │
│  🟢 Alerts: ON                  │
│                                 │
│  💰 Bankroll: $200.00           │
│  📈 Today: +$4.20 (+2.1%)      │
│  📊 Win Rate: 62% (13/21)      │
│                                 │
│  ┌──────────┐ ┌──────────┐     │
│  │ ⚙️ Series │ │ 📊 Status │     │
│  └──────────┘ └──────────┘     │
│  ┌──────────┐ ┌──────────┐     │
│  │ 🎚️ Tiers │ │ 📜 History│     │
│  └──────────┘ └──────────┘     │
└─────────────────────────────────┘

Series Menu:
┌─────────────────────────────────┐
│  📊 Series Settings              │
│                                 │
│  BTC 5m:  🔴 OFF  │ Alerts: ON   │
│  BTC 15m: 🟢 ON   │ Alerts: ON   │
│  BTC 1h:  🟢 ON   │ Alerts: ON   │
│  BTC 4h:  🟢 ON   │ Alerts: ON   │
│  BTC Daily: 🔴 OFF              │
│  ETH 15m: 🟢 ON   │ Alerts: ON   │
│  ETH 1h:  🟢 ON   │ Alerts: ON   │
│                                 │
│  ← Back to Menu                 │
└─────────────────────────────────┘

Status:
┌─────────────────────────────────┐
│  📊 Open Positions               │
│                                 │
│  BTC 15m Up @ 47¢               │
│  Entry: $5.00 | Current: $5.40  │
│  +8.0% (+$0.40)                │
│  Time left: 8m 32s             │
│                                 │
│  ETH 1h Down @ 52¢              │
│  Entry: $5.00 | Current: $4.60  │
│  -8.0% (-$0.40)                │
│  Time left: 42m 15s            │
│                                 │
│  ← Back to Menu                 │
└─────────────────────────────────┘
```

---

## 11. Monitoring & Alerting

### Trade Alerts (Telegram)
```
🔥 EDGE DETECTED
━━━━━━━━━━━━━━━━
📊 BTC 15m Up/Down
⏰ Window: 5:45-6:00PM ET (12m left)
📈 Trend: BTC +0.3% in 5m (strong uptrend)
💰 Polymarket: Up 47¢ | Down 53¢
🎯 Our estimate: P(Up) = 58%
📐 Edge: +11pp ✓

🤖 Auto-trading: OFF
👆 /buy_up to place trade
```

### Trade Confirmation (when auto-trade is on)
```
✅ TRADE PLACED
━━━━━━━━━━━━━━━━
📊 BTC 15m — BUY UP
💵 Price: 47¢ × 10 shares = $4.70
🎯 True prob: 58% (edge: +11pp)
⏰ Window closes: 6:00PM ET
📈 P&L if Up wins: +$5.30 (+113%)
📉 P&L if Down wins: -$4.70 (-100%)
```

### Resolution Alerts
```
🏆 TRADE RESOLVED — WIN
━━━━━━━━━━━━━━━━
📊 BTC 15m — UP ✓
💰 Profit: +$5.30
📈 Running total: +$14.20 (6/9 trades)
```

---

## 12. Critical Considerations

### The 7% Taker Fee Problem
Polymarket charges 7% on taker orders. This means:
- Buying Up at 48¢ costs 48¢ + 3.4¢ (7%) = 51.4¢ effective
- To break even, our true probability must be ≥ 51.4%
- **Strategy: Always use limit orders (maker side)** to avoid the taker fee
- This means we place orders at the current bid/ask and wait for fills
- Risk: our order may not get filled before the edge disappears

### Wallet Separation
- **Option A**: Share the same Polymarket wallet as the weather bot
  - Simpler, no new wallet setup
  - Risk: both bots draw from same bankroll, could conflict
- **Option B**: Separate Polymarket wallet
  - Cleaner isolation between strategies
  - Requires separate deposit wallet and API credentials
  - **Recommended** for proper P&L tracking

### Paper Trading First
- The edge detection model needs validation before real money
- Run for 1-2 weeks in alerts-only mode
- Track hypothetical trades and compare to actual results
- Only go live after proving consistent positive EV

### Backtesting
- Polymarket has historical data via the Gamma API (closed markets)
- We can backtest our edge detection model against resolved Up/Down markets
- Key metrics: win rate, average edge, P&L by timeframe and coin
- This should be the FIRST step before any paper trading

---

## 13. Comparison: Weather Bot vs UpDown Bot

| Aspect | Weather Bot | UpDown Bot |
|--------|-------------|------------|
| Market type | Yes/No (weather) | Up/Down (crypto) |
| Time horizon | 12-24 hours | 5 min to 1 day |
| Edge source | Forecast vs market | Exchange vs market latency |
| Scan frequency | Every 15 min | Every 5-30 seconds |
| Speed requirement | Low (Pi OK) | High (5m needs <1s) |
| Resolution | Manual (weather data) | Automatic (Chainlink/Binance) |
| Risk per trade | $5-15 | $5-10 |
| Expected win rate | 85-90% | 55-65% |
| Avg profit per trade | $2-3 | $0.50-2 |
| Fee impact | 7% taker (but we use limit orders) | 7% taker (MUST use limit orders) |
| Infrastructure | Pi (UK) | AWS us-east-1 (recommended) |

The weather bot has higher win rate and larger edge per trade but fewer opportunities. The UpDown bot has lower win rate but many more trades per day (potentially 50-100+ on 5m markets).