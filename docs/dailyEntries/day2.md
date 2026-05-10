## Day 2 — 08.05.2026

### Numbers
-Starting capital: $102.4
-Ending capital: $102.7
-PnL: $
-Trades: 2 (0 wins, 2 losses)
-Win rate: 0%
-Best trade: $-0.02497220568
-Worst trade: $-0.02498720568
-Fees paid: $0,002 (CEX) + $0.006 (DEX) + $0.042 (DEX gas)

### What Happened
- I designed situation that despite slight pnl loss helped me, testing and fixing
my arbitrage bot and its circuit breaker and loss stoppers. My bot managed to perform
2 trades but since they were 2 losses in a row my bot succesfully stopped itself.

### Problems Encountered
- For some time my DEX leg was failing cause, I moved to another address
of OKX wallet on same
changed everything except for private key of the wallet.
- My pool on DEX have relativelly high fee of 0.3% (a standart for V2),
which with maker-taker fee and arb gas pushes spread to 41 bps just to
break even. Even though, I deliberatly chose V2 pool, due to the fact
that liqudity is evenly spread across all pool contrary to V3
which allows for bigger changes in price
- I encountered mistake when adding money to my wallet balance.
And I swapped 25 USDT from arbitrum network to 25$ WETH but on
Eth network which cause me somewhat around $1.5 worth of fee
to supply my wallet with eth on eth network and after make bridge
of WETH on eth to WETH on arb netwrok

### Changes Made
- Migration from SushiSwap Uniswap

### Lessons Learned
- I've moved on from pool with probably scam token, a copy of sushi
- I also moved from sushiswap to uniswap as DEX with much higher liquidity.
Now I wouldn't spent time on researching pools on sushiswap and also wouldn't
waste my time on running bot on low luqidity pool.
- I've learned that my bot risk management is working correctly. So now, I have higher
trust in my script and I can safely push my maximal trade to

### Tomorrow's Plan
- Lauch a script for a longer time and look for any possible bugs
and
