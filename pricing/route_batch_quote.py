"""Batch (Multicall) evaluation of :class:`Route` outputs — V3 hops RPC, V2 hops local."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pricing.batch_quote import BatchQuoteExecutor, RawCallQuoteRequest
from pricing.liquidity_pool import QuoteResult
from pricing.route import Route
from pricing.uniswap_v3_pool import UniswapV3PoolQuoter
from pricing.uniswap_v3_quoter import decode_quote_exact_input_single_return


def batch_quote_route_outputs(
    route: Route,
    amount_ins: Sequence[int],
    executor: BatchQuoteExecutor,
    *,
    decode_v3: Callable[[bytes], QuoteResult] = decode_quote_exact_input_single_return,
    chunk_size: int = 200,
) -> list[tuple[int, int | None, int]]:
    """
    For each positive ``amount_in``, simulate the route.

    V2 hops use local math; V3 hops use ``executor`` (one Multicall RPC per chunk per hop).

    Returns parallel list: ``(amount_in, final_out_or_none, summed_gas_units)``.
    """
    amounts = [int(a) for a in amount_ins if int(a) > 0]
    if not amounts or not route.pools:
        return []

    cur = list(amounts)
    gas_sum = [0] * len(amounts)
    valid = [True] * len(amounts)
    n_hops = route.num_hops

    for hop in range(n_hops):
        pool = route.pools[hop]
        token_in = route.path[hop]
        batch_k: list[int] = []
        batch_req: list[RawCallQuoteRequest] = []

        for k in range(len(amounts)):
            if not valid[k]:
                continue
            if isinstance(pool, UniswapV3PoolQuoter):
                batch_k.append(k)
                batch_req.append(pool.raw_quote_request(token_in, cur[k]))
            else:
                try:
                    qr = pool.quote_exact_input(token_in, cur[k])
                except (ValueError, ZeroDivisionError):
                    valid[k] = False
                    continue
                cur[k] = qr.amount_out
                gas_sum[k] += qr.gas_estimate

        for start in range(0, len(batch_req), max(1, chunk_size)):
            sl = slice(start, start + chunk_size)
            chunk_req = batch_req[sl]
            chunk_k = batch_k[sl]
            if not chunk_req:
                continue
            decoded = executor.execute_quote_results(
                chunk_req,
                decode=decode_v3,
            )
            for idx, res in zip(chunk_k, decoded, strict=True):
                if not valid[idx]:
                    continue
                if isinstance(res, BaseException):
                    valid[idx] = False
                    continue
                cur[idx] = res.amount_out
                gas_sum[idx] += res.gas_estimate

    out: list[tuple[int, int | None, int]] = []
    for k in range(len(amounts)):
        amt = amounts[k]
        if valid[k]:
            out.append((amt, cur[k], gas_sum[k]))
        else:
            out.append((amt, None, gas_sum[k]))
    return out
