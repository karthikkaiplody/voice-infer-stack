"""Render the latency waterfall for the talk.

    uv run python chart.py                       # both builds, from committed traces
    uv run python chart.py --traces mine.jsonl

Produces CHT-01 (naive), CHT-02 (streaming) and CHT-03 (both, side by side).
All three share one x-axis scale, so bar lengths are comparable by eye across
slides. That is the whole argument of the talk: the bars do not get shorter,
they stop queuing.
"""

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

import analysis
import budget as budget_mod

OUT = Path("artifacts/charts")
DEADLINE_MS = budget_mod.BUDGET_MS

# Deadline red is used for ONE thing and never decoratively.
RED = "#dc2626"
INK = "#111827"
GREY = "#9ca3af"
STAGE_STYLE = {
    "turn_detection": ("#ffffff", "waiting, no compute"),
    "stt": ("#d1d5db", "speech to text"),
    "llm": ("#6b7280", "language model"),
    "tts": ("#374151", "speech synthesis"),
}
ORDER = ["turn_detection", "stt", "llm", "tts"]


def turns(spans, mode):
    """Every budget window for one build, with its stage intervals."""
    out = []
    for win in budget_mod.windows(spans):
        if win["attributes"].get("mode") != mode:
            continue
        w = (win["start_time_ns"], win["end_time_ns"])
        stages = {}
        for name in ORDER:
            ivs = budget_mod.clip(
                [(s["start_time_ns"], s["end_time_ns"])
                 for s in spans if s["name"] == name], w)
            if ivs:
                stages[name] = [((a - w[0]) / 1e6, (b - w[0]) / 1e6) for a, b in ivs]
        out.append({"total_ms": (w[1] - w[0]) / 1e6, "stages": stages,
                    "attrs": win["attributes"]})
    return out


def pick(runs, skip_warmup=1):
    """The run whose total is the median, so the picture matches the number."""
    runs = runs[skip_warmup:] or runs
    med = analysis.median([r["total_ms"] for r in runs])
    return min(runs, key=lambda r: abs(r["total_ms"] - med))


def draw(ax, run, title, xmax, uncertain=()):
    """One waterfall. Bars sit where the stage actually ran in time."""
    rows = [s for s in ORDER if s in run["stages"]]
    for y, name in enumerate(rows):
        colour, _ = STAGE_STYLE[name]
        # A row whose provenance differs between builds is drawn hatched, so it
        # is never read as directly comparable to the other build's row.
        hatch = "///" if name in uncertain else None
        for start, end in run["stages"][name]:
            ax.barh(y, end - start, left=start, height=0.62,
                    color=colour, edgecolor=INK, linewidth=0.9, zorder=3,
                    hatch=hatch)
        total = sum(e - s for s, e in run["stages"][name])
        last = max(e for _, e in run["stages"][name])
        label = f"{total:.0f}" + ("  *" if name in uncertain else "")
        ax.text(last + xmax * 0.012, y, label,
                va="center", ha="left", fontsize=11, color=INK, zorder=4)

    ax.axvline(DEADLINE_MS, color=RED, linewidth=2, zorder=5)
    ax.text(DEADLINE_MS, -0.72, f"{DEADLINE_MS:.0f} ms ",
            color=RED, fontsize=11, va="center", ha="right", zorder=6)

    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([n.replace("_", " ") for n in rows], fontsize=12)
    ax.set_ylim(len(rows) - 0.4, -0.9)
    ax.set_xlim(0, xmax)
    ax.set_xlabel("milliseconds after the user stopped speaking", fontsize=11)
    ax.set_title(f"{title}    {run['total_ms']:.0f} ms",
                 fontsize=14, loc="left", pad=12)
    ax.grid(axis="x", color=GREY, alpha=0.25, zorder=0)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    for ext in ("svg", "png"):
        fig.savefig(OUT / f"{name}.{ext}", bbox_inches="tight", dpi=200,
                    facecolor="white")
    plt.close(fig)
    print(f"  {OUT / name}.svg / .png")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--traces", default="artifacts/reference-traces.jsonl")
    args = ap.parse_args()

    spans = budget_mod.load_spans(args.traces)
    picked = {}
    for mode in ("naive", "streaming"):
        runs = turns(spans, mode)
        if runs:
            picked[mode] = pick(runs)
    if not picked:
        raise SystemExit(f"no runs in {args.traces}")

    # ONE scale for every chart in the deck. Non-negotiable: if the axes differ,
    # the audience cannot compare bar lengths between slides, and comparing bar
    # lengths is the entire point.
    xmax = max(r["total_ms"] for r in picked.values()) * 1.18

    titles = {"naive": "Build 1 - sequential",
              "streaming": "Build 2 - streaming"}
    # See config.py: streaming observes end-of-turn differently from naive and
    # the difference is not yet attributed. Hatched, starred, and footnoted
    # rather than quietly plotted as if the two were the same measurement.
    uncertain = {"naive": (), "streaming": ("turn_detection",)}
    print()
    for mode, run in picked.items():
        fig, ax = plt.subplots(figsize=(11, 3.4))
        draw(ax, run, titles[mode], xmax, uncertain[mode])
        save(fig, f"CHT-{'01' if mode == 'naive' else '02'}-{mode}")

    if len(picked) == 2:
        fig, axes = plt.subplots(2, 1, figsize=(11, 7),
                                 gridspec_kw={"hspace": 0.55})
        for ax, mode in zip(axes, ("naive", "streaming")):
            draw(ax, picked[mode], titles[mode], xmax, uncertain[mode])
        handles = [Patch(facecolor=STAGE_STYLE[s][0], edgecolor=INK,
                         label=STAGE_STYLE[s][1]) for s in ORDER]
        handles.append(Patch(facecolor="none", edgecolor=RED,
                             label=f"{DEADLINE_MS:.0f} ms human window"))
        axes[-1].legend(handles=handles, loc="upper center", ncol=5,
                        bbox_to_anchor=(0.5, -0.46), frameon=False, fontsize=10)
        fig.text(0.5, 0.015,
                 "*  measured differently in the two builds, not directly "
                 "comparable - see config.py",
                 ha="center", fontsize=9, color=GREY)
        save(fig, "CHT-03-both")

        a = picked["naive"]["total_ms"]
        b = picked["streaming"]["total_ms"]
        print(f"\n  {a:.0f} ms -> {b:.0f} ms  ({a / b:.1f}x)")
        print("  Same models, same audio. Only the scheduling differs.\n")


if __name__ == "__main__":
    main()
