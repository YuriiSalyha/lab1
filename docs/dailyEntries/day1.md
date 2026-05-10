## Day 1 — 05.05.2026

### Numbers
-Starting capital: $100
-Ending capital: $100
-PnL: no trades
-Trades: 0
-Win rate: no trades
-Best trade: no trades
-Worst trade: no trades
-Fees paid: 0

### What Happened
- Today, I was still running a dry run, fixing bugs and preparing
bot to real trades and polishing risk management.

### Problems Encountered
- Errors linked to some usage of floats
- Inventory was not showing the Arbitrum version of USDT
and any other token in the wallet, if its name was different
from the one in CEX.

### Changes Made
- Better display of inventory which fixed bug, of bot
not seeing USDT0 and WETH, more abstract code in relation
to CEXes and removed float to maximum extend from the repository

### Lessons Learned
- Since I didn't even start the production run, I don't
have much to say for now.

### Tomorrow's Plan
- I will try to carefully run the bot in production mode
and if I won't catch any arbitrage possibility,
I'll try to change DEX prices "manually" to see
if my bot is gonna catch this opportunity. Since liquidity
on my pool is extremely low, I can move prices pretty
dramatically, even with small trades
