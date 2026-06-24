"""Render Plotly figures as HTML fragments for Jinja templates."""

from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio


def figure_html(fig: go.Figure, *, div_id: str | None = None) -> str:
    return pio.to_html(
        fig,
        full_html=False,
        include_plotlyjs=False,
        config={"displayModeBar": False, "responsive": True},
        div_id=div_id,
    )
