# Week 2 — `pricing` package

This document summarizes what the **`pricing`** package provides, how modules fit together, and how they build on **`core`** and **`chain`** (Week 1).

---

## Big picture

| Layer | Role |
|--------|------|
| **`uniswap_v2_pair`** | Uniswap V2 constant-product math and reserve updates (integer-only); **`from_chain`** via **`ChainClient`**; **`from_subgraph_row`** for indexer snapshots. |
| **`uniswap_v2_discovery`** | Subgraph pagination (**`fetch_pair_rows_paginated`**), optional JSON cache, **`merge_discovered_with_explicit`** (on-chain refresh for **`--pools`** addresses). |
| **`route` / `route_finder`** | Multi-hop paths over loaded pairs; best route by **net** output after gas priced in the output token. |
| **`price_impact_analyzer`** | Trade-size tables, max size for impact bounds, “true cost” estimates including gas (ETH or via explicit price). |
| **`parsed_swap` / `mempool_monitor`** | Decode Uniswap V2 router swaps from pending txs; WebSocket subscription + callback. |
| **`fork_simulator`** | **`eth_call`** simulation of router swaps on a local fork (Anvil/Hardhat). |
| **`pricing_engine`** | **`PricingEngine`** wires client, routing, fork sim, and mempool; **`Quote`** / **`QuoteError`** for end-to-end quotes. |

`pricing` depends on **`core`** (`Address`, `Token`, …) and **`chain`** (`ChainClient`, **`TransactionDecoder`**, **`uniswap_v2_router`** encoding shared with the decoder).

---

## `pricing/` package

### `pricing.uniswap_v2_pair` — `UniswapV2Pair`

- **`get_amount_out` / `get_amount_in`** — Same integer formulas as Solidity (fee in basis points, default 30 bps).
- **`get_spot_price`**, **`get_execution_price`**, **`get_price_impact`** — **`Decimal`** only for display ratios, not for core swap math.
- **`simulate_swap`** — Returns a **new** pair with post-trade reserves (immutable original).
- **`from_chain(address, client)`** — Reads **`token0` / `token1` / `getReserves`** and token metadata via **`ChainClient.w3`**.
- **`from_subgraph_row(dict)`** — Builds a pair from a subgraph **`pairs`** row (skips zero reserves / bad rows).

### `pricing.uniswap_v2_discovery`

- **`resolve_subgraph_url`** — CLI override, then **`UNISWAP_V2_SUBGRAPH_URL`**, or **`THEGRAPH_API_KEY`** + default Uniswap V2 Ethereum subgraph id.
- **`fetch_pair_rows_paginated`** — GraphQL pages ordered by **`reserveUSD`** with **`reserveUSD_gt`** filter.
- **`save_pair_cache` / `load_pair_cache`** — JSON under **`.cache/uniswap_v2_pairs.json`** by default (used by **`scripts/pricing_best_route.py --discover`**).
- **`merge_discovered_with_explicit`** — Subgraph pairs plus **`--pools`** addresses refreshed with **`from_chain`** (overwrites same pair id).

### `pricing.route` — `Route`

- Validates that **`path`** tokens connect consecutive **`pools`**.
- **`get_output(amount_in)`** — Sequential **`get_amount_out`** along the path.
- **`get_intermediate_amounts`** — Per-hop amounts for debugging or UI.
- **`estimate_gas`** — Heuristic (~150k base + ~100k per extra hop) for routing comparisons.

### `pricing.route_finder` — `RouteFinder`

- Builds a token → **(pool, other token)** adjacency graph from a list of pairs.
- **`find_all_routes`** — Simple paths up to **`max_hops`** (no repeated pool or token on path).
- **`find_best_route`** — Maximizes **net** output: gross swap output minus **gas cost expressed in `token_out`** (uses WETH/ETH pools when available, or **`eth_price_in_output`**).
- **`compare_routes`** — Sorted breakdown: gross, gas in wei, gas as output token, net.

### `pricing.price_impact_analyzer` — `PriceImpactAnalyzer`

- **`generate_impact_table`** — Rows: `amount_in`, `amount_out`, spot/execution price, price impact %.
- **`find_max_size_for_impact`**, **`estimate_true_cost`** — Bounds and cost including gas (see tests for ETH-in vs USDC-in behavior).

### `pricing.parsed_swap` — `ParsedSwap`, `try_parse_uniswap_v2_swap`

- **`ParsedSwap`** — Normalized Uniswap V2 router swap from mempool metadata (router, method, path endpoints, amounts, deadline, sender, gas fields).
- **`try_parse_uniswap_v2_swap(tx, decoded)`** — Returns **`None`** unless **`decoded`** is a known V2 swap and **`path`** is valid (uses **`Address.from_string`** for path ends).

