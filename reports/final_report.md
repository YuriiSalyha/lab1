# Final Report — Spot Arbitrage Lab (Arbitrum)

---

## 1. Configuration & Setup

**Venue stack**

| Setting | Choice |
|--------|--------|
| Chain | Arbitrum |
| DEX | Uniswap V2 |
| CEX | OKX |
| Pair | ETH / USDT |

### Risk parameters — why these numbers

**`ARB_MAX_POSITION_USD=25`**
I believe in the strategy, but even at 25 it was extremely hard to find a profitable deal. For my inventory, that felt like a high but still reasonable cap.

**`ARB_MIN_TRADE_USD=1`**
I needed this for testing negative trades. Without it, the bot would always try to make the smallest possible trades. Gas on Arbitrum is low and my V2 pool fee is relatively high, so any unprofitable trade pushed the bot toward shrinking size to limit losses. In practice it kept picking smaller amounts whenever a trade was expected to lose money.

**`DEX_SLIPPAGE_BPS=50`**
It used to be smaller; once I started hunting for actually profitable deals, I loosened it.

**`ARB_MAX_DAILY_LOSS_USD=2`**
Still a large loss relative to my inventory. If I’m already down ~2 USD, something is wrong in the flow — the bot should stop and I should dig into what happened.

**`ARB_MAX_TRADE_PCT=0.25`**
Goes together with `ARB_MAX_POSITION_USD=25` for the same reasons.

**`ARB_CONSECUTIVE_LOSS_LIMIT=2`**
The bot wasn’t running for that long, so two losses in a row was enough to force me to look at what went wrong.

### What changed from testnet to production

Mostly environment only: I lowered `ARB_CONSECUTIVE_LOSS_LIMIT`, `ARB_MAX_DAILY_LOSS_USD`, and at the start of the week I also lowered `DEX_SLIPPAGE_BPS` and added `ARB_ALLOW_NEGATIVE_PNL_USD`.

---

## 2. Trading Results

| Metric | Value |
|--------|--------|
| Total trades | 11 |
| Win rate | 0% |
| Total PnL | $-0.206 |
| Max drawdown | Less than 0.1% |
| Starting capital | $100 |
| Ending capital | $102.01 |

**Best trade:** about **−$0.01497**. During testing, on **buy CEX → sell DEX** I had a slightly better spread. The 0.3% DEX fee was applied to a lower price, so the fee was slightly smaller, and maker fee is a bit lower than taker. Overall, **buy CEX / sell DEX** tended to look a bit more profitable in my runs.

**Worst trade:** about **−$0.02429**. That was the first trade while I was still testing and fixing production — I was okay losing about two cents once to prove the arb path actually executes end-to-end.

### Fees paid (CEX + DEX + gas)

| Bucket | Approx. |
|--------|---------|
| CEX fees | $0.011 |
| DEX pool / protocol fees | $0.033 |
| DEX gas | $0.22 |

---

## 3. Risk Management in Practice

Several times the **circuit breaker** fired; those episodes lined up with periods when either the CEX or the DEX side wasn’t really workable. I also touched the **kill-switch file** once to confirm behavior.

The most useful guardrail in practice was **`CONSECUTIVE_LOSS_LIMIT`** — it broke the loop when trades were coming through negative.

**Closest call:** I saw a spread around **−44 bps**, but I still couldn’t turn it into a positive trade at my size. If I could have traded **~1 ETH** notionally, it would have been roughly **+$0.60** on paper — but in reality rebalancing would have eaten a big chunk of that, so it’s not as juicy as it sounds on a spreadsheet.

---

## 4. What I Learned

Even **~$1,000** is still a relatively small inventory for this kind of arb bot — but it’s a lot easier to hunt for edge than at micro size. Next iteration I’d **raise max trade toward ~50**, **lower `ARB_MAX_TRADE_PCT` to ~0.15**, and **lower `ARB_CONSECUTIVE_LOSS_LIMIT` to 1** so I can watch slippage behavior with larger clips and catch issues earlier.

I’m most confident in **risk management**. I still have doubts about **slippage in production**: it’s hard to stress-test slippage on a few-dollar trade against hundreds of thousands in pool liquidity — at my parameters it’s basically invisible. I also expect slippage to worsen near the “almost profitable” zone because everyone piles into the same side at the same time.

---

## 5. Technical Challenges

### L2 adaptation issues

**Token naming and inventory** also behaved differently than on a toy setup: USDT on-chain vs what the CEX calls things caused **inventory mismatches** until I generalized handling (e.g. USDT0 vs USDT labels).

### Gas estimation accuracy

Gas on Arbitrum is **cheap per tx**, but **not negligible** versus my trade sizes — **DEX gas dominated** a meaningful slice of the economics (see fees section). Estimates vs reality drift with **congestion**, **router path**. Combined with a **fixed 0.3% V2 fee**, the **break-even spread** moved higher.

### Bugs found in production that didn’t show in testing

- **Inventory not tracking Arbitrum tokens** when the **symbol didn’t match the CEX** — showed up when reconciling real balances, not in simplified fixtures.
- **CEX leg failures** — Due to the agressivness of orders
- **First-live-trade edge cases** while tuning circuit breakers and loss limits — behavior only validated once real money and real latency were in the loop.

---

## 6. Beyond Spot Arbitrage

The advanced strategy I analyzed is **futures basis trading (cash-and-carry)** — a **delta-neutral** approach meant to earn **without betting on spot direction**. It exploits **contango**: futures trade **above** spot.

Execution sketch: **buy spot** and **short equivalent quarterly futures**. Opposite notionals → **directional exposure largely neutralized**. As expiry approaches, **basis tends to converge**; the trader aims to harvest the **locked-in spread**, net of costs.

Profitability hinges on **entry basis** and **annualized carry**, plus **active risk management**: **liquidation** on the futures leg, **negative basis**, and exchange-specific risks like **auto-deleveraging (ADL)**.

### How it connects to what I’ve built

With modest adaptation I could reuse:

- Logging and monitoring
- Inventory and balance tracking
- Trade execution plumbing
- Spread / edge calculation patterns
- Risk controls
- **Rebalancing** ideas across venues

### Would I pursue this after the internship?

I really like this strategy: even though the **profits are relatively small** and it needs **more capital** than, say, a spot arbitrage bot, the **risk is much lower**. I would **probably pursue it after the internship**, but whether it turns into a **real strategy** or stays a **pet project** depends on whether I **get a job in the field**.

At the moment I **cannot realistically run** this kind of strategy on my own. To make it **safe and consistent** long term, I would need **at least ~$8,000** in capital — which I **don’t have right now**. That is why I **would not consider it a viable real-world strategy for myself today**.

If I **do get a job** and get the chance to work on this type of system **professionally**, I would **definitely** want to **implement and improve** it.

---

*End of report.*
