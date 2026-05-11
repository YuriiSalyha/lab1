# Advanced Strategy Analysis: Cash-and-Carry (Basis Trading)

## Overview

**Cash-and-carry** (future basis trading) is a **delta-neutral** strategy: expected outcome does not depend on directional moves in the underlying token price.

The setup exploits **contango**—a market state where the futures price sits above the spot price. That situation appears often because holding futures typically ties up less capital than holding spot.

---

## Strategy mechanics

### Entry

Simultaneously:

- **Buy** the asset on the **spot** market
- **Sell (short)** a **quarterly** futures contract for the **same notional** size

### The hold (“the wait”)

Both legs move with price, but you are **long spot** and **short futures**, so **net delta stays near zero** and total portfolio value stays comparatively stable.

### Convergence

As futures approach **delivery/expiry**, the **premium** of futures over spot **decays**. At expiry, spot and futures **converge**; the premium goes to **zero**.

### Exit

Close both legs at expiry (or let the contract settle). **Profit** comes from the **premium locked in at entry**—either via favorable spot moves or by effectively selling at the higher futures-implied level as basis collapses.

---

## Formulas

| Concept | Expression |
|--------|------------|
| **PnL** | `PnL = ((Entry price / Exit price) − 1) × token position size` |
| **Basis (%)** | `Basis % = ((Futures − Spot) / Spot) × 100` |
| **Annualized basis** *(rule of thumb)* | `Annualized Basis ≈ Basis % × 4` *(four quarters per year)* |

> **Note:** Annualized predictions are uncertain—especially in volatile macro conditions—but scaling quarterly basis by 4 is the usual back-of-the-envelope annualization.

---

## Capital plan

| Bucket | Amount (USD) |
|--------|----------------|
| Spot | 5,000 |
| Futures margin | 3,000 |
| **Total** | **8,000** |

---

## Risks and mitigations

Risks overlap with those covered in the Week 6 lecture, plus strategy-specific issues:

1. **Negative basis (backwardation)**
   In a very bearish regime, futures can trade **below** spot.
   **Mitigation:** Enter only when **annualized basis > 8%** (per your rule).

2. **Short-side liquidation**
   If spot rallies sharply, **spot gains are often unrealized** while **futures short losses hit margin in real time**. If the futures wallet runs out of USDT margin, the short can be **liquidated**.
   **Mitigation:** An **auto-rebalancer** that moves funds from spot to futures when liquidation price gets **too close** to the current mark price.

3. **Auto-deleveraging (ADL)**
   If the exchange insurance fund is stressed in a flash crash, the venue may **force-close profitable shorts** (which can include yours).
   **Mitigation:** Watch Binance’s **ADL indicator** (e.g. the 5-dot display). If **4–5 dots** are lit, **reduce leverage** manually or move to a **more liquid** contract. **COIN-M** tends to be **more ADL-prone** than **USDT-M**.

---

## Reuse from the existing project

With minor adjustments, you can reuse:

- **Stop orders**
- **Inventory tracker**
- **Logging**

These modules already fit the operational needs of this strategy after small tweaks.

---

## Closing remark

Exact **annual return** forecasts are hard in the current environment; treat **annualized basis** as a **theoretical annualization** of the locked basis, not a guaranteed outcome.
