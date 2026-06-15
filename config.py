#!/usr/bin/env python3
"""Configuration for the Polymarket Up/Down crypto bot."""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env')

# ── Polymarket API ────────────────────────────────────────────────────────────
POLYMARKET_PRIVATE_KEY = os.environ.get('POLYMARKET_PRIVATE_KEY', '')
DEPOSIT_WALLET = os.environ.get('DEPOSIT_WALLET', '')
EOA_WALLET = os.environ.get('EOA_WALLET', '')
POLYMARKET_API_KEY = os.environ.get('POLYMARKET_API_KEY', '')
POLYMARKET_API_SECRET = os.environ.get('POLYMARKET_API_SECRET', '')
POLYMARKET_API_PASSPHRASE = os.environ.get('POLYMARKET_API_PASSPHRASE', '')

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = int(os.environ.get('TELEGRAM_CHAT_ID', '6104346726'))

# ── Binance ───────────────────────────────────────────────────────────────────
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"
BINANCE_REST_URL = "https://api.binance.com/api/v3"

# ── Polymarket APIs ───────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ── Series Configuration ─────────────────────────────────────────────────────
# Each series has: enabled, max_stake_pct, min_liquidity, scan_interval
SERIES = {
    "btc-up-or-down-5m":     {"enabled": False, "max_stake_pct": 2,  "min_liquidity": 500,  "scan_interval": 5},
    "btc-up-or-down-15m":    {"enabled": True,  "max_stake_pct": 3,  "min_liquidity": 500,  "scan_interval": 15},
    "btc-up-or-down-hourly": {"enabled": True,  "max_stake_pct": 5,  "min_liquidity": 200,  "scan_interval": 60},
    "btc-up-or-down-4h":     {"enabled": True,  "max_stake_pct": 5,  "min_liquidity": 100,  "scan_interval": 120},
    "btc-up-or-down-daily":  {"enabled": False, "max_stake_pct": 5,  "min_liquidity": 100,  "scan_interval": 300},
    "eth-up-or-down-5m":     {"enabled": False, "max_stake_pct": 2,  "min_liquidity": 300,  "scan_interval": 5},
    "eth-up-or-down-15m":    {"enabled": True,  "max_stake_pct": 3,  "min_liquidity": 300,  "scan_interval": 15},
    "eth-up-or-down-hourly": {"enabled": True,  "max_stake_pct": 5,  "min_liquidity": 100,  "scan_interval": 60},
    "eth-up-or-down-4h":     {"enabled": False, "max_stake_pct": 5,  "min_liquidity": 50,   "scan_interval": 120},
    "eth-up-or-down-daily":  {"enabled": False, "max_stake_pct": 5,  "min_liquidity": 50,   "scan_interval": 300},
}

# ── Edge Detection Parameters ─────────────────────────────────────────────────
MIN_EDGE_PP = 10             # Minimum edge in percentage points (need 10pp to be worth it)
MIN_CONFIDENCE = 0.55         # Minimum confidence in edge estimate (was 0.85, now realistic)
MOMENTUM_LOOKBACK = 300      # Seconds of lookback for momentum calc
MOMENTUM_DECAY = 0.95        # Exponential decay for older price data
MEAN_REVERSION_THRESHOLD = 0.02  # 2% move triggers mean reversion check (4h/daily)

# ── Risk Management ──────────────────────────────────────────────────────────
MAX_POSITION_PCT = 5         # Max % of bankroll per trade
MIN_ORDER_SIZE = 5           # Polymarket minimum order ($5)
MAX_TRADES_PER_HOUR = 6      # Rate limit
MAX_DAILY_LOSS_PCT = 10      # Stop trading if down 10% in a day
COOLDOWN_AFTER_LOSS = 300    # Seconds to wait after a loss
MAX_SPREAD_CENTS = 5         # Skip if spread > 5¢ (illiquid)
MIN_VOLUME_24H = 1000        # Skip if volume < $1k (thin market)
EXTREME_PRICE_MIN = 0.10     # Skip if price < 10¢
EXTREME_PRICE_MAX = 0.90     # Skip if price > 90¢ (edge too small)

# ── Timing ────────────────────────────────────────────────────────────────────
# Only trade in first X% of each window (edge decays as window closes)
TRADE_WINDOW_PCT = {
    "5m": 0.60,    # Only first 3 min
    "15m": 0.67,   # Only first 10 min
    "1h": 0.50,    # Only first 30 min
    "4h": 0.50,    # Only first 2 hours
    "daily": 0.50, # Only first 12 hours
}

# ── Data Paths ────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / 'data'
TRADE_HISTORY_FILE = DATA_DIR / 'trade_history.json'
POSITIONS_FILE = DATA_DIR / 'positions.json'

# ── Auto-Trade ────────────────────────────────────────────────────────────────
AUTO_TRADE_ENABLED = False    # Default: alerts only, no auto-trading
PAPER_TRADE = True           # Default: paper trading (no real orders)

# ── Market Scan ────────────────────────────────────────────────────────────────
MARKET_SCAN_INTERVAL = 60    # How often to discover new markets (seconds)