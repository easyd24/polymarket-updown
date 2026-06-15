# Polymarket Up/Down Crypto Bot

A bot that trades Polymarket's crypto Up/Down markets by detecting when market odds lag real-time exchange prices.

## Quick Start

```bash
# Set up virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure .env (see .env.example)
cp .env.example .env
# Edit .env with your API keys

# Run in paper trading mode (default)
python main.py
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design document.

## Modules

- **config.py** — Configuration constants and series settings
- **models.py** — Data classes (Market, EdgeResult, TradeResult, PriceSnapshot)
- **market_discovery.py** — Discover active Up/Down markets via Gamma Series API
- **price_feed.py** — Binance WebSocket real-time price feed + REST fallback
- **edge_detector.py** — Detect mispricing between exchange prices and Polymarket odds
- **trader.py** — CLOB order placement (limit orders to avoid 7% taker fee)
- **telegram_bot.py** — Telegram alerts and controls (/start, /status, /pause)
- **engine.py** — Main orchestration loop

## Strategy

**Primary Edge: Momentum Mispricing**
When BTC/ETH is trending strongly on Binance but Polymarket hasn't repriced yet:
1. Get live price from Binance WebSocket
2. Get Polymarket odds from CLOB API
3. Estimate true P(Up) using momentum + volatility
4. If edge > 8pp and confidence > 85%, place trade

**Key insight:** The 7% taker fee means we MUST use limit orders (maker side, 0% fee).

## Phases

1. **Phase 1 (current)** — Paper trading on Pi, alerts only
2. **Phase 2** — AWS us-east-1 deployment, BTC 15m + 1h live
3. **Phase 3** — Scale to 5m markets, add ETH
4. **Phase 4** — Full automation with SOL/XRP/DOGE