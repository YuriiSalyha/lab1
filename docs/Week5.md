# Week 5 — Operations: arb bot, risk, monitoring, and production wiring

This document summarizes the **main application loop** (**`scripts/arb_bot.py`**), the new **`risk/`** and **`monitoring/`** packages, the **`safety/`** compatibility shim, and supporting changes across **config**, **exchange**, **strategy**, **executor**, **pricing**, **chain**, and **inventory**. Signal generation and two-leg execution remain as in **[Week4.md](Week4.md)**; Week 5 focuses on **running safely**, **observability**, and **production toggles**.

---

## Big picture

| Area | Role |
|------|------|
| **`scripts/arb_bot.py`** | **`ArbBot`** — async tick loop wiring **`SignalGenerator`**, **`SignalScorer`**, **`Executor`**, **`InventoryTracker`**, **`PnLEngine`**, optional **`PricingEngine`** + pool refresh, **dry-run** (log vs **signed** DEX), **live** CEX/DEX flags, **kill-switch** polling, **`RiskManager`** / **`PreTradeValidator`**, **Telegram** (alerts + optional slash commands), **CSV trade journal**, optional **Prometheus** metrics, **circuit-breaker** hooks (metrics + Telegram + webhook). |
| **`risk/`** | **`RiskLimits`** (env-driven soft limits, ~$100-capital defaults), **`RiskManager`** (daily PnL roll, drawdown, hourly trade count, consecutive losses, open positions, **`safety_check`** hard rails), **`kill_switch`** (file-based stop), **`pre_trade`** validation, **`safety`** absolute caps. |
| **`monitoring/`** | **`TelegramNotifier`**, **`telegram_control`** (long-poll commands), **`trade_csv_log`** (**`TradeCsvJournal`**), **`health_state`**, **`daily_summary`**, **`prometheus_metrics`**, **`logging_setup`** for bot file logging. |
| **`safety/`** | Thin re-export of **`risk.kill_switch`** (and related symbols) for older import paths. |
| **`inventory/usd_mark.py`** | **`estimate_inventory_usd`** — reference USD marks for portfolio / capital checks shared with risk and PnL. |

---

## `scripts/arb_bot.py` — `ArbBot`, `ArbBotConfig`

- **Modes** — **`--demo`**: offline **`MockExchange`**, scripted spreads, exits after the demo script. **Non-demo**: real **`ExchangeClient`** (Binance from **`config.config.BINANCE_CONFIG`**), optional **`PricingEngine`** when RPC + pools resolve.
- **Dry-run** — No CEX/DEX broadcast; **`ARB_DRY_RUN_MODE=log`** (default) skips executor; **`signed`** runs the **live DEX path** with **`dry_run=True`** (route, fork preflight, EIP-1559 build, sign — **no** `eth_sendRawTransaction`). See **`.env.example`** (`ARB_DRY_RUN_MODE`) and the **`scripts/arb_bot.py`** module docstring.
- **Fees** — Non-demo builds **`FeeStructure`** via **`_resolve_live_fee_structure`**: optional **`ARB_CEX_TAKER_BPS`**, else **`ExchangeClient.max_taker_fee_bps_for_symbols`** over **`--pairs`**, else static default. Demo zeros **gas** in **`FeeStructure`** for scripted profitability.
- **Inventory** — CEX + wallet sync; **`ARB_VIRTUAL_BALANCES`** / **`ARB_VIRTUAL_CEX_BALANCES`** in dry-run only; **`ARB_POOL_REFRESH_SECONDS`** to re-read V2 reserves for generator quotes.
- **Risk** — **`RiskLimits.from_env()`**, **`RiskManager`** with **`ARB_INITIAL_CAPITAL`** (default 100), pre-trade checks, optional post-trade **balance verify** (env-gated).
- **Resilience** — **`CircuitBreaker`** with **`on_trip`** chain: metrics, urgent Telegram, optional **`ARB_CIRCUIT_WEBHOOK_URL`** / **`ARB_WEBHOOK_URL`** JSON POST.
- **Output** — Structured per-tick logging, **`TradeCsvJournal`**, Telegram on opportunities / trades / halts / kill switch.

