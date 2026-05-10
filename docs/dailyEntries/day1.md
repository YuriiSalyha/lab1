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
- Today I was still running dry-run, fixing bugs and preparing
bot to real trades and polishing risk managment.

### Problems Encountered
- Errors linked to some usage of floats, Inventory was not
showing arbitrum version of USDT and any other token in wallet
if its name is different from the one in CEX.

### Changes Made
- Better display of inventory which fixed bug, of bot
not seeing USDT0 and WETH, more abstract code in relation
to CEXes and removed float to maximum extend from the repository

### Lessons Learned
- Since I didn't even started production run I don't
have much to say for now.

### Tomorrow's Plan
- I will try to carefully run bot on production mode
and if I won't catch any arbitrage possibility,
I'll try to change DEX prices "manually" to see
if my bot gonna cathc this opportunity. Since LP
on my pool is extremelly low I can move prices pretty
dramaticly even with small trades
