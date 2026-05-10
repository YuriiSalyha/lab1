## Day 2 — 08.05.2026

### Numbers
-Starting capital: $102.4
-Ending capital: $102.7
-PnL: $
-Trades: 2 (0 wins, 4 losses)
-Win rate: 0%
-Best trade: $-0.02497220568
-Worst trade: $-0.02498720568
-Fees paid: $0,004 (CEX) + $0.084 (DEX) + $0.012 (DEX gas)

### What Happened
- I designed a situation that, despite a slight pnl loss, helped me test and fix
my arbitrage bot and its circuit breaker and loss stoppers. My bot managed to perform
2 trades, but since they were 2 losses in a row, my bot successfully stopped itself.

### Problems Encountered
- For some time, my DEX leg was failing cause I moved to another address
of the OKX wallet and since it was the same app,
I forgot to change the private key of the wallet.
- My pool on DEX have relativelly high fee of 0.3% (a standard for V2),
which with maker-taker fee and arb gas pushes spread to 41 bps just to
break even. Even though I deliberately chose the V2 pool, due to the fact
that liquidity is evenly spread across all pools
which allows for bigger changes in price, , contrary to V3
- I encountered a mistake when adding money to my wallet balance.
And I swapped 25 USDT from the Arbitrum network to 25$ WETH but on
The Eth network, which caused me to spend around $1.5 worth of fees
to supply my wallet with eth on eth network and after make a bridge
of WETH on eth to WETH on arb network

### Changes Made
- Migration from SushiSwap to Uniswap

### Lessons Learned
- I've moved on from pool with probably scam token, a copy of sushi
- I also moved from sushiswap to uniswap as DEX with much higher liquidity.
Now I wouldn't spend time researching pools on sushiswap and also wouldn't
waste my time running a bot on a low liquidity pool.
- I've learned that my bot risk management is working correctly. So now, I have higher
trust in my script and I can safely push my maximal trade to

### Tomorrow's Plan
- Launch a script for a longer time and look for any possible bugs
and
