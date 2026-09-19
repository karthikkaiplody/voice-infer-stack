"""Read a turn you already ran, as a self-contained HTML page.

The offline counterpart to `live.py`. `make live` lets you talk to the agent and
watch the stages light up; this renders a turn from a trace file, needs no
models and no install, and is what someone reads first after cloning.

    python3 viewer.py                        # committed reference traces
    python3 viewer.py --traces mine.jsonl
    python3 viewer.py --open

Shows a request flowing through the pipeline: you stop speaking, the agent
decides you are done, your audio becomes text, the text becomes a reply, the
reply becomes audio. Each stage is drawn where it actually ran in time, so the
gaps and the overlaps are visible rather than described.

It renders whatever runs are in the file. If the trace holds more than one
configuration it draws each one; that is a consequence of what you measured, not
a comparison this tool is trying to make.

Standard library only, and the data is inlined, so the output is one file that
works offline with no server and no install.
"""

from __future__ import annotations

import argparse
import html
import json
import webbrowser
from pathlib import Path

import analysis
import budget

STAGE_LABEL = {
    "turn_detection": ("Turn detection", "deciding you have finished speaking"),
    "stt": ("Speech to text", "your audio becomes words"),
    "llm": ("Language model", "words become a reply"),
    "tts": ("Text to speech", "the reply becomes audio"),
}


def collect(spans, win):
    """One turn: each stage clipped to the budget window, in milliseconds."""
    lo, hi = win["start_time_ns"], win["end_time_ns"]
    total = (hi - lo) / 1e6
    stages = []
    for name in budget.STAGES:
        ivs = budget.clip(
            [(s["start_time_ns"], s["end_time_ns"])
             for s in spans if s["name"] == name], (lo, hi))
        if not ivs:
            continue
        attrs = next((s["attributes"] for s in spans
                      if s["name"] == name
                      and s["end_time_ns"] > lo and s["start_time_ns"] < hi), {})
        stages.append({
            "name": name,
            "bars": [((a - lo) / 1e6, (b - lo) / 1e6) for a, b in ivs],
            "ms": analysis.duration_sum_ms(ivs),
            "detail": attrs.get("transcript") or attrs.get("output") or "",
            "model": attrs.get("gen_ai.request.model", ""),
        })
    # Endpointing that waits for a transcript produces a turn_detection bar
    # that mirrors the stt bar. Drawn without comment it reads as two stages
    # costing time; it is one wait, counted once in the total.
    note = ""
    byname = {s["name"]: s for s in stages}
    if "turn_detection" in byname and "stt" in byname:
        a, b = byname["turn_detection"], byname["stt"]
        if abs(a["ms"] - b["ms"]) < max(a["ms"], b["ms"]) * 0.2:
            note = ("Turn detection here waits for the transcript, so it "
                    "overlaps speech to text rather than adding to it.")
    return {"mode": win["attributes"].get("mode", "?"),
            "fixture": win["attributes"].get("fixture", ""),
            "total": total, "stages": stages, "note": note,
            "legacy": win["attributes"].get("measured_as")
            != "output_transport_accepted"}


