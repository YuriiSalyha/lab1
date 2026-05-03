# Week 4 — `strategy` and `executor`

This document summarizes what the **`strategy`** and **`executor`** packages provide, how they sit on top of **`pricing`**, **`exchange`**, and **`inventory`** (Weeks 2–3), and how they connect to the main bot (**`scripts/arb_bot.py`**).

---

## Big picture

| Layer | Role |
|--------|------|
| **`strategy`** | **Signal model** (`Direction`, `Signal`), **fee math** (`FeeStructure`), **opportunity generation** (`SignalGenerator` — spreads, inventory, cooldown, optional optimal size vs `PricingEngine`), **DEX token resolution** from loaded V2 pools, **scoring** (`SignalScorer`), and **execution ordering** (`sort_candidates_by_priority`). |
| **`executor`** | **Two-leg arb execution**: async **`Executor`** with an explicit **state machine** (validate → leg1 → leg2 or unwind), **circuit breaker** and **replay protection**, **CEX-first** vs **DEX-first (Flashbots)** ordering, **simulated** vs **live** CEX IOC / DEX router paths, optional **signed dry-run DEX** (full build + fork preflight + sign, no broadcast), **PnL** from fills using the same fee model, and optional **webhook** helpers for alerts. |

Together they replace the read-only **`ArbChecker`** path with **validated, sized `Signal` objects** and a **coordinated executor** that the bot can drive in dry-run or live configurations.

---

## `strategy/` package

### `strategy.signal` — `Direction`, `Signal`, `to_decimal`

- **`Direction`** — `BUY_CEX_SELL_DEX` vs `BUY_DEX_SELL_CEX` (which venue buys base).
- **`Signal`** — Immutable-style dataclass: pair, prices, spread bps, size, expected gross/fees/net PnL, **score**, timestamps (**`timestamp`** / **`expiry`** / TTL), inventory and limit flags, optional **`metadata`**. All money and spread fields are **`Decimal`**; **`to_decimal`** coerces inputs via **`Decimal(str(x))`** to avoid float noise.
- **Validation** — **`is_valid()`** / **`invalidity_reasons()`** for downstream gates (e.g. executor pre-checks).

### `strategy.fees` — `FeeStructure`

- **CEX taker bps**, **DEX swap bps**, flat **gas in USD**; all coerced with **`to_decimal`**.
- **`total_fee_bps`**, **`breakeven_spread_bps`**, **`total_fee_usd`**, **`net_profit_usd(spread_bps, trade_value_usd)`** — shared language between **signal sizing** and **executor PnL**.

### `strategy.generator` — `SignalGenerator`

- **Purpose** — Turn live **CEX** + **DEX** (or stub) prices into a single **`Signal`** per pair, or **`None`** with diagnosable reasons.
- **Config knobs** — Min spread bps, min profit USD, max position USD, **signal TTL**, **cooldown** per pair, optional **`max_trade_base`** cap.
- **Sizing** — Default path **`generate(pair, size=None)`** searches for a **feasible base size** (inventory + caps); with **size-dependent** `PricingEngine` quotes it uses a **short grid** over candidate sizes to maximize **`FeeStructure.net_profit_usd`** (see **`OPTIMAL_SIZE_GRID_SAMPLES`**).
- **DEX quotes** — Optional **`token_resolver`** maps **`"BASE/QUOTE"`** to on-chain **`Token`** pair for real engine math; otherwise **stub** DEX prices vs CEX mid (documented premiums in code).
- **Operator visibility** — **`last_snapshot`**, **`last_reason`** (e.g. `no_edge`, `inventory_blocked`, `in_cooldown`), **`last_dex_price_source`** (`engine_math` vs `stub`) for console / logging.

### `strategy.dex_token_resolver`

- **`symbol_match`** — Treats **ETH/WETH** and **BTC/WBTC** as compatible with CEX symbols.
- **`find_pool_for_pair`**, **`base_quote_tokens`** — Locate the unique loaded **Uniswap V2** pool and order tokens as **(base, quote)**.
- **`token_resolver_from_pricing_engine`** — Factory for the **`TokenResolver`** callable wired into **`SignalGenerator`**.

### `strategy.scorer` — `SignalScorer`

- **Multi-factor score** in **`[0, 100]`** (`Decimal`): spread, liquidity placeholder, inventory, rolling **history** of outcomes; **TTL decay** so stale signals lose priority.
- **`ScorerConfig`** — Weights and spread anchors; **`recent_results`** drives pair-level success bias after enough samples.

### `strategy.signal_priority` — `ScoredCandidate`, `sort_candidates_by_priority`

- **`ScoredCandidate`** wraps **`(signal, pair)`** with a **`sort_key`** — descending **score**, then **expected_net_pnl**, **spread_bps**, stable **pair** tie-break.
- Used when multiple pairs produce signals in one tick so the bot executes the **best candidate first**.

---

## `executor/` package

### `executor.engine` — `ExecutorState`, `ExecutionContext`, `ExecutorConfig`, `Executor`

