#!/usr/bin/env python3
"""Render the README chart from published measurements (requires matplotlib)."""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MultipleLocator

ROOT = Path(__file__).resolve().parents[1]
samples = json.loads((ROOT / "docs/research-data/summary-validation-0.5.json").read_text())["samples"]
names = ["short", "history", "technical"]
labels = ["AI news  ·  7:10", "History  ·  55:43", "Transformer  ·  87:05"]
colors = {"legacy": "#99A6B5", "structured": "#109C8C"}
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "text.color": "#243348",
                     "axes.labelcolor": "#64748B", "xtick.color": "#64748B", "ytick.color": "#243348"})
fig, axes = plt.subplots(1, 2, figsize=(14, 5.6), facecolor="#F7F9FC")
fig.subplots_adjust(left=0.16, right=0.94, top=0.72, bottom=0.26, wspace=0.55)
fig.text(0.045, 0.91, "Whole-source understanding, measured", fontsize=22, weight="bold")
fig.text(0.045, 0.845, "Same transcripts. Same model. One complete run per strategy and sample.", fontsize=12, color="#64748B")
for ax, metric, title, unit in zip(axes, ["elapsedSeconds", "reportedTotalTokens"],
                                  ["Summarization time", "Total token usage"], ["Minutes", "Tokens"]):
    ax.set_facecolor("#F7F9FC")
    for strategy, offset in [("legacy", -0.18), ("structured", 0.18)]:
        values = [samples[name][strategy]["elapsedSeconds"] / 60 if metric == "elapsedSeconds"
                  else samples[name][strategy]["usage"][metric] for name in names]
        bars = ax.barh([i + offset for i in range(3)], values, height=0.28, color=colors[strategy],
                       label="Legacy chunks" if strategy == "legacy" else "Whole-source v2", zorder=3)
        ax.bar_label(bars, labels=[f"{v:.2f}" if metric == "elapsedSeconds" else f"{v:,.0f}" for v in values],
                     padding=6, fontsize=10, color="#243348")
    ax.set_yticks(range(3), labels if ax == axes[0] else [""] * 3)
    ax.invert_yaxis()
    ax.set_title(title, loc="left", pad=18, weight="bold", fontsize=14)
    ax.set_xlabel(unit, labelpad=10)
    ax.set_xlim(0, 26 if metric == "elapsedSeconds" else 870_000)
    ax.xaxis.set_major_locator(MultipleLocator(5 if metric == "elapsedSeconds" else 200_000))
    if metric != "elapsedSeconds":
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1000:.0f}k"))
    ax.grid(axis="x", color="#E2E8F0", linewidth=0.8, zorder=0)
    ax.tick_params(length=0, pad=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
handles, legend_labels = axes[0].get_legend_handles_labels()
fig.legend(handles, legend_labels, loc="lower left", bbox_to_anchor=(0.04, 0.06), ncol=2, frameon=False)
fig.text(0.045, 0.025, "2026-10-25  |  deepseek-v4.1-flash · low reasoning  |  Download and transcription excluded; provider cache may vary.",
         fontsize=9, color="#64748B")
target = ROOT / "docs/images/summary-benchmark.png"
fig.savefig(target, dpi=160, facecolor=fig.get_facecolor())
plt.close(fig)
print(target.relative_to(ROOT))
