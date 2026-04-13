# Crypto Trading Bot — Session Summary

## Architecture
- orchestrator.py — main entry point (python orchestrator.py)
- data/binance_feed.py — OHLCV + liquidation websocket
- data/coinglass_feed.py — Funding Rate, OI, L/S Ratio (Binance Futures endpoints, no API key needed)
- agents/signal_agent.py — score-based signal engine (see Signal Logic below)
- agents/risk_agent.py — position sizing, SL, TP1/TP2, R:R verdict
- backtest/engine.py — walk-forward, train/val/test split, overfitting check

## Signal Logic (Score-Based)

### Bull Score (max 8pts)
- RSI < 30 → +3pts (overrides +2)
- RSI < 35 → +2pts
- Bullish FVG present → +2pts
- MACD histogram rising (hist > prev bar) → +1pt
- Funding rate < -0.01% → +1pt
- Short side L/S > 55% → +1pt

### Bear Score (mirror)
- RSI > 70 → +3pts (overrides +2)
- RSI > 65 → +2pts
- Bearish FVG present → +2pts
- MACD histogram falling → +1pt
- Funding rate > +0.01% → +1pt
- Long side L/S > 65% → +1pt

### Decision Thresholds
- ≥ 5pts → STRONG BUY / STRONG SELL (triggers risk agent + paper trade)
- 3–4pts → BUY / SELL (shown in log, no trade)
- < 3pts → HOLD

## Current State
- Mode: PAPER TRADING (no live execution)
- Symbols: BTCUSDT, ETHUSDT, SOLUSDT
- Timeframe: 1h candles
- Risk: 1% per trade on $1,000 simulated balance
- Poll interval: 300s
- Bot running via: nohup python -u orchestrator.py >> logs/bot.log 2>&1 &
- Paper trades collected: 0 (SOL bear 4/5, ETH bull 3/5 — first trades imminent)

## Terminal Display Format
One line per symbol, compact:
```
Cycle #X  ·  2026-04-13 14:41:55  ·  300s interval
  BTC  HOLD        RSI:42.5  Bull:1  Bear:2  [MACD↑ FVG↓]
  ETH  BUY         RSI:40.9  Bull:3  Bear:1  [FVG↑ MACD↑ Longs65%]  +2 to STRONG
  SOL  SELL        RSI:44.0  Bull:0  Bear:4  [FVG↓ MACD↓ Longs75%]  +1 to STRONG
  ── next cycle at 14:47:02 ──
```

## Next Steps
1. Collect 20+ paper trades
2. Analyze WIN/LOSS ratio
3. Fix backtest context window (needs 350+ candles for EMA300)
4. If results good → enable execution agent (Binance Testnet first)
5. Eventually add: Liquidity Heatmap (CoinGlass paid), Order Flow

## Known Issues
- Backtest produces too few trades (EMA300 needs 350 candle context window, currently 200)
- Test set is SACRED — never run manually until strategy is finalized

## Bug Fixes Applied
- Cooldown now triggers immediately when a paper trade is logged, regardless of Telegram success — prevents duplicate trade entries if Telegram is down

## Key Decisions Made
- 1h candles (not 4h) — strategy already built around it
- Wick-based FVG detection — fewer but higher quality signals
- Score-based signal engine — MACD alone is not sufficient; funding rate + L/S included
- Binance Futures for derivatives data — free, no API key needed
- Paper trade before live — collecting real signal data
- Test set is sacred — never touch until strategy finalized