- **State machine** (high level): **`IDLE` → `VALIDATING` → `LEG1_PENDING` → `LEG1_FILLED` → `LEG2_PENDING` → `DONE`**, with **`FAILED`** and **`UNWINDING`** on partial failure after leg 1.
- **`ExecutionContext`** — Tracks venues, fill prices/sizes, optional **tx hash**, timestamps, **`actual_net_pnl`**, error string, unwind flag, and **`metadata`** (e.g. signed raw tx from dry-run DEX).
- **`ExecutorConfig`** — Leg timeouts, **`min_fill_ratio`**, **`use_flashbots`** (DEX-first path), **`simulation_mode`**, DEX **slippage bps**, **deadline**, **fork preflight**, **expected chain id**, **mainnet guard**, and **`dex_dry_run_signed`** (live DEX pipeline with **`dry_run=True`** on the leg).
- **`execute(signal)`** flow:
  1. **Circuit breaker** open → fail fast.
  2. **Replay protection** duplicate **`signal_id`** → fail fast.
  3. **Signal validity** check.
  4. Either **`_execute_cex_first`** (default) or **`_execute_dex_first`** (Flashbots-style “DEX failed = no cost” assumption).
  5. On leg 2 failure after leg 1 filled → **`_run_unwind`** (CEX market reverse in live mode; simulated delay + log in sim).
  6. **`finally`**: mark replay, update breaker (**success** on **`DONE`**, else **failure**), optional **`metrics.record_execution`**.

- **CEX leg** — Simulation: small price/size fudge + latency. Live: **IOC limit** padded with **`CEX_SLIPPAGE_PAD`** vs signal CEX price.
- **DEX leg** — Three modes (precedence in code): **`dex_dry_run_signed`** → **`sync_execute_live_dex_leg(..., dry_run=True)`**; else pure **simulation** if **`simulation_mode`**; else **live broadcast** via the same **`sync_execute_live_dex_leg`** (requires wallet, resolver, pricing).
- **`_calculate_pnl`** — Uses **`FeeStructure.total_fee_usd`** on notional from the signal’s CEX reference price and **gross** from resolved CEX/DEX fill prices depending on **`Direction`**.

### `executor.live_dex_leg` — `sync_execute_live_dex_leg`

- **Uniswap V2 router** swap for the DEX arb leg (ERC20–ERC20 path aligned with **`pricing.fork_swap_executor`** / **`chain`** builders).
- **Safety** — **`LiveDexLegError`** for policy blocks; **`_assert_dex_chain`** enforces **optional expected chain id** and **refuses Ethereum mainnet** unless explicitly allowed.
- **`dry_run=True`** — Still runs **route resolution**, optional **fork preflight**, **EIP-1559** build, balance checks, and **signing**; does **not** call **`eth_sendRawTransaction`**. Returns **`signed_raw_tx_hex`** and a **synthetic** **`tx_hash`** prefix (**`0xDRYRUN`**) so CSV / Telegram / console can treat the leg like a completed “paper” broadcast.

### `executor.circuit_breaker` — `CircuitBreaker`

- **Rolling window** of failure timestamps; trips after **`failure_threshold`** within **`window_seconds`**, then stays **open** for **`cooldown_seconds`**.
- **`record_success`** is **leaky** (drops oldest failure) so intermittent successes recover without a full manual reset.
- Optional **`on_trip`** hook (e.g. webhook — see **`webhook_alerts`**).

### `executor.replay_protection` — `ReplayProtection`

- In-memory **`signal_id → executed_at`** map with **TTL pruning**; prevents double execution of the same signal id within the window.

### `executor.webhook_alerts`

- **Stdlib** JSON **POST** helper with timeouts; intended for **best-effort** operator alerts (must not raise on network errors).

---

## Cross-cutting themes

1. **`Decimal` end-to-end** for money, sizes, spreads, scores — time-based knobs stay **`float`** seconds where appropriate.
2. **Shared fee model** — **`FeeStructure`** links **generator** expectations and **executor** realized PnL.
3. **Simulation vs live vs signed dry-run** — Same **`Executor`** API; config selects how each leg is fulfilled, including **production-grade DEX prep** without broadcast.
4. **Safety rails** — Replay dedup, circuit breaker, chain-id / mainnet policy on DEX, partial-fill threshold, unwind path after leg 1.

---

## How this ties to the rest of the repo

- **`scripts/arb_bot.py`** wires **`SignalGenerator`**, **`SignalScorer`** (where used), **`Executor`**, inventory, pricing, and exchange clients; env flags control **dry-run**, **signed DEX dry-run**, and live trading.
- **Tests** (representative): `tests/test_strategy.py` (generator, fees, scorer), `tests/test_engine.py` (executor + circuit/replay), `tests/test_live_dex_leg_unit.py`, `tests/test_dry_run_signed.py`, `tests/test_webhook_alerts.py`, `tests/test_arb_bot_dry_run.py`, `tests/test_recovery.py`.

---

## Related docs

- **[Week1.md](Week1.md)** — `core` and `chain`.
- **[Week2.md](Week2.md)** — `pricing`.
- **[Week3.md](Week3.md)** — `exchange`, `inventory`, and `scripts.arb_checker`.
