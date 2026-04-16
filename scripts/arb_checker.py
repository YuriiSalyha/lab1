# scripts/arb_checker.py
"""End-to-end arbitrage check: DEX (V2 pool) + CEX order book + inventory."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from core.types import Address, Token, TokenAmount
from exchange.client import ExchangeClient
from inventory.pnl import PnLEngine
from inventory.tracker import InventoryTracker, Venue
from pricing.price_impact_analyzer import impact_row_for_amount
from pricing.pricing_engine import PricingEngine
from pricing.uniswap_v2_pair import UniswapV2Pair

# Default cost assumptions (override via constructor or CLI)
DEFAULT_CEX_TAKER_FEE_BPS = Decimal("10")
DEFAULT_CEX_SLIPPAGE_BPS = Decimal("0.4")
DEFAULT_GAS_COST_USD = Decimal("5")
ORDERBOOK_LIMIT = 50

_ROOT = Path(__file__).resolve().parents[1]


class ArbCheckError(Exception):
    """Raised when a pool is missing or configuration is invalid."""


def _symbol_match(pair_sym: str, token: Token) -> bool:
    s = pair_sym.upper()
    t = token.symbol.upper()
    if s == t:
        return True
    return {s, t} <= {"ETH", "WETH"}


def _find_pool_for_pair(
    pools: dict[Address, UniswapV2Pair],
    base_sym: str,
    quote_sym: str,
) -> UniswapV2Pair:
    for pool in pools.values():
        ok_b0 = _symbol_match(base_sym, pool.token0) and _symbol_match(quote_sym, pool.token1)
        ok_b1 = _symbol_match(base_sym, pool.token1) and _symbol_match(quote_sym, pool.token0)
        if ok_b0 or ok_b1:
            return pool
    raise ArbCheckError(
        f"No Uniswap V2 pool loaded for {base_sym}/{quote_sym}. Call load_pools first.",
    )


def _base_quote_tokens(pool: UniswapV2Pair, base_sym: str, quote_sym: str) -> tuple[Token, Token]:
    for t in (pool.token0, pool.token1):
        if _symbol_match(base_sym, t):
            base_t = t
            break
    else:
        raise ArbCheckError(f"Base token {base_sym} not found on pool")
    for t in (pool.token0, pool.token1):
        if _symbol_match(quote_sym, t):
            quote_t = t
            break
    else:
        raise ArbCheckError(f"Quote token {quote_sym} not found on pool")
    return base_t, quote_t


def _pct_to_bps(price_impact_pct: Decimal) -> Decimal:
    """impact_row price_impact_pct is in 'percent' units (e.g. 1.2 means 1.2%)."""
    return price_impact_pct * Decimal("100")


class ArbChecker:
    """
    End-to-end arbitrage check: detect → validate → check inventory.
    Does NOT execute — just identifies opportunities.
    """

    def __init__(
        self,
        pricing_engine: PricingEngine,
        exchange_client: ExchangeClient,
        inventory_tracker: InventoryTracker,
        pnl_engine: PnLEngine | None = None,
        *,
        cex_taker_fee_bps: Decimal | None = None,
        cex_slippage_bps: Decimal | None = None,
        default_gas_cost_usd: Decimal | None = None,
    ) -> None:
        self._pricing = pricing_engine
        self._exchange = exchange_client
        self._inventory = inventory_tracker
        self._pnl = pnl_engine
        self._cex_fee_bps = (
            cex_taker_fee_bps if cex_taker_fee_bps is not None else DEFAULT_CEX_TAKER_FEE_BPS
        )
        self._cex_slip_bps = (
            cex_slippage_bps if cex_slippage_bps is not None else DEFAULT_CEX_SLIPPAGE_BPS
        )
        self._default_gas_usd = (
            default_gas_cost_usd if default_gas_cost_usd is not None else DEFAULT_GAS_COST_USD
        )

    def check(
        self,
        pair: str,
        size_base: Decimal,
        *,
        gas_cost_usd: Decimal | None = None,
    ) -> dict:
        """
        Full arb check for a trading pair.

        ``gap_bps`` is the gross edge vs mid: ``(edge / mid) * 10000`` where
        ``mid`` is the average of DEX effective price and the relevant CEX quote.
        ``estimated_net_pnl_bps`` = ``gap_bps - estimated_costs_bps``.
        """
        self._exchange._validate_symbol(pair)
        parts = pair.upper().split("/")
        if len(parts) != 2:
            raise ValueError("pair must be like ETH/USDT")
        base_sym, quote_sym = parts[0], parts[1]

        if not self._pricing.pools:
            raise ArbCheckError("No pools loaded on PricingEngine; call load_pools first.")

        pool = _find_pool_for_pair(self._pricing.pools, base_sym, quote_sym)
        base_t, quote_t = _base_quote_tokens(pool, base_sym, quote_sym)

        size_base = Decimal(size_base)
        if size_base <= 0:
            raise ValueError("size_base must be positive")

        raw_base = TokenAmount.from_human(size_base, base_t.decimals, base_t.symbol).raw

        ob = self._exchange.fetch_order_book(pair, limit=ORDERBOOK_LIMIT)
        bb = ob["best_bid"]
        ba = ob["best_ask"]
        mid = ob["mid_price"]
        if bb is None or ba is None or mid is None or mid <= 0:
            raise ArbCheckError("Order book missing bid/ask/mid")
        best_bid = bb[0]
        best_ask = ba[0]

        gas_usd = self._default_gas_usd if gas_cost_usd is None else Decimal(gas_cost_usd)
        notional_usd = size_base * mid
        gas_bps = (gas_usd / notional_usd * Decimal("10000")) if notional_usd > 0 else Decimal("0")

        dex_fee_bps = Decimal(pool.fee_bps)

        # --- buy DEX (spend quote), sell CEX ---
        quote_in_raw = pool.get_amount_in(raw_base, base_t)
        quote_in_human = Decimal(quote_in_raw) / Decimal(10**quote_t.decimals)
        dex_buy_px = quote_in_human / size_base
        row_buy = impact_row_for_amount(pool, quote_t, quote_in_raw)
        dex_impact_buy_bps = _pct_to_bps(row_buy["price_impact_pct"])

        edge_buy = best_bid - dex_buy_px
        mid_buy = (dex_buy_px + best_bid) / Decimal("2")
        gap_buy_bps = (edge_buy / mid_buy * Decimal("10000")) if mid_buy > 0 else Decimal("0")

        costs_buy = (
            dex_fee_bps + dex_impact_buy_bps + self._cex_fee_bps + self._cex_slip_bps + gas_bps
        )
        net_buy_bps = gap_buy_bps - costs_buy

        inv_buy = self._inventory.can_execute(
            Venue.WALLET,
            quote_sym,
            quote_in_human,
            Venue.BINANCE,
            base_sym,
            size_base,
        )

        # --- buy CEX, sell DEX ---
        quote_out_raw = pool.get_amount_out(raw_base, base_t)
        quote_out_human = Decimal(quote_out_raw) / Decimal(10**quote_t.decimals)
        dex_sell_px = quote_out_human / size_base
        row_sell = impact_row_for_amount(pool, base_t, raw_base)
        dex_impact_sell_bps = _pct_to_bps(row_sell["price_impact_pct"])

        edge_sell = dex_sell_px - best_ask
        mid_sell = (dex_sell_px + best_ask) / Decimal("2")
        gap_sell_bps = (edge_sell / mid_sell * Decimal("10000")) if mid_sell > 0 else Decimal("0")

        cex_buy_cost = best_ask * size_base
        inv_sell = self._inventory.can_execute(
            Venue.BINANCE,
            quote_sym,
            cex_buy_cost,
            Venue.WALLET,
            base_sym,
            size_base,
        )

        costs_sell = (
            dex_fee_bps + dex_impact_sell_bps + self._cex_fee_bps + self._cex_slip_bps + gas_bps
        )
        net_sell_bps = gap_sell_bps - costs_sell

        # pick better direction by net bps
        if net_buy_bps >= net_sell_bps:
            direction = "buy_dex_sell_cex"
            gap_bps = gap_buy_bps
            dex_price = dex_buy_px
            dex_impact_bps = dex_impact_buy_bps
            estimated_costs_bps = costs_buy
            estimated_net_pnl_bps = net_buy_bps
            inventory_ok = inv_buy["can_execute"]
            inv_exec = inv_buy
        else:
            direction = "buy_cex_sell_dex"
            gap_bps = gap_sell_bps
            dex_price = dex_sell_px
            dex_impact_bps = dex_impact_sell_bps
            estimated_costs_bps = costs_sell
            estimated_net_pnl_bps = net_sell_bps
            inventory_ok = inv_sell["can_execute"]
            inv_exec = inv_sell

        executable = inventory_ok and estimated_net_pnl_bps > 0

        return {
            "pair": pair,
            "timestamp": datetime.utcnow(),
            "dex_price": dex_price,
            "cex_bid": best_bid,
            "cex_ask": best_ask,
            "gap_bps": gap_bps,
            "direction": direction,
            "estimated_costs_bps": estimated_costs_bps,
            "estimated_net_pnl_bps": estimated_net_pnl_bps,
            "inventory_ok": inventory_ok,
            "executable": executable,
            "details": {
                "dex_price_impact_bps": dex_impact_bps,
                "cex_slippage_bps": self._cex_slip_bps,
                "cex_fee_bps": self._cex_fee_bps,
                "dex_fee_bps": dex_fee_bps,
                "gas_cost_usd": gas_usd,
                "notional_usd": notional_usd,
                "gas_bps": gas_bps,
                "quote_in_human": quote_in_human if direction == "buy_dex_sell_cex" else None,
                "cex_buy_cost_usd": cex_buy_cost if direction == "buy_cex_sell_dex" else None,
                "inventory_check": inv_exec,
                "net_buy_bps": net_buy_bps,
                "net_sell_bps": net_sell_bps,
            },
        }


def _repo_root() -> Path:
    return _ROOT


def _rpc_from_env(cli_rpc: str | None) -> str:
    load_dotenv()
    if cli_rpc and cli_rpc.strip():
        return cli_rpc.strip()
    for key in ("MAINNET_RPC", "ETH_MAINNET_RPC", "RPC_ENDPOINT"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    raise SystemExit(
        "Set MAINNET_RPC / ETH_MAINNET_RPC / RPC_ENDPOINT or pass --rpc URL",
    )


def _pool_from_env(cli_pool: str | None) -> str:
    load_dotenv()
    if cli_pool and cli_pool.strip():
        return cli_pool.strip()
    v = os.environ.get("ARB_V2_POOL", "").strip()
    if v:
        return v
    raise SystemExit("Pass --pool V2_PAIR_ADDRESS or set ARB_V2_POOL")


def _print_report(result: dict, size_base: Decimal, pair: str) -> None:
    base = pair.split("/")[0]
    quote = pair.split("/")[1]
    d = result["details"]
    print()
    print("═" * 43)
    print(f"  ARB CHECK: {pair} (size: {size_base} {base})")
    print("═" * 43)
    print()
    print("Prices:")
    print(f"  Uniswap V2:      ${result['dex_price']:.2f} (buy {size_base} {base})")
    if result["direction"] == "buy_cex_sell_dex":
        print(f"  Uniswap V2 (sell): ${result['dex_price']:.2f} revenue for {size_base} {base}")
    print(f"  Binance bid:      ${result['cex_bid']:.2f}")
    print(f"  Binance ask:      ${result['cex_ask']:.2f}")
    print()
    gap = result["gap_bps"]
    print(f"Gap: {gap:.1f} bps (direction: {result['direction']})")
    print()
    print("Costs:")
    print(f"  DEX fee:           {d['dex_fee_bps']:.1f} bps")
    print(f"  DEX price impact:   {d['dex_price_impact_bps']:.1f} bps")
    print(f"  CEX fee:           {d['cex_fee_bps']:.1f} bps")
    print(f"  CEX slippage:       {d['cex_slippage_bps']:.1f} bps")
    print(f"  Gas:               ${d['gas_cost_usd']:.2f} ({d['gas_bps']:.1f} bps)")
    print("  " + "─" * 24)
    print(f"  Total costs:       {result['estimated_costs_bps']:.1f} bps")
    print()
    net = result["estimated_net_pnl_bps"]
    ok = net > 0
    print(f"Net PnL estimate: {net:.1f} bps {'OK PROFITABLE' if ok else 'NOT PROFITABLE'}")
    print()
    # inventory lines
    ic = d["inventory_check"]
    if result["direction"] == "buy_dex_sell_cex":
        need_q = d.get("quote_in_human") or Decimal("0")
        print("Inventory:")
        print(
            f"  Wallet {quote}:  {ic['buy_venue_available']:.0f} "
            f"(need ~{need_q:.0f}) {'OK' if ic['can_execute'] else 'NO'}"
        )
        print(
            f"  Binance {base}:   {ic['sell_venue_available']:.1f}   "
            f"(need {size_base})    {'OK' if ic['can_execute'] else 'NO'}"
        )
    else:
        print("Inventory:")
        print(
            f"  Binance {quote}:  {ic['buy_venue_available']:.0f} "
            f"(need ~{d.get('cex_buy_cost_usd', 0):.0f}) {'OK' if ic['can_execute'] else 'NO'}"
        )
        print(
            f"  Wallet {base}:   {ic['sell_venue_available']:.1f}   "
            f"(need {size_base})    {'OK' if ic['can_execute'] else 'NO'}"
        )
    print()
    if not result["executable"]:
        reason = []
        if not result["inventory_ok"]:
            reason.append("inventory")
        if net <= 0:
            reason.append("costs exceed gap")
        print(f"Verdict: SKIP — {', '.join(reason)}")
    else:
        print("Verdict: EXECUTABLE")
    print("═" * 43)
    print()


def main(argv: list[str] | None = None) -> None:
    if str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(_repo_root()))

    p = argparse.ArgumentParser(description="Arbitrage check (DEX + Binance + inventory)")
    p.add_argument("pair", help="Unified symbol e.g. ETH/USDT")
    p.add_argument("--size", type=str, required=True, help="Trade size in base asset, e.g. 2.0")
    p.add_argument("--rpc", default=None, help="Ethereum HTTP RPC (or MAINNET_RPC env)")
    p.add_argument("--pool", default=None, help="Uniswap V2 pair address (or ARB_V2_POOL env)")
    p.add_argument("--gas-usd", type=str, default=None, help="Gas estimate in USD")
    args = p.parse_args(argv)

    from chain.client import ChainClient
    from config.config import BINANCE_CONFIG

    rpc = _rpc_from_env(args.rpc)
    pool_hex = _pool_from_env(args.pool)
    size_base = Decimal(args.size)

    chain = ChainClient([rpc])
    quote_sender = Address.from_string("0x0000000000000000000000000000000000000001")
    fork_url = os.environ.get("FORK_RPC_URL", "http://127.0.0.1:8545")
    ws_url = os.environ.get("WS_URL", "ws://127.0.0.1:8546")
    engine = PricingEngine(chain, fork_url, ws_url, quote_sender)
    engine.load_pools([Address.from_string(pool_hex)])

    xc = ExchangeClient(BINANCE_CONFIG)
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    try:
        bal = xc.fetch_balance()
        tracker.update_from_cex(Venue.BINANCE, bal)
    except Exception:
        pass
    pnl = PnLEngine()
    checker = ArbChecker(engine, xc, tracker, pnl)
    gas_usd = Decimal(args.gas_usd) if args.gas_usd else None
    result = checker.check(args.pair, size_base, gas_cost_usd=gas_usd)
    _print_report(result, size_base, args.pair.upper())


if __name__ == "__main__":
    main()