### `pricing.mempool_monitor` — `MempoolMonitor`

- **`parse_transaction(tx)`** — **`TransactionDecoder.decode_function_call`** + **`try_parse_uniswap_v2_swap`**.
- **`start()`** — Async: **`newPendingTransactions`** over WebSocket, resolves full txs, parses off-thread with a semaphore for concurrency; invokes the user **callback** for each **`ParsedSwap`**.

### `pricing.fork_simulator` — `ForkSimulator`, `SimulationResult`

- **`simulate_swap(router, swap_params, sender)`** — Raw **`data`** or structured router args (mirrors **`chain.uniswap_v2_router`**); **`eth_call`**; decodes **`uint256[]`** return and uses the **last** element as output; best-effort **`estimate_gas`**.
- **`simulate_route`** — Single **`swapExactTokensForTokens`** over the full **`Route.path`**; if gas estimate fails, falls back to **`Route.estimate_gas`**.
- **`compare_simulation_vs_calculation`** — Single-hop check: closed-form **`get_amount_out`** vs fork (documents fee-on-transfer divergence).
- **`SimulationResult.success` / `error`** — **`ContractLogicError`** revert reasons decoded when possible via **`TransactionDecoder`**. **`logs`** stays empty for plain **`eth_call`** (no log payloads).

### `pricing.pricing_engine` — `PricingEngine`, `Quote`, `QuoteError`

- **`load_pools` / `refresh_pool`** — Populate **`UniswapV2Pair`** from chain and rebuild **`RouteFinder`**.
- **`get_quote`** — Best **net** route, then fork **`simulate_route`**; **`Quote`** carries **gross** (matches simulation), **net** (after gas in output token), **simulated_output**, **gas_estimate**. **`Quote.is_valid`** compares **gross** vs **simulated** within a small relative tolerance (not net vs gross).
- **`swap_router`** — Defaults to mainnet Uniswap V2 router; **`quote_sender`** must have balance + allowance on the fork for real simulations.
- **`affected_pool_addresses`**, **`_on_mempool_swap`** — Mempool swaps that touch loaded pool tokens; recent hits kept in a bounded deque for tests or downstream hooks.
- **`monitor`** is **not** started in **`__init__`**; callers run **`await monitor.start()`** when needed.

---

## `chain/` support used by `pricing`

### `chain.uniswap_v2_router`

- **`UNISWAP_V2_ROUTER_SWAP_ENTRIES`** — Selector + ABI types for the six Uniswap V2 swap functions.
- **`encode_uniswap_v2_swap_calldata`**, **`decode_swap_amounts_return_data`** — Used by **`ForkSimulator`** and merged into **`chain.decoder`** **`_FUNCTION_SELECTORS`** so encoding and decoding stay aligned.

---

## Scripts and tests (Week 2 scope)

- **`scripts/start_fork.ps1`** — Starts **Anvil** against **`ETH_RPC_URL` / `MAINNET_RPC` / `RPC_ENDPOINT`** (see **[setup.md](setup.md)**). Used with local **`FORK_RPC_URL`** for optional fork tests.
- **CLI demos** — `scripts/pricing_impact_table.py` (requires `--pool` and `--token`; absolute impact %), `scripts/pricing_best_route.py` (requires `--token-in`, `--token-out`, `--amount`; optional `--pools`), `scripts/pricing_mempool_monitor.py` (pending V2 swaps via WebSocket). Shorthand: `.\run.ps1 pricing-impact`, `pricing-route`, `pricing-mempool` (Windows).
- **Tests** — `test_uniswap_v2_router.py`, `test_fork_simulator.py`, `test_fork_simulator_integration.py` (**`@pytest.mark.fork`**, **`FORK_RPC_URL`**), `test_pricing_engine.py`, plus existing route/pair/mempool/price-impact coverage (`test_routes.py`, `test_aam_pricer.py`, `test_mempool_monitor.py`, …).

---

## Cross-cutting themes

1. **Integer safety** — Swap amounts and reserves stay **`int`**; **`Decimal`** only where explicitly for ratios/UI.
2. **Gross vs net** — Routing optimizes **net** output (gas in output token); fork simulation returns **gross** tokens out; **`Quote`** and **`is_valid`** keep that distinction explicit.
3. **DRY with `chain`** — Router selectors and calldata live in **`chain.uniswap_v2_router`**, shared with **`TransactionDecoder`**.
4. **Fork prerequisites** — **`quote_sender`** (or Anvil impersonation / WETH wrap + approve in tests) must match on-chain allowance expectations; **`PricingEngine`** does not auto-fund accounts.

---

## Related docs

- **[Week1.md](Week1.md)** — `core` and `chain` foundations.
- **[setup.md](setup.md)** — environment, `run.ps1`, lint/tests, RPC variables.
