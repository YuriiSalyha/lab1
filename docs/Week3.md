# Week 3 — `exchange`, `inventory`, and `scripts.arb_checker`

This document summarizes what the **`exchange`**, **`inventory`**, and **`scripts/arb_checker`** module provide, how they fit together with **`pricing`** (Week 2) and **`chain`** (Week 1), and how they are tested and run from the CLI.

---

## Big picture

| Layer | Role |
|--------|------|
| **`exchange`** | CCXT-based **Binance (testnet)** access: normalized **order book** and **balances**, request **weight** limiting aligned with Binance REST budgets, optional **IOC limit orders**, and an **order book analyzer** CLI. |
| **`inventory`** | **Cross-venue** balance tracking (CEX + on-chain wallet), **pre-trade** sufficiency checks, **skew** / rebalance signals, **rebalance planning** (fees, min operating balances), and **PnL** for completed arb legs. |
| **`scripts.arb_checker`** | **`ArbChecker`** — combines DEX quotes (**`UniswapV2Pair`** via **`PricingEngine.load_pools`**), CEX prices (**`ExchangeClient.fetch_order_book`**), and inventory (**`InventoryTracker.can_execute`**) into a single **read-only** opportunity assessment. Lives under **`scripts/`** (runnable as **`python -m scripts.arb_checker`**). |

`exchange` depends on **ccxt** (see `pyproject.toml`). `inventory` depends on **`core`**-style `Decimal` usage only (no hard dependency on `exchange` types beyond conventions). **`scripts/arb_checker.py`** depends on **`pricing`**, **`exchange`**, **`inventory`**, **`chain`**, and **`config`** (Binance config for the CLI).

---

## `exchange/` package

### `exchange.client` — `ExchangeClient`

- **CCXT Binance** instance with **`sandbox: True`** in the usual lab config (`config.config.BINANCE_CONFIG` from `.env`).
- **`WeightRateLimiter`** — sliding window (default **1200** weight / **60** s) before each weighted REST call; weights follow Binance docs (e.g. **`orderbook_request_weight(limit)`** for depth).
- **`fetch_order_book(symbol, limit)`** — Normalized dict: **`bids`** / **`asks`** as `(price, qty)` lists sorted **bid high→low** / **ask low→high**; **`best_bid`** / **`best_ask`** as tuples; **`mid_price`**, **`spread_bps`**; all monetary fields as **`Decimal`**.
- **`fetch_balance()`** — Per-asset **`free`**, **`used`**, **`total`** as **`Decimal`** (zeros skipped).
- **`create_limit_ioc_order`** — Limit **IOC** (immediate-or-cancel) with validation and normalized order result.
- **`to_decimal`**, **`_validate_symbol`** — Unified symbols such as **`ETH/USDT`**.
- **Health check** on init via **`fetch_time`**; retries classify **ccxt** network / availability errors.

### `exchange.rate_limiter` — `WeightRateLimiter`

- **`acquire(weight)`** — Sleeps until the sum of weights in the last **`window_sec`** is ≤ **`max_weight`** (FIFO deque of `(timestamp, weight)`).

### `exchange.orderbook` — `OrderBookAnalyzer`, CLI

- **`OrderBookAnalyzer`** — Takes the normalized dict from **`fetch_order_book`**. **`walk_the_book(side, qty)`** simulates filling a **base** quantity against bids (sell) or asks (buy); returns average price, cost, slippage vs best touch, levels consumed, fills.
- **CLI**: `python -m exchange.orderbook SYMBOL [--depth N] ...` (also **`.\run.ps1 orderbook ETH/USDT`**).

---

## `inventory/` package

### `inventory.tracker` — `Venue`, `Balance`, `InventoryTracker`

- **`Venue`** — Enum (e.g. **`BINANCE`**, **`WALLET`**).
- **`Balance`** — **`free`**, **`locked`**, **`total`** per asset at a venue.
- **`update_from_cex`** — Replaces one venue’s map from **`ExchangeClient.fetch_balance()`**-shaped rows.
- **`update_from_wallet`** — **`{asset: amount}`** treated as all **free**.
- **`snapshot(usd_prices=None)`** — Per-venue breakdown and **`totals`**; optional **`total_usd`** if a price map is supplied.
- **`get_available`** — **Free** balance only (tradable).
- **`can_execute`** — Checks **both** legs: buy venue has **`buy_amount`** of **`buy_asset`**, sell venue has **`sell_amount`** of **`sell_asset`**.
- **`record_trade`** — Adjusts balances after a **buy** or **sell** (base/quote) and deducts **fee** from **`fee_asset`**.
- **`skew` / `get_skews`** — Per-venue share of each asset vs equal split; **`deviation_pct`**, **`max_deviation_pct`**, **`needs_rebalance`** vs **`DEFAULT_REBALANCE_DEVIATION_THRESHOLD_PCT`** (or an override).

