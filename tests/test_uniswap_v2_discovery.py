"""Tests for :mod:`pricing.uniswap_v2_discovery`."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.types import Address, Token
from pricing import uniswap_v2_discovery as uvd
from pricing.uniswap_v2_discovery import (
    fetch_pair_rows_paginated,
    load_pair_cache,
    merge_discovered_with_explicit,
    resolve_subgraph_url,
    rows_to_pairs,
    save_pair_cache,
)
from pricing.uniswap_v2_pair import UniswapV2Pair

PAIR_A = Address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
T0 = Address("0x1111111111111111111111111111111111111111")
T1 = Address("0x2222222222222222222222222222222222222222")


def _sample_row(pair_id: str) -> dict:
    return {
        "id": pair_id,
        "reserve0": "1000000000000000000",
        "reserve1": "2000000000",
        "token0": {"id": T0.checksum, "symbol": "AAA", "decimals": 18},
        "token1": {"id": T1.checksum, "symbol": "BBB", "decimals": 6},
    }


def test_resolve_subgraph_url_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNISWAP_V2_SUBGRAPH_URL", "https://env/wrong")
    assert resolve_subgraph_url("https://cli/ok") == "https://cli/ok"


def test_resolve_subgraph_url_from_thegraph_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNISWAP_V2_SUBGRAPH_URL", raising=False)
    monkeypatch.setenv("THEGRAPH_API_KEY", "testkey")
    url = resolve_subgraph_url(None)
    assert "testkey" in url
    assert uvd._UNISWAP_V2_ETHEREUM_SUBGRAPH_ID in url


def test_resolve_subgraph_url_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNISWAP_V2_SUBGRAPH_URL", raising=False)
    monkeypatch.delenv("THEGRAPH_API_KEY", raising=False)
    with pytest.raises(ValueError, match="UNISWAP_V2_SUBGRAPH_URL"):
        resolve_subgraph_url(None)


def test_fetch_pair_rows_paginated_mock() -> None:
    pages = [
        {"data": {"pairs": [_sample_row(PAIR_A.checksum)]}},
        {"data": {"pairs": []}},
    ]

    def fake_post(_url: str, _payload: dict, _timeout: int) -> dict:
        return pages.pop(0)

    rows = fetch_pair_rows_paginated(
        "https://example.com/graphql",
        min_reserve_usd="1",
        max_pairs=5,
        page_size=1,
        post_json=fake_post,
    )
    assert len(rows) == 1
    assert rows[0]["id"] == PAIR_A.checksum


def test_fetch_pair_rows_subgraph_errors() -> None:
    def fake_post(_url: str, _payload: dict, _timeout: int) -> dict:
        return {"errors": [{"message": "indexing"}]}

    with pytest.raises(RuntimeError, match="indexing"):
        fetch_pair_rows_paginated(
            "https://example.com/graphql",
            min_reserve_usd="1",
            max_pairs=3,
            page_size=10,
            post_json=fake_post,
        )


def test_rows_to_pairs_skips_invalid() -> None:
    good = _sample_row(PAIR_A.checksum)
    bad = {"id": "not-an-address", "reserve0": "1", "reserve1": "1"}
    pairs = rows_to_pairs([bad, good])
    assert len(pairs) == 1
    assert pairs[0].address == PAIR_A


def test_save_and_load_pair_cache(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    rows = [_sample_row(PAIR_A.checksum)]
    save_pair_cache(
        path,
        subgraph_url="https://g.example/subgraphs/x",
        min_reserve_usd="100",
        pair_rows=rows,
    )
    loaded, meta = load_pair_cache(path)
    assert loaded == rows
    assert meta["subgraph_url"] == "https://g.example/subgraphs/x"
    assert meta["min_reserve_usd"] == "100"


def test_load_pair_cache_rejects_bad_shape(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"pair_rows": "nope"}), encoding="utf-8")
    with pytest.raises(ValueError, match="pair_rows"):
        load_pair_cache(path)


@patch("pricing.uniswap_v2_discovery.UniswapV2Pair.from_chain")
def test_merge_explicit_overwrites_discovered(mock_from_chain: MagicMock) -> None:
    weth = Token(address=T0, symbol="WETH", decimals=18)
    usdc = Token(address=T1, symbol="USDC", decimals=6)
    discovered = [
        UniswapV2Pair(
            address=PAIR_A,
            token0=weth,
            token1=usdc,
            reserve0=1,
            reserve1=1,
            fee_bps=30,
        )
    ]
    fresh = UniswapV2Pair(
        address=PAIR_A,
        token0=weth,
        token1=usdc,
        reserve0=99,
        reserve1=99,
        fee_bps=30,
    )
    mock_from_chain.return_value = fresh
    client = MagicMock()
    out = merge_discovered_with_explicit(discovered, [PAIR_A], client)
    assert len(out) == 1
    assert out[0].reserve0 == 99
    mock_from_chain.assert_called_once()


def test_route_finder_finds_path_in_discovered_pairs() -> None:
    """Smoke: subgraph-style pairs form a graph usable by RouteFinder."""
    from pricing.route_finder import RouteFinder

    mid = Address("0x3333333333333333333333333333333333333333")
    p0 = UniswapV2Pair.from_subgraph_row(
        {
            "id": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "reserve0": str(10**24),
            "reserve1": str(10**24),
            "token0": {"id": T0.checksum, "symbol": "A", "decimals": 18},
            "token1": {"id": mid.checksum, "symbol": "M", "decimals": 18},
        }
    )
    p1 = UniswapV2Pair.from_subgraph_row(
        {
            "id": "0xcccccccccccccccccccccccccccccccccccccccc",
            "reserve0": str(10**24),
            "reserve1": str(10**24),
            "token0": {"id": mid.checksum, "symbol": "M", "decimals": 18},
            "token1": {"id": T1.checksum, "symbol": "B", "decimals": 18},
        }
    )
    assert p0 is not None and p1 is not None
    tok0 = Token(address=T0, symbol="A", decimals=18)
    # Output token must be WETH (or another _ETH_SYMBOLS) so gas is priced without a WETH pair.
    tok1 = Token(address=T1, symbol="WETH", decimals=18)
    finder = RouteFinder([p0, p1])
    route, _ = finder.find_best_route(tok0, tok1, 10**18, gas_price_gwei=1, max_hops=3)
    assert route is not None
    assert route.num_hops == 2
