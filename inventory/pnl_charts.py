"""
Historical cumulative PnL chart export (matplotlib or plotly). Optional ``viz`` extra.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from inventory.pnl import PnLEngine

VIZ_INSTALL_HINT = "pip install 'lab1[viz]' or pip install matplotlib plotly"

Backend = Literal["matplotlib", "plotly"]

# Figure sizing (inches for matplotlib; plotly uses pixel-ish layout).
CHART_FIG_WIDTH_IN = 10.0
CHART_FIG_HEIGHT_IN = 5.0
CHART_DPI = 100


def _cumulative_series(engine: PnLEngine) -> tuple[list[datetime], list[float]]:
    trades = sorted(engine.trades, key=lambda t: t.timestamp)
    xs: list[datetime] = []
    ys: list[float] = []
    cum = Decimal("0")
    for t in trades:
        cum += t.net_pnl
        xs.append(t.timestamp)
        ys.append(float(cum))
    return xs, ys


def export_pnl_chart(
    engine: PnLEngine,
    path: str | Path,
    *,
    backend: Backend = "matplotlib",
    title: str = "Cumulative net PnL (USD)",
) -> None:
    """
    Export cumulative net PnL vs time. Requires optional dependencies for the chosen backend.

    - ``matplotlib``: writes PNG.
    - ``plotly``: writes HTML.
    """
    if not engine.trades:
        raise ValueError("no trades in PnLEngine; nothing to chart")

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    xs, ys = _cumulative_series(engine)

    if backend == "matplotlib":
        try:
            import matplotlib

            matplotlib.use("Agg", force=True)
            import matplotlib.pyplot as plt
        except ImportError as e:
            raise ImportError(
                f"matplotlib is required for backend={backend!r}. Install with: {VIZ_INSTALL_HINT}",
            ) from e

        fig, ax = plt.subplots(figsize=(CHART_FIG_WIDTH_IN, CHART_FIG_HEIGHT_IN), dpi=CHART_DPI)
        ax.plot(xs, ys, color="C0", linewidth=1.5)
        ax.set_title(title)
        ax.set_xlabel("Time (UTC)")
        ax.set_ylabel("Cumulative PnL (USD)")
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(out, format="png")
        plt.close(fig)
        return

    if backend == "plotly":
        try:
            import plotly.graph_objects as go
        except ImportError as e:
            raise ImportError(
                f"plotly is required for backend={backend!r}. Install with: {VIZ_INSTALL_HINT}",
            ) from e

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(x=xs, y=ys, mode="lines", name="Cumulative PnL", line=dict(width=2)),
        )
        fig.update_layout(
            title=title,
            xaxis_title="Time (UTC)",
            yaxis_title="Cumulative PnL (USD)",
            width=int(CHART_FIG_WIDTH_IN * CHART_DPI),
            height=int(CHART_FIG_HEIGHT_IN * CHART_DPI),
        )
        fig.write_html(str(out))
        return

    raise ValueError(f"unknown backend: {backend!r}")