---

## `risk/` package

- **`risk.limits`** — **`RiskLimits`** dataclass; **`defaults()`** vs **`from_env()`** for all **`ARB_MAX_*`** style variables (including **`ARB_CONSECUTIVE_LOSS_LIMIT`**).
- **`risk.manager`** — **`RiskManager.can_trade`**, **`record_trade_result`**, **`patch_limits`** for Telegram **`/set`**.
- **`risk.kill_switch`** — **`is_kill_switch_active`**, **`default_kill_switch_path`** (Linux **`/tmp/arb_bot_kill`**, Windows temp dir), override **`ARB_KILL_SWITCH_FILE`**.
- **`risk.pre_trade`** — **`PreTradeValidator`** for gate checks before execution.
- **`risk.safety`** — **`safety_check`** absolute floors / ceilings independent of soft limits.

---

## `monitoring/` package

- **`TelegramNotifier`** — Bot API **`sendMessage`** (stdlib **urllib**); never raises to callers.
- **`telegram_control`** — **`getUpdates`** long-poll, **`/pause`**, **`/resume`**, **`/stop_bot`**, **`/status`**, **`/help`**, **`/set`** (same chat as **`TELEGRAM_CHAT_ID`**).
- **`trade_csv_log`** — Append-only CSV of signals / executions / dry-run outcomes; path **`ARB_TRADE_CSV`**, disable **`ARB_TRADE_CSV_DISABLED`**.
- **`health_state`**, **`daily_summary`** — Session health and plain-text summaries for logs / Telegram.
- **`prometheus_metrics`** — Optional **`/metrics`** when **`PROMETHEUS_METRICS_PORT`** is set (extra **`[metrics]`** in **`pyproject.toml`**).

---

## Supporting code and config

- **`config/config.py`** — **`PRODUCTION`**: Binance **live** vs **testnet** keys; **`BYBIT_CONFIG`** for multi-exchange / dashboard tooling.
- **`exchange/client.py`** — **`max_taker_fee_bps_for_symbols`**; dependency note: **`websockets`** declared for WS helpers.
- **`strategy/fees.py`** — **`cex_taker_bps_from_ccxt_ratio`** (CCXT taker fraction → bps).
- **`strategy/generator.py`** — Pool reserve refresh cadence integration for fresher DEX quotes when the bot supplies a pricing engine.
- **`executor/engine.py`**, **`executor/live_dex_leg.py`** — **`ExecutionContext.metadata`** for signed dry-run payloads (raw tx, preflight gas, synthetic hash prefix **`0xDRYRUN`**).
- **`pricing/pricing_engine.py`**, **`scripts/pricing_best_route.py`** — Ongoing routing / quote robustness (see tests **`test_pricing_engine.py`**).
- **`chain/client.py`** — RPC / health-related hardening used by fork and live paths.
- **`inventory/pnl.py`**, **`inventory/rebalancer.py`** — Small alignments with USD marks and fee reporting where applicable.

---

## Tests and documentation

- New or expanded tests under **`tests/`** for **arb bot dry-run**, **kill switch**, **risk manager**, **safety**, **Telegram** (alerts + control), **trade CSV**, **balance verify**, **signed dry-run**, **pool refresh throttle**, **exchange** fee aggregation, **pricing** / **live DEX** units, etc.
- **`docs/PREFLIGHT_CHECKLIST.md`** — Present in the tree as a checklist placeholder (fill before live runs).
- **`.env.example`** — Extended with arb bot, risk, Telegram, DEX live, dry-run signed, webhooks, and **`ARB_CEX_TAKER_BPS`** documentation.

---

## Related docs

- **[Week1.md](Week1.md)** — `core` and `chain`.
- **[Week2.md](Week2.md)** — `pricing`.
- **[Week3.md](Week3.md)** — `exchange`, `inventory`, **`scripts.arb_checker`**.
- **[Week4.md](Week4.md)** — **`strategy`** and **`executor`**.
- **`scripts/arb_bot.py`** — module docstring and **`.env.example`** for signed dry-run and live DEX flags.
- **[setup.md](setup.md)** — Environment and **`run.ps1`**.