### `inventory.rebalancer` — `RebalancePlanner`, `TransferPlan`

- **`TRANSFER_FEES`**, **`MIN_OPERATING_BALANCE`**, reference **USD** constants for cost display.
- **`check_all`** — Compact rows from tracker skews.
- **`plan(asset)`** — One **`TransferPlan`** when skew is high: gross **`amount`**, **`estimated_fee`**, **`net_amount`**, respecting **min withdrawal** and **min operating** on the source.
- **`estimate_cost`**, **`plan_all`** — Aggregate fee USD and ETA.
- **CLI**: `python -m inventory.rebalancer --check` / `--plan ETH`.

### `inventory.pnl` — `TradeLeg`, `ArbRecord`, `PnLEngine`

- **`ArbRecord`** — **`gross_pnl`**, **`total_fees`** (USD, including **`gas_cost_usd`**), **`net_pnl`**, **`notional`**, **`net_pnl_bps`**.
- **`PnLEngine`** — **`record`**, **`summary`**, **`recent`**, **`export_csv`**.
- **CLI**: `python -m inventory.pnl --summary` (in-memory engine empty unless you wire trades).

---

## `scripts/arb_checker.py` — `ArbChecker`, `ArbCheckError`

The **`scripts`** directory is a small package (**`scripts/__init__.py`**) so you can run **`python -m scripts.arb_checker`** from the repo root (with **`PYTHONPATH`** / editable install including **`scripts*`**).

- **`ArbChecker(pricing_engine, exchange_client, inventory_tracker, pnl_engine=None, ...)`**
- **`check(pair, size_base, gas_cost_usd=None)`**:
  - Resolves a **`UniswapV2Pair`** from **`pricing_engine.pools`** matching **`ETH`/`WETH`** and quote (e.g. USDT).
  - **CEX**: **`fetch_order_book`** — uses **best bid / best ask** prices from the first level (**`(price, qty)`** tuples).
  - Compares **buy DEX → sell CEX** vs **buy CEX → sell DEX** (V2 **`get_amount_in`** / **`get_amount_out`** + **`impact_row_for_amount`**); picks the better **net** edge in bps.
  - **Costs** — Pool **fee bps**, DEX **impact**, configurable **CEX fee / slippage** bps, **gas** as bps of notional.
  - **Inventory** — **`can_execute`** for the chosen direction (wallet vs Binance legs).
  - Returns **`gap_bps`**, **`estimated_costs_bps`**, **`estimated_net_pnl_bps`**, **`executable`**, **`details`**, etc.
- **`PnLEngine`** is optional for **`check()`** (reserved for future hooks / CLI extras).
- **CLI** (needs **RPC**, **`--pool`** or **`ARB_V2_POOL`**, Binance credentials for balances when available):

  ```bash
  python -m scripts.arb_checker ETH/USDT --size 2.0 --rpc https://... --pool 0x...
  ```

  Equivalent: **`python scripts/arb_checker.py ETH/USDT --size 2.0 ...`** (repo root on **`sys.path`**).

---

## Cross-cutting themes

1. **Decimals** — Money and sizes use **`Decimal`** end-to-end in **`exchange`** responses and **`inventory`**.
2. **Testnet** — Binance config targets **testnet**; inventory **venues** are abstract (Binance + wallet).
3. **No execution in ArbChecker** — Assessment only; rebalancer **plans** transfers but does not send them.

---

## Scripts and tests (Week 3 scope)

- **Tests** — `tests/test_exchange_client.py`, `tests/test_orderbook.py`, `tests/test_inventory.py`, `tests/test_arb_checker.py`.
- **Config** — `config/config.py` **`BINANCE_CONFIG`** from **`BINANCE_TESTNET_API_KEY`** / **`BINANCE_TESTNET_SECRET`** (see `.env.example`).

---

## Related docs

- **[Week1.md](Week1.md)** — `core` and `chain`.
- **[Week2.md](Week2.md)** — `pricing`.
- **[setup.md](setup.md)** — environment, `run.ps1`, lint/tests.
