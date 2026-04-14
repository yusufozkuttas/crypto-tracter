"""
orchestrator.py
===============
Main entry point for the trading signal bot.

On startup
----------
  1. Loads config from .env
  2. Restores cooldown state from disk (prevents duplicate trades after restart)
  3. Starts the 5-minute polling loop
  (Startup backtest is disabled by default — set RUN_STARTUP_BACKTEST=True to enable)

Each cycle (every 5 minutes)
-----------------------------
  For each symbol:
    1. Fetch 200 x 1h candles  (binance_feed)
    2. Run all technical signals (signal_agent)
    3. Fetch market context     (coinglass_feed: funding, OI, L/S ratio)
    4. If signal is STRONG BUY or STRONG SELL:
         → run risk_agent to get trade plan
         → if verdict is TAKE_TRADE and cooldown has passed:
              → send Telegram alert

Telegram alert format
---------------------
  🟢 STRONG BUY — BTCUSDT
  Entry:     $72,932
  Stop loss: $70,648  (-3.1%)
  TP1:       $77,500  (+2R)
  TP2:       $79,783  (+3R)
  RSI: 57 | MACD: bullish | Sweep: YES
  Funding: -0.007% | L/S: 42/58

Config (.env)
-------------
  TELEGRAM_BOT_TOKEN   — bot token from BotFather
  TELEGRAM_CHAT_ID     — destination chat/channel ID
  SYMBOLS              — comma-separated, e.g. BTCUSDT,ETHUSDT,IOTXUSDT
  ACCOUNT_BALANCE      — paper account size in USD (default 1000)
  RISK_PCT             — % of account to risk per trade (default 1.0)
  POLL_INTERVAL        — seconds between cycles (default 300)
  ALERT_COOLDOWN       — seconds before re-alerting same coin (default 600)
  BACKTEST_SYMBOL      — symbol to backtest on startup (default first in SYMBOLS)
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# .env loader (stdlib — no external dependencies)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env"):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_raw_symbols = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,IOTXUSDT")
SYMBOLS = [s.strip().upper() for s in _raw_symbols.split(",") if s.strip()]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()
TELEGRAM_URL       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

ACCOUNT_BALANCE  = float(os.environ.get("ACCOUNT_BALANCE",  "1000"))
RISK_PCT         = float(os.environ.get("RISK_PCT",         "1.0"))
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL",     "300"))   # seconds
ALERT_COOLDOWN   = int(os.environ.get("ALERT_COOLDOWN",    "600"))   # seconds
BACKTEST_SYMBOL  = os.environ.get("BACKTEST_SYMBOL", SYMBOLS[0])

PAPER_TRADE           = os.environ.get("PAPER_TRADE", "true").lower() in ("1", "true", "yes")
PAPER_TRADES_FILE     = "paper_trades.json"
COOLDOWN_STATE_FILE   = "cooldown_state.json"
BALANCE_STATE_FILE    = "balance_state.json"
RUN_STARTUP_BACKTEST  = False  # 500-candle startup backtest is too few to be useful

# Signals that trigger a risk calculation
ACTIONABLE_SIGNALS = {"STRONG BUY", "STRONG SELL"}

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_last_alert: dict[str, float] = {}   # symbol → last alert unix timestamp
_balance: list = [ACCOUNT_BALANCE]   # mutable — updated as trades close


def _load_balance_state():
    """Load persisted balance from disk."""
    if not os.path.isfile(BALANCE_STATE_FILE):
        return
    try:
        with open(BALANCE_STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        _balance[0] = float(data.get("balance", ACCOUNT_BALANCE))
    except Exception:
        pass


def _save_balance_state():
    """Persist current balance to disk."""
    with open(BALANCE_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"balance": round(_balance[0], 4)}, fh)


def _load_cooldown_state():
    """Load persisted cooldown timestamps from disk into _last_alert."""
    if not os.path.isfile(COOLDOWN_STATE_FILE):
        return
    try:
        with open(COOLDOWN_STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        _last_alert.update({k: float(v) for k, v in data.items()})
    except Exception:
        pass  # corrupt file — start fresh, non-fatal


def _save_cooldown_state():
    """Persist current _last_alert timestamps to disk."""
    with open(COOLDOWN_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(_last_alert, fh)


# ---------------------------------------------------------------------------
# Agent imports (deferred to avoid import errors at module level)
# ---------------------------------------------------------------------------

def _import_agents():
    """Import all agents lazily so import errors surface clearly."""
    from data.binance_feed   import fetch_ohlcv, fetch_ticker
    from data.coinglass_feed import fetch_market_snapshot
    from agents.signal_agent import run_all
    from agents.risk_agent   import from_signal_agent
    return fetch_ohlcv, fetch_ticker, fetch_market_snapshot, run_all, from_signal_agent


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> bool:
    """
    Send a plain-text message to TELEGRAM_CHAT_ID via JSON POST.
    Returns True on success, False on failure (non-fatal).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram] Credentials not set — skipping alert.")
        return False
    try:
        resp = requests.post(
            TELEGRAM_URL,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"  [Telegram] Send failed: {exc}")
        return False


