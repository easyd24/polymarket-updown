#!/usr/bin/env python3
"""Telegram bot for UpDown notifications and controls."""

import json
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

import config
from models import EdgeResult, Market, TradeResult

import engine as eng_module

# Register the alert callback so engine can send alerts without circular import
eng_module._alert_callback = lambda edge: send_alert_sync(edge)

# ── State (delegated to engine) ─────────────────────────────────────────────────
_running = True
_auto_trade = config.AUTO_TRADE_ENABLED
_paper_trade = config.PAPER_TRADE
_scan_paused = False

# Application instance — set by run_bot()
_app = None


def _fmt_price(price: float) -> str:
    """Format price as cents: 0.48 → '48¢'."""
    return f"{price * 100:.0f}¢"


def _fmt_usd(amount: float) -> str:
    """Format USD amount."""
    return f"${amount:,.2f}"


def _fmt_pct(pct: float) -> str:
    """Format percentage: 0.58 → '58%'."""
    return f"{pct:.0%}"


# ── Command Handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show status and main menu."""
    balance = _get_balance()
    
    stats = eng_module._stats
    text = (
        f"📊 *UpDown Bot*\n"
        f"\n"
        f"{'🟢 Auto-Trade' if _auto_trade else '🔴 Auto-Trade'}: "
        f"{'ON' if _auto_trade else 'OFF'}\n"
        f"{'📝 Paper' if _paper_trade else '💰 Live'} Trading\n"
        f"{'⏸️ Paused' if _scan_paused else '▶️ Scanning'}\n"
        f"\n"
        f"💰 Balance: {_fmt_usd(balance)}\n"
        f"📊 Today: {stats['trades_placed']} trades "
        f"({stats['trades_won']}W/{stats['trades_lost']}L)\n"
        f"💵 P&L: {_fmt_usd(stats['total_pnl'])}\n"
        f"\n"
        f"🔔 Opps found: {stats['opportunities_found']}\n"
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Series", callback_data="menu_series"),
            InlineKeyboardButton("📊 Status", callback_data="menu_status"),
        ],
        [
            InlineKeyboardButton("🤖 Auto-Trade", callback_data="menu_autotrade"),
            InlineKeyboardButton("📜 History", callback_data="menu_history"),
        ],
    ])
    
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show open positions and recent trades."""
    from engine import get_open_positions, get_recent_trades
    
    positions = get_open_positions()
    trades = get_recent_trades(limit=5)
    
    text = "📊 *Open Positions*\n\n"
    
    if not positions:
        text += "No open positions\n"
    else:
        for pos in positions:
            text += (
                f"• {pos['coin'].upper()} {pos['timeframe']} — "
                f"{pos['direction']} @ {_fmt_price(pos['price'])}\n"
                f"  Staked: {_fmt_usd(pos['amount_usd'])} | "
                f"Time left: {pos.get('time_remaining', '?')}\n"
            )
    
    text += f"\n📜 *Recent Trades*\n\n"
    if not trades:
        text += "No trades yet\n"
    else:
        for t in trades:
            pnl_str = f"+{_fmt_usd(t['pnl'])}" if t.get('pnl', 0) >= 0 else _fmt_usd(t.get('pnl', 0))
            text += (
                f"• {t['coin'].upper()} {t['timeframe']} "
                f"{t['direction']} @ {_fmt_price(t['price'])} → "
                f"{t.get('result', 'pending')} {pnl_str}\n"
            )
    
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause/resume scanning."""
    global _scan_paused
    _scan_paused = not _scan_paused
    eng_module._scan_paused = _scan_paused
    status = "PAUSED ⏸️" if _scan_paused else "RESUMED ▶️"
    await update.message.reply_text(f"Scanning {status}")


async def cmd_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "menu_series":
        await _show_series_menu(query)
    elif data == "menu_status":
        await cmd_status(update, context)
    elif data == "menu_autotrade":
        await _show_autotrade_menu(query)
    elif data == "menu_history":
        await _show_history(query)
    elif data.startswith("toggle_"):
        await _toggle_series(query, data)
    elif data.startswith("autotrade_"):
        await _toggle_autotrade(query, data)
    elif data.startswith("buy_"):
        await _handle_buy(query, data)


