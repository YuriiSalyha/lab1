from decimal import Decimal

from chain.client import ChainClient
from core.errors import InvalidAddressError
from core.types import Address, Token


class UniswapV2Pair:
    """
    Represents a Uniswap V2 liquidity pair.
    All math uses integers only — no floats anywhere.
    """

    def __init__(
        self,
        address: Address,
        token0: Token,
        token1: Token,
        reserve0: int,
        reserve1: int,
        fee_bps: int = 30,  # 0.30% = 30 basis points
    ):
        self.address = address
        self.token0 = token0
        self.token1 = token1
        self.reserve0 = reserve0
        self.reserve1 = reserve1
        self.fee_bps = fee_bps

    def get_amount_out(self, amount_in: int, token_in: Token) -> int:
        """
        Calculate output amount for a given input.
        Must match Solidity exactly:

        amount_in_with_fee = amount_in * (10000 - fee_bps)
        numerator = amount_in_with_fee * reserve_out
        denominator = reserve_in * 10000 + amount_in_with_fee
        amount_out = numerator // denominator
        """
        if token_in == self.token0:
            reserve_in = self.reserve0
            reserve_out = self.reserve1
        elif token_in == self.token1:
            reserve_in = self.reserve1
            reserve_out = self.reserve0
        else:
            raise ValueError(f"Token: {token_in} is not a valid token for this pair")

        amount_in_with_fee = amount_in * (10000 - self.fee_bps)
        numerator = reserve_out * amount_in_with_fee
        denominator = reserve_in * 10000 + amount_in_with_fee
        amount_out = numerator // denominator
        return amount_out

    def get_amount_in(self, amount_out: int, token_out: Token) -> int:
        """
        Calculate required input for desired output.
        (Inverse of get_amount_out)
        """
        if token_out == self.token1:
            reserve_in = self.reserve0
            reserve_out = self.reserve1
        elif token_out == self.token0:
            reserve_in = self.reserve1
            reserve_out = self.reserve0
        else:
            raise ValueError(f"Token: {token_out} is not a valid token for this pair")

        top_part = reserve_in * amount_out * 10000
        bottom_part = (reserve_out - amount_out) * (10000 - self.fee_bps)

        return top_part // bottom_part

    def get_spot_price(self, token_in: Token) -> Decimal:
        """
        Returns spot price (for display only, not calculations).
        """
        if token_in == self.token0:
            return Decimal(self.reserve1) / Decimal(self.reserve0)
        elif token_in == self.token1:
            return Decimal(self.reserve0) / Decimal(self.reserve1)
        else:
            raise ValueError(f"Token: {token_in} is not a valid token for this pair")

    def get_execution_price(self, amount_in: int, token_in: Token) -> Decimal:
        """
        Returns actual execution price for given trade size.
        """
        amount_out = self.get_amount_out(amount_in, token_in)
        return Decimal(amount_out) / Decimal(amount_in)

    def get_price_impact(self, amount_in: int, token_in: Token) -> Decimal:
        """
        Returns price impact as a decimal (0.01 = 1%).
        """
        spot_price = self.get_spot_price(token_in)
        execution_price = self.get_execution_price(amount_in, token_in)
        return (execution_price - spot_price) / (spot_price)

    def simulate_swap(self, amount_in: int, token_in: Token) -> "UniswapV2Pair":
        """
        Returns a NEW pair with updated reserves after the swap.
        (Useful for multi-hop simulation)
        """
        amount_out = self.get_amount_out(amount_in, token_in)
        if token_in == self.token0:
            new_r0 = self.reserve0 + amount_in
            new_r1 = self.reserve1 - amount_out
        else:
            new_r0 = self.reserve0 - amount_out
            new_r1 = self.reserve1 + amount_in
        return UniswapV2Pair(
            address=self.address,
            token0=self.token0,
            token1=self.token1,
            reserve0=new_r0,
            reserve1=new_r1,
            fee_bps=self.fee_bps,
        )

    def with_reserves(self, reserve0: int, reserve1: int) -> "UniswapV2Pair":
        """Same pair metadata with new reserves (e.g. after a ``Sync`` event)."""
        return UniswapV2Pair(
            address=self.address,
            token0=self.token0,
            token1=self.token1,
            reserve0=reserve0,
            reserve1=reserve1,
            fee_bps=self.fee_bps,
        )

    @staticmethod
    def fetch_token_metadata(client: ChainClient, address: Address) -> dict:
        contract = client.w3.eth.contract(
            address=address.checksum,
            abi=ERC20_ABI,
        )

        try:
            symbol = contract.functions.symbol().call()
        except Exception:
            try:
                contract_bytes32 = client.w3.eth.contract(
                    address=address.checksum,
                    abi=[
                        {
                            "name": "symbol",
                            "outputs": [{"type": "bytes32"}],
                            "inputs": [],
                            "type": "function",
                        }
                    ],
                )
                raw = contract_bytes32.functions.symbol().call()

                # Decode bytes32 → string
                symbol = raw.decode("utf-8").rstrip("\x00")
            except Exception:
                symbol = "UNKNOWN"

        try:
            decimals = contract.functions.decimals().call()
        except Exception:
            try:
                contract_bytes32 = client.w3.eth.contract(
                    address=address.checksum,
                    abi=[
                        {
                            "name": "decimals",
                            "outputs": [{"type": "uint8"}],
                            "inputs": [],
                            "type": "function",
                        }
                    ],
                )
            except Exception:
                raise ValueError(f"Failed to fetch decimals for token: {address}") from None

        return {
            "address": address,
            "symbol": symbol,
            "decimals": decimals,
        }

    @classmethod
    def from_subgraph_row(cls, row: dict, fee_bps: int = 30) -> "UniswapV2Pair | None":
        """
        Build a pair from a Uniswap V2 subgraph ``pairs`` document (``id``, ``reserve0``/``1``,
        nested ``token0`` / ``token1``). Returns ``None`` if reserves are zero or data is invalid.
        """
        try:
            addr = Address.from_string(str(row["id"]))
            r0 = int(row["reserve0"])
            r1 = int(row["reserve1"])
            t0 = row["token0"]
            t1 = row["token1"]
            token0 = Token(
                address=Address.from_string(str(t0["id"])),
                symbol=str(t0.get("symbol") or "UNKNOWN"),
                decimals=int(t0["decimals"]),
            )
            token1 = Token(
                address=Address.from_string(str(t1["id"])),
                symbol=str(t1.get("symbol") or "UNKNOWN"),
                decimals=int(t1["decimals"]),
            )
        except (KeyError, TypeError, ValueError, InvalidAddressError):
            return None
        if r0 <= 0 or r1 <= 0:
            return None
        return cls(
            address=addr,
            token0=token0,
            token1=token1,
            reserve0=r0,
            reserve1=r1,
            fee_bps=fee_bps,
        )

    @classmethod
    def from_chain(
        cls, address: Address, client: ChainClient, fee_bps: int = 30
    ) -> "UniswapV2Pair":
        """
        Fetch pair data from on-chain.
        """

        contract = client.w3.eth.contract(
            address=address.checksum,
            abi=PAIR_ABI,
        )
        token0_addr = Address.from_string(contract.functions.token0().call())
        token1_addr = Address.from_string(contract.functions.token1().call())

        reserve0, reserve1, _ = contract.functions.getReserves().call()

        t0_meta = cls.fetch_token_metadata(client, token0_addr)
        t1_meta = cls.fetch_token_metadata(client, token1_addr)

        token0 = Token(address=token0_addr, symbol=t0_meta["symbol"], decimals=t0_meta["decimals"])
        token1 = Token(address=token1_addr, symbol=t1_meta["symbol"], decimals=t1_meta["decimals"])

        return cls(
            address=address,
            token0=token0,
            token1=token1,
            reserve0=reserve0,
            reserve1=reserve1,
            fee_bps=fee_bps,
        )


ERC20_ABI = [
    {"name": "symbol", "outputs": [{"type": "string"}], "inputs": [], "type": "function"},
    {"name": "decimals", "outputs": [{"type": "uint8"}], "inputs": [], "type": "function"},
]

PAIR_ABI = [
    {"name": "token0", "outputs": [{"type": "address"}], "inputs": [], "type": "function"},
    {"name": "token1", "outputs": [{"type": "address"}], "inputs": [], "type": "function"},
    {
        "name": "getReserves",
        "outputs": [
            {"type": "uint112"},
            {"type": "uint112"},
            {"type": "uint32"},
        ],
        "inputs": [],
        "type": "function",
    },
]