def _can_alert(symbol: str) -> bool:
    """Return True if enough time has passed since the last alert for this symbol."""
    return time.time() - _last_alert.get(symbol, 0) >= ALERT_COOLDOWN


def _mark_alerted(symbol: str):
    _last_alert[symbol] = time.time()
    _save_cooldown_state()


# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------

def _load_paper_trades() -> list:
    if not os.path.isfile(PAPER_TRADES_FILE):
        return []
    with open(PAPER_TRADES_FILE, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return []


def _save_paper_trades(trades: list):
    with open(PAPER_TRADES_FILE, "w", encoding="utf-8") as fh:
        json.dump(trades, fh, indent=2)


def _log_paper_trade(symbol: str, plan: dict):
    trades = _load_paper_trades()
    trades.append({
        "id":            f"{symbol}_{int(time.time())}",
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "symbol":        symbol,
        "direction":     plan["direction"],
        "entry":         plan["entry"],
        "stop_loss":     plan["stop_loss"],
        "tp1":           plan["tp1"],
        "tp2":           plan["tp2"],
        "risk_usd":          plan["risk_usd"],
        "contracts":         plan.get("contracts"),
        "position_size_usd": plan.get("position_size_usd"),
        "outcome":           None,
        "outcome_price":     None,
        "outcome_time":      None,
    })
    _save_paper_trades(trades)
    print(f"  [Paper] Logged {plan['direction']} {symbol}  "
          f"entry=${plan['entry']:,.0f}  "
          f"sl=${plan['stop_loss']:,.0f}  "
          f"tp1=${plan['tp1']:,.0f}")


def _check_open_paper_trades(symbol: str, ohlcv: list):
    """
    Scan the latest OHLCV candles against every open paper trade for this symbol.
    Resolves WIN (price hits TP1) or LOSS (price hits SL), first hit wins.
    Updates paper_trades.json in-place.
    """
    trades = _load_paper_trades()
    open_trades = [t for t in trades if t["symbol"] == symbol and t["outcome"] is None]
    if not open_trades:
        return

    updated = False
    for trade in open_trades:
        opened_dt = datetime.fromisoformat(trade["timestamp"]).replace(tzinfo=None)
        direction = trade["direction"]
        tp1 = trade["tp1"]
        sl  = trade["stop_loss"]

        for candle in ohlcv:
            if candle["open_time"] <= opened_dt:
                continue
            if direction == "LONG":
                if candle["high"] >= tp1:
                    trade["outcome"]       = "WIN"
                    trade["outcome_price"] = tp1
                    trade["outcome_time"]  = candle["open_time"].isoformat()
                    break
                if candle["low"] <= sl:
                    trade["outcome"]       = "LOSS"
                    trade["outcome_price"] = sl
                    trade["outcome_time"]  = candle["open_time"].isoformat()
                    break
            else:  # SHORT
                if candle["low"] <= tp1:
                    trade["outcome"]       = "WIN"
                    trade["outcome_price"] = tp1
                    trade["outcome_time"]  = candle["open_time"].isoformat()
                    break
                if candle["high"] >= sl:
                    trade["outcome"]       = "LOSS"
                    trade["outcome_price"] = sl
                    trade["outcome_time"]  = candle["open_time"].isoformat()
                    break

        if trade["outcome"]:
            updated = True
            # Update compound balance
            if trade["outcome"] == "WIN":
                _balance[0] += trade["risk_usd"] * 2
            elif trade["outcome"] == "LOSS":
                _balance[0] -= trade["risk_usd"]
            _save_balance_state()
            tag = f"{_GREEN}✓ WIN{_RESET}" if trade["outcome"] == "WIN" else f"{_RED}✗ LOSS{_RESET}"
            print(f"  [Paper] {tag}  {symbol} {direction}  "
                  f"@ ${trade['outcome_price']:,.0f}  "
                  f"({trade['outcome_time']})  balance=${_balance[0]:,.2f}")

    if updated:
        _save_paper_trades(trades)


# ---------------------------------------------------------------------------
# Alert message builder
# ---------------------------------------------------------------------------

def _build_alert(symbol: str, plan: dict, sig: dict, ctx: dict) -> str:
    """
    Format the Telegram alert message.

    Parameters
    ----------
    plan : output of risk_agent.from_signal_agent()
    sig  : output of signal_agent.run_all()
    ctx  : output of coinglass_feed.fetch_market_snapshot()
    """
    direction = plan["direction"]
    emoji     = "🟢" if direction == "LONG" else "🔴"
    signal    = plan["signal"]
    coin      = symbol.replace("USDT", "")

    entry    = plan["entry"]
    sl       = plan["stop_loss"]
    tp1      = plan["tp1"]
    tp2      = plan["tp2"]
    sl_pct   = -plan["stop_distance_pct"] if direction == "LONG" else plan["stop_distance_pct"]
    tp1_r    = plan["risk_reward"]

    # TP2 R-multiple
    tp2_dist = abs(tp2 - entry)
    sl_dist  = plan["stop_distance"]
    tp2_r    = tp2_dist / sl_dist if sl_dist else 0

    # Signal context
    rsi_str  = f"{sig['rsi']:.0f}" if sig["rsi"] is not None else "N/A"
    macd_str = "bullish" if (sig["macd_hist"] or 0) > 0 else "bearish"
    sweep_str = "YES" if sig.get("sweeps_recent") else "NO"

    # Market context
    summary      = ctx.get("summary", {})
    funding      = summary.get("latest_funding_rate")
    long_pct     = summary.get("latest_long_pct")
    short_pct    = summary.get("latest_short_pct")
    funding_str  = f"{funding:+.3f}%" if funding is not None else "N/A"
    ls_str       = (f"{long_pct:.0f}/{short_pct:.0f}"
                    if long_pct is not None else "N/A")

    lines = [
        *(["📋 PAPER TRADE"] if PAPER_TRADE else []),
        f"{emoji} {signal} — {symbol}",
        f"Entry:     ${entry:>10,.0f}",
        f"Stop loss: ${sl:>10,.0f}  ({sl_pct:+.1f}%)",
        f"TP1:       ${tp1:>10,.0f}  (+{tp1_r:.0f}R)",
        f"TP2:       ${tp2:>10,.0f}  (+{tp2_r:.0f}R)",
        f"",
        f"RSI: {rsi_str} | MACD: {macd_str} | Sweep: {sweep_str}",
        f"Funding: {funding_str} | L/S: {ls_str}",
    ]
    if plan.get("stop_source"):
        lines.append(f"Stop basis: {plan['stop_source']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-symbol processing
# ---------------------------------------------------------------------------

def _process_symbol(
    symbol: str,
    fetch_ohlcv, fetch_ticker, fetch_market_snapshot, run_all, from_signal_agent,
) -> dict:
    """
    Run the full pipeline for one symbol.
    Returns a status dict for terminal display.
    """
    status = {
        "symbol":  symbol,
        "signal":  "ERROR",
        "verdict": None,
        "alerted": False,
        "error":   None,
    }

    try:
        # 1. Candles
        ohlcv  = fetch_ohlcv(symbol, "1h", 200)
        ticker = fetch_ticker(symbol)
        entry  = float(ticker["lastPrice"])
        status["current_price"] = entry

        # 1a. Check open paper trades against latest candles
        if PAPER_TRADE:
            _check_open_paper_trades(symbol, ohlcv)

        # 2. Market context — fetched first so scores include funding/L/S
        try:
            ctx = fetch_market_snapshot(symbol)
        except Exception as ctx_exc:
            print(f"  [ctx] {symbol} market snapshot failed: {ctx_exc}")
            ctx = {"summary": {}}

        summary      = ctx.get("summary", {})
        funding_rate = summary.get("latest_funding_rate")
        ls_long_pct  = summary.get("latest_long_pct")
        ls_short_pct = summary.get("latest_short_pct")

        status["funding"] = funding_rate
        status["ls"]      = (ls_long_pct, ls_short_pct)

        # 3. Signal agent — market context passed in for score-based logic
        sig = run_all(
            ohlcv,
            funding_rate=funding_rate,
            ls_long_pct=ls_long_pct,
            ls_short_pct=ls_short_pct,
        )

        status["signal"]       = sig["signal"]
        status["rsi"]          = sig["rsi"]
        status["hist"]         = sig["macd_hist"]
        status["bull_score"]   = sig["bull_score"]
        status["bear_score"]   = sig["bear_score"]
        status["bull_reasons"] = sig["bull_reasons"]
        status["bear_reasons"] = sig["bear_reasons"]
        status["trend"]        = sig.get("trend", "NEUTRAL")

        # 4. Risk agent — only for strong signals
        if sig["signal"] in ACTIONABLE_SIGNALS:
            plan = from_signal_agent(
                sig, entry,
                account_balance=_balance[0],
                risk_pct=RISK_PCT,
            )
            status["verdict"] = plan["verdict"]

            # 4. Paper log + Telegram alert
            already_open = PAPER_TRADE and any(
                t["symbol"] == symbol and t.get("outcome") is None
                for t in _load_paper_trades()
            )
            if plan["verdict"] == "TAKE_TRADE" and _can_alert(symbol) and not already_open:
                if PAPER_TRADE:
                    _log_paper_trade(symbol, plan)
                _mark_alerted(symbol)   # always set cooldown — prevents duplicate logs if Telegram is down
                msg     = _build_alert(symbol, plan, sig, ctx)
                success = _send_telegram(msg)
                if success:
                    status["alerted"] = True

    except Exception as exc:
        status["error"] = str(exc)

    return status


# ---------------------------------------------------------------------------
# Terminal display helpers
# ---------------------------------------------------------------------------

_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_DIM    = "\033[2m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

_BAR_WIDTH = 10  # visual width; max score is 12

def _score_bar(score: int) -> str:
    filled = min(score, _BAR_WIDTH)
    return "▓" * filled + "░" * (_BAR_WIDTH - filled)

def _print_cycle_header(cycle: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{_CYAN}Cycle #{cycle}  ·  {now}  ·  {POLL_INTERVAL}s interval  ·  balance=${_balance[0]:,.2f}{_RESET}")
    print(f"{_DIM}{'━' * 60}{_RESET}")

def _open_trade_pnl_lines(symbol: str, current_price: float) -> list:
    """Return formatted P&L lines for any open paper trades on this symbol."""
    lines = []
    for t in _load_paper_trades():
        if t["symbol"] != symbol or t.get("outcome") is not None:
            continue
        direction = t["direction"]
        entry     = t["entry"]
        sl        = t["stop_loss"]
        tp1       = t["tp1"]
        pnl_pct   = ((current_price - entry) / entry * 100
                     if direction == "LONG"
                     else (entry - current_price) / entry * 100)
        color = _GREEN if pnl_pct >= 0 else _RED
        sign  = "+" if pnl_pct >= 0 else ""
        lines.append(
            f"  {color}📋 {direction} ${entry:,.0f} → now ${current_price:,.0f}"
            f"  |  {sign}{pnl_pct:.2f}%"
            f"  |  SL:${sl:,.0f}  |  TP1:${tp1:,.0f}{_RESET}"
        )
    return lines

def _print_status(st: dict):
    sym           = st["symbol"][:3]          # BTC / ETH / SOL
    signal        = st.get("signal", "ERROR")
    rsi           = st.get("rsi") or 0
    bull          = st.get("bull_score", 0)
    bear          = st.get("bear_score", 0)
    trend         = st.get("trend", "NEUTRAL")
    bull_r        = st.get("bull_reasons", [])
    bear_r        = st.get("bear_reasons", [])
    funding       = st.get("funding")
    ls_long, ls_short = st.get("ls") or (None, None)
    current_price = st.get("current_price")
    error         = st.get("error")

    if error:
        print(f"\n  {_BOLD}{sym}{_RESET}  {_RED}ERROR  {error[:55]}{_RESET}")
        return

    # Line 1: symbol  signal  trend arrow
    if "STRONG BUY"   in signal: sig_fmt = f"{_BOLD}{_GREEN}{signal}{_RESET}"
    elif "BUY"        in signal: sig_fmt = f"{_GREEN}{signal}{_RESET}"
    elif "STRONG SELL" in signal: sig_fmt = f"{_BOLD}{_RED}{signal}{_RESET}"
    elif "SELL"       in signal: sig_fmt = f"{_RED}{signal}{_RESET}"
    else:                         sig_fmt = f"{_DIM}{signal}{_RESET}"

    if trend == "BULLISH":   trend_arrow = f"{_GREEN}▲ BULLISH{_RESET}"
    elif trend == "BEARISH": trend_arrow = f"{_RED}▼ BEARISH{_RESET}"
    else:                    trend_arrow = f"{_DIM}─ NEUTRAL{_RESET}"

    print(f"\n  {_BOLD}{sym}{_RESET}  {sig_fmt}  {trend_arrow}")

    # Line 2: RSI  Funding  L/S
    funding_str = f"{funding:+.3f}%" if funding is not None else "N/A"
    ls_str      = (f"{ls_long:.0f}/{ls_short:.0f}"
                   if ls_long is not None else "N/A")
    print(f"  RSI:{rsi:>4.1f}  Funding:{funding_str}  L/S:{ls_str}")

    # Line 3: Bull score bar + reasons
    bull_reasons_s = ("  " + "  ".join(bull_r)) if bull_r else ""
    print(f"  {_GREEN}Bull {bull:>2}pt  [{_score_bar(bull)}]{_RESET}{bull_reasons_s}")

    # Line 4: Bear score bar + reasons
    bear_reasons_s = ("  " + "  ".join(bear_r)) if bear_r else ""
    print(f"  {_RED}Bear {bear:>2}pt  [{_score_bar(bear)}]{_RESET}{bear_reasons_s}")

    # Open trade P&L
    if current_price is not None:
        for line in _open_trade_pnl_lines(st["symbol"], current_price):
            print(line)


# ---------------------------------------------------------------------------
# Startup backtest
# ---------------------------------------------------------------------------

def _run_startup_backtest():
    print(f"\n{'='*70}")
    print("  STARTUP BACKTEST — strategy health check")
    print(f"{'='*70}")
    print(f"  Symbol: {BACKTEST_SYMBOL}  |  500 x 1h candles  "
          f"|  train=60%  val=20%  test=20% (sacred)")

    try:
        from backtest.engine import BacktestEngine
        engine = BacktestEngine(
            symbol          = BACKTEST_SYMBOL,
            interval        = "1h",
            total_candles   = 500,
            account_balance = ACCOUNT_BALANCE,
            risk_pct        = RISK_PCT,
            min_rr          = 2.0,
        )
        engine.fetch_and_split()
        engine.run()
        engine.report()
    except Exception as exc:
        print(f"\n  {_YELLOW}Backtest failed: {exc}{_RESET}")
        print("  Continuing to live loop...\n")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print(f"\n{_CYAN}{'='*70}")
    print("  Crypto Signal Bot — starting up")
    print(f"  Symbols   : {', '.join(SYMBOLS)}")
    print(f"  Balance   : ${ACCOUNT_BALANCE:,.0f}  |  Risk: {RISK_PCT}%/trade")
    print(f"  Interval  : {POLL_INTERVAL}s  |  Cooldown: {ALERT_COOLDOWN}s")
    print(f"  Telegram  : {'configured' if TELEGRAM_BOT_TOKEN else 'NOT SET'}")
    print(f"  Mode      : {'📋 PAPER TRADING (no live execution)' if PAPER_TRADE else '🔴 LIVE TRADING'}")
    print(f"{'='*70}{_RESET}\n")

    # Restore persisted state
    _load_balance_state()
    _load_cooldown_state()
    print(f"  {_DIM}Balance restored: ${_balance[0]:,.2f}{_RESET}")
    if _last_alert:
        surviving = {s: int(ALERT_COOLDOWN - (time.time() - t))
                     for s, t in _last_alert.items()
                     if time.time() - t < ALERT_COOLDOWN}
        if surviving:
            parts = "  ·  ".join(f"{s} {r}s left" for s, r in surviving.items())
            print(f"  {_DIM}Cooldown restored: {parts}{_RESET}\n")

    # Startup backtest
    if RUN_STARTUP_BACKTEST:
        _run_startup_backtest()

    # Import agents once
    fetch_ohlcv, fetch_ticker, fetch_market_snapshot, run_all, from_signal_agent = (
        _import_agents()
    )

    print(f"\n  Starting live loop — polling every {POLL_INTERVAL}s")
    print("  Press Ctrl+C to exit\n")

    cycle = 0
    while True:
        cycle += 1
        _print_cycle_header(cycle)

        for symbol in SYMBOLS:
            try:
                st = _process_symbol(
                    symbol,
                    fetch_ohlcv, fetch_ticker, fetch_market_snapshot,
                    run_all, from_signal_agent,
                )
            except Exception as exc:
                st = {"symbol": symbol, "signal": "ERROR",
                      "verdict": None, "alerted": False, "error": str(exc)}
            _print_status(st)

        # Footer
        now = time.time()
        cooling = [s for s in SYMBOLS if now - _last_alert.get(s, 0) < ALERT_COOLDOWN]
        if cooling:
            remaining = {s: int(ALERT_COOLDOWN - (now - _last_alert[s])) for s in cooling}
            parts = "  ·  ".join(f"{s} {r}s" for s, r in remaining.items())
            print(f"\n  {_DIM}cooldown: {parts}{_RESET}")

        next_time = datetime.fromtimestamp(now + POLL_INTERVAL).strftime("%H:%M:%S")
        print(f"\n{_DIM}  ── next cycle at {next_time} ──{_RESET}")

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n{_DIM}  Interrupted by user. Exiting.{_RESET}\n")
