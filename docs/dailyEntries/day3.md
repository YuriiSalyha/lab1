## Day 3 — 09.05.2026

### Numbers
-Starting capital: $102.7
-Ending capital: $102.04
-PnL: $XX
-Trades: 2 (0 wins, 2 losses)
-Win rate: 0%
-Best trade: $-0.02197410431031481851510816109
-Worst trade: $-0.02198905331821787633462982139
-Fees paid: $0,002 (CEX) + $0.042 (DEX) + $0.006 (DEX gas) + 0.03(rebalancing fees)

### What Happened
- I launched again to check if my tx will happen. After it, I changed the bot
to make it accept only positive trades, which I didn't get for half of a day

### Problems Encountered
- Just to break even, in theory with big inventory I need a spread
 of at least 41 bps, which is really hard to catch without any signals

### Changes Made
- DEX and CEX fees are now fetched via etherscan API
- Added tx hash to CSV exports

### Lessons Learned
- I would prefer not to trade on weekends cause other bots are always active
but humans make fewer trades on DEX, which lowers opportunities.

### Tomorrow's Plan
- Launch a script for a whole night, lower expected profit, push trade limit higher
and look for any possible bugs and improvements.