async def _show_series_menu(query):
    """Show series toggle menu."""
    text = "📊 *Series Settings*\n\n"
    buttons = []
    
    for slug, settings in config.SERIES.items():
        coin_tf = slug.replace("btc-up-or-down-", "BTC ").replace("eth-up-or-down-", "ETH ").replace("-", " ")
        status = "🟢" if settings.get("enabled") else "🔴"
        buttons.append([InlineKeyboardButton(
            f"{status} {coin_tf}",
            callback_data=f"toggle_{slug}"
        )])
    
    buttons.append([InlineKeyboardButton("← Back", callback_data="menu_back")])
    
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )


async def _show_autotrade_menu(query):
    """Show auto-trade toggle menu."""
    text = (
        f"🤖 *Auto-Trade Settings*\n\n"
        f"Auto-Trade: {'ON 🟢' if _auto_trade else 'OFF 🔴'}\n"
        f"Mode: {'Paper 📝' if _paper_trade else 'Live 💰'}\n"
    )
    
    buttons = [
        [InlineKeyboardButton(
            f"Auto-Trade: {'Disable' if _auto_trade else 'Enable'}",
            callback_data=f"autotrade_{'off' if _auto_trade else 'on'}"
        )],
        [InlineKeyboardButton(
            f"Mode: {'Switch to Live' if _paper_trade else 'Switch to Paper'}",
            callback_data=f"autotrade_{'live' if _paper_trade else 'paper'}"
        )],
        [InlineKeyboardButton("← Back", callback_data="menu_back")],
    ]
    
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )


async def _show_history(query):
    """Show trade history."""
    from engine import get_recent_trades
    trades = get_recent_trades(limit=10)
    
    text = "📜 *Trade History*\n\n"
    if not trades:
        text += "No trades yet\n"
    else:
        for t in trades:
            pnl_str = f"+{_fmt_usd(t['pnl'])}" if t.get('pnl', 0) >= 0 else _fmt_usd(t.get('pnl', 0))
            text += (
                f"• {t['coin'].upper()} {t['timeframe']} "
                f"{t['direction']} @ {_fmt_price(t['price'])} → "
                f"{t.get('result', 'pending')} {pnl_str}\n"
            )
    
    await query.edit_message_text(text, parse_mode="Markdown")


async def _toggle_series(query, data):
    """Toggle a series on/off."""
    slug = data.replace("toggle_", "")
    if slug in config.SERIES:
        config.SERIES[slug]["enabled"] = not config.SERIES[slug].get("enabled", False)
        status = "enabled 🟢" if config.SERIES[slug]["enabled"] else "disabled 🔴"
        await query.answer(f"{slug} {status}")
        await _show_series_menu(query)


async def _toggle_autotrade(query, data):
    """Toggle auto-trade on/off or switch paper/live."""
    global _auto_trade, _paper_trade
    
    if data == "autotrade_on":
        _auto_trade = True
        eng_module._auto_trade_enabled = True
    elif data == "autotrade_off":
        _auto_trade = False
        eng_module._auto_trade_enabled = False
    elif data == "autotrade_paper":
        _paper_trade = True
        eng_module._paper_trade = True
    elif data == "autotrade_live":
        _paper_trade = False
        eng_module._paper_trade = False
    
    await _show_autotrade_menu(query)


