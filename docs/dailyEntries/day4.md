## Day 4 — 10.05.2026

### Numbers
-Starting capital: $102.04
-Ending capital: $102,01
-PnL: $XX
-Trades: 5 (0 wins, 5 losses)
-Win rate: 0%
-Best trade: $−0.01497
-Worst trade: $-0.01846
-Fees paid: $0,005 (CEX) + $0.10 (DEX gas) + $0.015 (DEX)

### What Happened
- I once almost got a positive trade. I had a moment with
spread of -44, but I couldn't get a positive trade from this
With my inventory, if I could perform a trade of 1 ETH, this
would have been a positive trade of around 60 cents. It is not
completely so profitable in a real situation, since rebalancing
would eat a big chunk of it
- I tested another direction of arbitrage (buy_cex_sell_dex)
- I again loosen up a bit on the limits to make 5 trades to get through
the threshold of 10 trades in week 6, with a total of 11 trades

### Problems Encountered
- DEX fees dominated economics: fixed 0.3% eat a large share
of any arb.

### Changes Made
- Tokens to pay the gas are now also
included in inventory tracking
- Again, temporal change of env params to allow negative trades

### Lessons Learned
- I'll maybe try not to wait for a profitable trade on v2 pool
and switch to v3 pool with a much smaller fee.

### Tomorrow's Plan
- Since it is the last day, I won't try to change much and just
try running my script for a night, to see if there are
any profitable deals.
