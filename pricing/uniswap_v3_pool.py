"""Uniswap V3 pool as :class:`LiquidityPoolQuote` via QuoterV2 static calls."""

from __future__ import annotations

from web3 import Web3

from chain.client import ChainClient
from core.types import Address, Token
from pricing.batch_quote import RawCallQuoteRequest
from pricing.liquidity_pool import QuoteResult
from pricing.uniswap_v2_pair import UniswapV2Pair
from pricing.uniswap_v3_quoter import (
    QUOTER_V2_MAINNET,
    decode_quote_exact_input_single_return,
    encode_quote_exact_input_single,
    read_v3_pool_meta,
)


class UniswapV3PoolQuoter:
    """
    Quotes exact-in swaps on one V3 pool using QuoterV2 (``eth_call``, no state change).

    Token metadata is loaded like V2 (``symbol`` / ``decimals`` via RPC).
    """

    def __init__(
        self,
        pool_address: Address,
        client: ChainClient,
        *,
        token0: Token,
        token1: Token,
        fee: int,
        quoter_address: str | None = None,
    ) -> None:
        self._pool_address = pool_address
        self._client = client
        self._w3 = client.w3
        self._token0 = token0
        self._token1 = token1
        self._fee = fee
        self._quoter = (quoter_address or QUOTER_V2_MAINNET).strip()

    @classmethod
    def from_chain(
        cls,
        pool_address: Address,
        client: ChainClient,
        *,
        quoter_address: str | None = None,
    ) -> UniswapV3PoolQuoter:
        t0s, t1s, fee = read_v3_pool_meta(client.w3, pool_address.checksum)
        t0_addr = Address.from_string(t0s)
        t1_addr = Address.from_string(t1s)
        m0 = UniswapV2Pair.fetch_token_metadata(client, t0_addr)
        m1 = UniswapV2Pair.fetch_token_metadata(client, t1_addr)
        sym0 = str(m0.get("symbol") or "UNKNOWN")
        dec0 = int(m0.get("decimals") or 18)
        sym1 = str(m1.get("symbol") or "UNKNOWN")
        dec1 = int(m1.get("decimals") or 18)
        token0 = Token(address=t0_addr, symbol=sym0, decimals=dec0)
        token1 = Token(address=t1_addr, symbol=sym1, decimals=dec1)
        return cls(
            pool_address,
            client,
            token0=token0,
            token1=token1,
            fee=fee,
            quoter_address=quoter_address,
        )

    @property
    def address(self) -> Address:
        return self._pool_address

    @property
    def token0(self) -> Token:
        return self._token0

    @property
    def token1(self) -> Token:
        return self._token1

    @property
    def fee(self) -> int:
        return self._fee

    def pool_id(self) -> str:
        return f"{self._pool_address.lower}:{self._fee}"

    def raw_quote_request(self, token_in: Token, amount_in: int) -> RawCallQuoteRequest:
        """Calldata for QuoterV2 ``quoteExactInputSingle`` (Multicall / batch)."""
        if token_in == self._token0:
            token_out = self._token1
        elif token_in == self._token1:
            token_out = self._token0
        else:
            raise ValueError(f"token_in {token_in!r} is not in this pool")
        data = encode_quote_exact_input_single(
            self._w3,
            token_in=token_in.address.checksum,
            token_out=token_out.address.checksum,
            fee=self._fee,
            amount_in=amount_in,
            quoter_address=self._quoter,
        )
        return RawCallQuoteRequest(
            target=Web3.to_checksum_address(self._quoter),
            data=data,
            allow_failure=True,
        )

    def quote_exact_input(self, token_in: Token, amount_in: int) -> QuoteResult:
        req = self.raw_quote_request(token_in, amount_in)
        ret = self._w3.eth.call(
            {
                "to": Web3.to_checksum_address(self._quoter),
                "data": req.data,
            }
        )
        return decode_quote_exact_input_single_return(bytes(ret))
