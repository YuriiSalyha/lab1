from decimal import Decimal

from core.types import Token

from .uniswap_v2_pair import UniswapV2Pair


def impact_row_for_amount(pair: UniswapV2Pair, token_in: Token, amount_in: int) -> dict:
    """
    Single-row price impact metrics for one trade size (shared by table, WS feed, history).

    Returns:
        ``amount_in``, ``amount_out``, ``spot_price``, ``execution_price``, ``price_impact_pct``.
    """
    if amount_in <= 0:
        raise ValueError(f"Amount in must be greater than 0, got {amount_in}")
    amount_out = pair.get_amount_out(amount_in, token_in)
    spot_price = pair.get_spot_price(token_in)
    execution_price = pair.get_execution_price(amount_in, token_in)
    price_impact_pct = pair.get_price_impact(amount_in, token_in) * 100
    return {
        "amount_in": amount_in,
        "amount_out": amount_out,
        "spot_price": spot_price,
        "execution_price": execution_price,
        "price_impact_pct": price_impact_pct,
    }


class PriceImpactAnalyzer:
    """
    Analyzes price impact across different trade sizes for Uniswap V2 pairs.
    """

    def __init__(self, pair: UniswapV2Pair):
        self.pair = pair

    def generate_impact_table(
        self,
        token_in: Token,
        sizes: list[int],  # List of input amounts to analyze
    ) -> list[dict]:
        """
        Calculates execution metrics for a list of input amounts.

        Returns list of:
        {
            'amount_in': int,
            'amount_out': int,
            'spot_price': Decimal,
            'execution_price': Decimal,
            'price_impact_pct': Decimal,
        }
        """
        return [impact_row_for_amount(self.pair, token_in, amount_in) for amount_in in sizes]

    def find_max_size_for_impact(self, token_in: Token, max_impact_pct: Decimal) -> int:
        """
        Binary search to find largest trade with impact <= max_impact_pct.
        """
        target_impact_decimal = max_impact_pct / Decimal("100")

        # Determine the reserve of the input token to set a safe upper bound
        if token_in == self.pair.token0:
            reserve_in = self.pair.reserve0
        elif token_in == self.pair.token1:
            reserve_in = self.pair.reserve1
        else:
            raise ValueError(f"Token: {token_in} is not a valid token for this pair")

        low = 0
        # A safe upper bound: trading 100x the reserve results in roughly a 99% price impact
        high = reserve_in * 100
        best_amount = 0

        # Binary search loop
        for _ in range(60):  # log2(10^18) = 60
            if low >= high:
                break

            mid = (low + high) // 2

            current_impact = self.pair.get_price_impact(mid, token_in)

            if current_impact <= target_impact_decimal:
                # Impact is acceptable
                best_amount = mid
                low = mid + 1
            else:
                # Impact is too high
                high = mid - 1

        return best_amount

    _ETH_SYMBOLS = frozenset({"ETH", "WETH", "wETH"})

    def _find_eth_reserve(self) -> tuple[int, bool] | None:
        """Identify which side of the pair is ETH/WETH.

        Returns (eth_reserve, is_token0) or None.
        """
        if self.pair.token0.symbol in self._ETH_SYMBOLS:
            return self.pair.reserve0, True
        if self.pair.token1.symbol in self._ETH_SYMBOLS:
            return self.pair.reserve1, False
        return None

    def estimate_true_cost(
        self,
        amount_in: int,
        token_in: Token,
        gas_price_gwei: int,
        gas_estimate: int = 150000,
        eth_price_in_output: int | None = None,
    ) -> dict:
        """
        Returns total cost including gas:
        {
            'gross_output': int,
            'gas_cost_eth': int,  # wei (10**18 wei = 1 ETH)
            'gas_cost_in_output_token': int,
            'net_output': int,
            'effective_price': Decimal,
        }

        Gas is always paid in ETH.  To express it in output-token units
        the function resolves an ETH→output-token rate in this order:
          1. Explicit eth_price_in_output (raw output-token units per
             1 whole ETH / 10**18 wei) — used when provided.
          2. Auto-detected from the pair when one side is ETH/WETH.
          3. Raises ValueError when neither is available.
        """
        if amount_in <= 0:
            raise ValueError(f"Amount in must be greater than 0, got {amount_in}")

        if token_in == self.pair.token0:
            reserve_in = self.pair.reserve0
            reserve_out = self.pair.reserve1
        elif token_in == self.pair.token1:
            reserve_in = self.pair.reserve1
            reserve_out = self.pair.reserve0
        else:
            raise ValueError(f"Token: {token_in} is not a valid token for this pair")

        gross_output = self.pair.get_amount_out(amount_in, token_in)

        gas_cost_wei = gas_price_gwei * gas_estimate * 10**9

        if eth_price_in_output is not None:
            gas_cost_in_output_token = gas_cost_wei * eth_price_in_output // 10**18
        else:
            eth_info = self._find_eth_reserve()
            if eth_info is None:
                raise ValueError(
                    "Neither pair token is ETH/WETH — "
                    "pass eth_price_in_output (raw output-token units per 10**18 wei)"
                )
            if token_in.symbol in self._ETH_SYMBOLS:
                gas_cost_in_output_token = gas_cost_wei * reserve_out // reserve_in
            else:
                gas_cost_in_output_token = gas_cost_wei

        net_output = gross_output - gas_cost_in_output_token

        effective_price = Decimal(net_output) / Decimal(amount_in)

        return {
            "gross_output": gross_output,
            "gas_cost_eth": gas_cost_wei,
            "gas_cost_in_output_token": gas_cost_in_output_token,
            "net_output": net_output,
            "effective_price": effective_price,
        }