def render(turns, scale_ms):
    css = """
body{margin:0;padding:40px 28px;background:#fbfbfa;color:#1a1a18;
 font:15px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
main{max-width:980px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
p.sub{margin:0 0 34px;color:#78756e}
section{margin-bottom:38px;padding:22px 24px;border:1px solid #e4e2dd;
 border-radius:10px;background:#fff}
h2{font-size:15px;margin:0 0 2px}
h2 span{color:#78756e;font-weight:400}
p.tot{margin:0 0 20px;color:#78756e;font-size:13px}
.legacy{margin:12px 0;color:#9a3412;font-weight:600}
.row{display:grid;grid-template-columns:150px 1fr 74px;gap:12px;
 align-items:center;margin-bottom:11px}
.lbl b{display:block;font-weight:600;font-size:13px}
.lbl i{font-style:normal;color:#8b8880;font-size:11px}
.track{position:relative;height:26px;background:#f4f3f0;border-radius:4px}
.bar{position:absolute;top:0;height:26px;border-radius:4px;
 border:1px solid #2b2a27}
.ms{text-align:right;color:#4a4844;font-size:13px}
.dl{position:absolute;top:-4px;bottom:-4px;width:2px;background:#dc2626}
.legend{margin-top:26px;color:#78756e;font-size:12px}
.detail{margin:4px 0 0 162px;color:#8b8880;font-size:11px;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:640px}
"""
    fill = {"turn_detection": "#ffffff", "stt": "#d9d7d1",
            "llm": "#8d8a83", "tts": "#3f3d39"}
    out = []
    for t in turns:
        rows = []
        for s in t["stages"]:
            label, why = STAGE_LABEL.get(s["name"], (s["name"], ""))
            bars = "".join(
                f'<div class="bar" style="left:{a / scale_ms * 100:.3f}%;'
                f'width:{max(b - a, 1) / scale_ms * 100:.3f}%;'
                f'background:{fill.get(s["name"], "#999")}"></div>'
                for a, b in s["bars"])
            dl = (f'<div class="dl" style="left:{800 / scale_ms * 100:.3f}%"></div>'
                  if 800 < scale_ms else "")
            rows.append(
                f'<div class="row"><div class="lbl"><b>{html.escape(label)}</b>'
                f'<i>{html.escape(why)}</i></div>'
                f'<div class="track">{bars}{dl}</div>'
                f'<div class="ms">{s["ms"]:.0f} ms</div></div>')
            if s["detail"]:
                rows.append(f'<div class="detail">{html.escape(s["detail"][:120])}</div>')
        out.append(
            f'<section><h2>{html.escape(t["mode"])} '
            f'<span>· {html.escape(t["fixture"])}</span></h2>'
            + (f'<p class="legacy">{html.escape(budget.LEGACY_NOTICE)}</p>'
               if t["legacy"] else '')
            + f'<p class="tot">{t["total"]:.0f} ms from the moment you stopped '
            f'speaking to the recorded output boundary</p>'
            f'{"".join(rows)}'
            + (f'<p class="legend">{html.escape(t["note"])}</p>'
               if t.get("note") else "")
            + '</section>')

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Where did my 800 milliseconds go?</title><style>{css}</style></head>
<body><main>
<h1>One turn, end to end</h1>
<p class="sub">Your audio becomes words, the words become a reply, the reply
becomes audio. Every bar sits where that stage actually ran, so bars that start
before the one above finishes were running at the same time. The red line is
800&nbsp;ms, roughly what human turn-taking costs.</p>
{"".join(out)}
<p class="legend">Generated by <code>viewer.py</code> from OpenTelemetry spans.
Nothing here is drawn by hand; re-run the pipeline and regenerate to
see your own numbers.</p>
</main></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--traces", default="artifacts/reference-traces.jsonl")
    ap.add_argument("--out", default="artifacts/viewer.html")
    ap.add_argument("--skip-warmup", type=int, default=1)
    ap.add_argument("--open", action="store_true", help="open it in a browser")
    args = ap.parse_args()

    spans = budget.load_spans(args.traces)
    wins = budget.windows(spans)
    if not wins:
        raise SystemExit(f"no budget windows in {args.traces}")

    # One representative turn per build: the one whose total is the median, so
    # the picture matches the number budget.py prints.
    turns = []
    by_mode = {}
    for w in wins:
        by_mode.setdefault(w["attributes"].get("mode"), []).append(w)
    for mode, ws in by_mode.items():
        ws = ws[args.skip_warmup:] or ws
        totals = [(w["end_time_ns"] - w["start_time_ns"]) / 1e6 for w in ws]
        med = analysis.median(totals)
        chosen = min(ws, key=lambda w:
                     abs((w["end_time_ns"] - w["start_time_ns"]) / 1e6 - med))
        turns.append(collect(spans, chosen))

    turns.sort(key=lambda t: -t["total"])
    scale = max(t["total"] for t in turns) * 1.06

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(turns, scale))
    print(f"  {out}")
    for t in turns:
        print(f"    {t['mode']:<10} {t['total']:.0f} ms")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