async def _handle_buy(query, data):
    """Handle Buy button press from an alert — paper trade only for now."""
    import engine as eng
    import market_discovery
    
    parts = data.split("_", 2)  # buy_{slug}_{direction}
    if len(parts) < 3:
        await query.answer("❌ Invalid action", show_alert=True)
        return
    
    slug = parts[1]
    direction = parts[2]  # 'up' or 'down'
    
    await query.answer(f"📝 Paper trade: Buy {direction.title()}...", show_alert=False)
    
    # Find the market
    markets = market_discovery.discover_markets(force_refresh=False)
    market = next((m for m in markets if m.slug == slug), None)
    
    if not market:
        await query.edit_message_text("❌ Market expired — try the next alert.")
        return
    
    price = getattr(market, f"{direction}_price")
    
    # Place paper trade — $5 default
    amount = 5.0
    shares = int(amount / price) if price > 0 else 0
    order_id = f"paper_{slug}_{direction}_{int(time.time())}"
    
    # Record in engine
    eng._trade_history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_slug": slug,
        "coin": market.coin,
        "timeframe": market.timeframe,
        "direction": direction.title(),
        "price": price,
        "amount_usd": amount,
        "shares": shares,
        "order_id": order_id,
        "paper_trade": True,
    })
    eng._open_positions[slug] = {
        "coin": market.coin,
        "timeframe": market.timeframe,
        "direction": direction.title(),
        "price": price,
        "amount_usd": amount,
        "time_remaining": market.time_remaining_str,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    eng._save_trade_history()
    eng._save_positions()
    eng._stats["trades_placed"] = eng._stats.get("trades_placed", 0) + 1
    
    text = (
        f"✅ *Paper Trade Placed*\n\n"
        f"📊 {market.coin.upper()} {market.timeframe}\n"
        f"📈 Buy {direction.title()} @ {_fmt_price(price)}\n"
        f"💰 {_fmt_usd(amount)} → {shares} shares\n"
        f"📝 Order: `{order_id[:30]}…`\n\n"
        f"⏰ Window: {market.time_remaining_str} left"
    )
    await query.edit_message_text(text, parse_mode="Markdown")


def _get_balance():
    """Get USDC balance (cached)."""
    try:
        from trader import get_usdc_balance
        return get_usdc_balance()
    except:
        return 0.0


# ── Alert Functions ───────────────────────────────────────────────────────────

def send_alert_sync(edge: EdgeResult, chat_id: int = None):
    """Send an alert from a background thread — uses direct HTTP to avoid event loop issues."""
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    m = edge.market
    
    # Emoji for direction
    emoji = "📈" if edge.direction == "Up" else "📉"
    
    text = (
        f"{emoji} *EDGE DETECTED*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📊 {m.coin.upper()} {m.timeframe} Up/Down\n"
        f"⏰ Window: {m.time_remaining_str} left\n"
        f"💰 Polymarket: Up {_fmt_price(m.up_price)} | Down {_fmt_price(m.down_price)}\n"
        f"🎯 Our estimate: P(Up) = {_fmt_pct(edge.estimated_true_prob)}\n"
        f"📐 Edge: {edge.edge_pp:+.1f}pp → Buy *{edge.direction}*\n"
        f"📊 Momentum: {edge.momentum_direction} ({edge.momentum_pct:+.2f}%)\n"
        f"🔬 Confidence: {_fmt_pct(edge.confidence)}\n"
    )
    
    # Show Chainlink divergence for 15m/1h markets (they resolve via Chainlink)
    if hasattr(edge, 'chainlink_divergence') and edge.chainlink_divergence is not None:
        div = edge.chainlink_divergence
        if abs(div) > 0.03:
            warn = " ⚠️" if abs(div) > 0.08 else ""
            text += f"🔗 Chainlink vs Binance: {div:+.3f}%{warn}\n"
    
    text += "\n"
    
    if _auto_trade and edge.is_tradeable:
        text += f"🤖 Auto-trading: {'ON (Paper 📝)' if _paper_trade else 'ON (Live 💰)'}\n"
    else:
        text += f"🤖 Auto-trading: OFF\n"
    
    # Build inline keyboard (Buy button when auto-trade is off)
    reply_markup = None
    if not _auto_trade and edge.is_tradeable:
        price_str = _fmt_price(getattr(m, f'{edge.direction.lower()}_price'))
        button = {
            "inline_keyboard": [[
                {
                    "text": f"Buy {edge.direction} @ {price_str}",
                    "callback_data": f"buy_{m.slug}_{edge.direction.lower()}"
                }
            ]]
        }
        reply_markup = json.dumps(button)
    
    # Send via direct HTTP POST (thread-safe, no event loop needed)
    try:
        import httpx
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = httpx.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[telegram_bot] Alert sent: {m.coin.upper()} {m.timeframe} {edge.direction}")
        else:
            print(f"[telegram_bot] Alert HTTP error: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[telegram_bot] Alert send error: {e}")


# ── Bot Runner ────────────────────────────────────────────────────────────────

def run_bot():
    """Start the Telegram bot (blocking)."""
    global _app
    _app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    
    app = _app
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CallbackQueryHandler(cmd_menu_callback))
    
    print("[telegram_bot] Starting UpDown bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()