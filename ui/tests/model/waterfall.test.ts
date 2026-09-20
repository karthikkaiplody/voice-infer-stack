import { describe, expect, it } from "vitest";
import type { StageKey } from "../../src/contract/events";
import { epochFixtureEvents, fixtureEvents, rawFixtureEvents, turnOf, type FixtureName } from "../helpers/fixtures";
import { parseEvent } from "../../src/contract/events";
import { buildWaterfall, formatDuration, niceTicks, percent, type WaterfallContext } from "../../src/model/waterfall";

const ALL: Record<StageKey, boolean> = {
  user_speech: true, endpointing: true, stt: true, retrieval: true, llm: true, tools: true, tts: true, output_transport: true,
};
const fixtureCtx: WaterfallContext = { mode: "fixture", stages: ALL, sourceEnded: false, vadStopSecs: 0.4 };
const liveCtx: WaterfallContext = { mode: "live", stages: { ...ALL, tools: false }, sourceEnded: false, vadStopSecs: 0.4 };

const build = (name: FixtureName, ctx = fixtureCtx, count?: number) => {
  const events = fixtureEvents(name);
  return buildWaterfall(turnOf(events.slice(0, count ?? events.length)), ctx);
};
const row = (wf: ReturnType<typeof build>, key: string) => wf.rows.find((r) => r.key === key)!;
const summary = (wf: ReturnType<typeof build>) =>
  Object.fromEntries(wf.rows.map((r) => [r.key.startsWith("tool:") ? "tool" : r.key, r.status]));

describe("measured stages come from recorded timestamps", () => {
  it("normal turn: every stage but tools is measured, relative to speech ending", () => {
    const wf = build("normal-completed");
    expect(row(wf, "user_speech")).toMatchObject({ status: "measured", startMs: -800, endMs: 0, durationMs: 800 });
    expect(row(wf, "endpointing")).toMatchObject({ status: "measured", startMs: 0, endMs: 130, durationMs: 130 });
    expect(row(wf, "stt")).toMatchObject({ status: "measured", startMs: 10, endMs: 120, durationMs: 110, markerMs: 70 });
    expect(row(wf, "llm")).toMatchObject({ status: "measured", startMs: 140, endMs: 210, durationMs: 70 });
    expect(row(wf, "tts")).toMatchObject({ status: "measured", startMs: 220, endMs: 300, durationMs: 80 });
    expect(row(wf, "output_transport")).toMatchObject({ status: "measured", startMs: 300, endMs: 320, durationMs: 20, markerMs: 320 });
    expect(row(wf, "tools").status).toBe("not_in_scenario");
    expect(wf.slowest).toEqual({ label: "Endpointing", latencyMs: 130 });
  });

  it("slow blocking tool: the tool is the slowest stage and holds first audio back", () => {
    const wf = build("slow-blocking-tool");
    const tool = wf.rows.find((r) => r.stage === "tools")!;
    expect(tool).toMatchObject({ label: "Tool: synthetic_lookup (attempt 1)", status: "measured", startMs: 160, endMs: 3170, durationMs: 3010 });
    expect(tool.notes).toEqual(["ended: completed", "1 progress update"]);
    expect(row(wf, "output_transport").endMs).toBe(3270);
    expect(wf.slowest).toEqual({ label: "Tool: synthetic_lookup (attempt 1)", latencyMs: 3010 });
  });

  it("interrupted turn: audio was accepted before the interruption", () => {
    const wf = build("interrupted");
    expect(row(wf, "output_transport")).toMatchObject({ status: "measured", endMs: 270 });
    expect(wf.slowest).toEqual({ label: "Language model", latencyMs: 80 });
  });

  it("failed tool: one row per attempt, and no audio stages", () => {
    const wf = build("failed-tool");
    const tools = wf.rows.filter((r) => r.stage === "tools");
    expect(tools.map((t) => [t.label, t.durationMs, t.notes])).toEqual([
      ["Tool: synthetic_lookup (attempt 1)", 90, ["ended: error"]],
      ["Tool: synthetic_lookup (attempt 2)", 90, ["ended: error"]],
    ]);
    expect(row(wf, "tts").status).toBe("not_in_scenario");
    expect(row(wf, "output_transport").status).toBe("not_in_scenario");
  });
});

describe("a stage without timestamps is never given a duration", () => {
  it("failed tool: the model started but never produced a first token", () => {
    const llm = row(build("failed-tool"), "llm");
    expect(llm.status).toBe("started_no_end");
    expect(llm).toMatchObject({ endMs: null, durationMs: null, latencyMs: null });
  });

  it("no row has a number unless both of its timestamps were observed", () => {
    for (const name of ["normal-completed", "slow-blocking-tool", "interrupted", "failed-tool"] as const) {
      for (const r of build(name).rows) {
        if (r.status !== "measured") {
          expect([r.durationMs, r.latencyMs], `${name} ${r.key}`).toEqual([null, null]);
        }
      }
    }
  });

  it("fixtures that omit a stage say so, rather than drawing it", () => {
    expect(summary(build("slow-blocking-tool"))).toMatchObject({
      endpointing: "not_in_scenario", stt: "not_in_scenario", tts: "measured",
    });
  });

  it("formats an instant honestly instead of as 0 ms", () => {
    expect(formatDuration(0)).toBe("instant");
    expect(formatDuration(0.3)).toBe("<1 ms");
    expect(formatDuration(320)).toBe("320 ms");
    expect(formatDuration(3010.4)).toBe("3,010 ms");
  });
});

describe("live source", () => {
  it("shows tools as not instrumented, always", () => {
    expect(row(build("normal-completed", liveCtx), "tools").status).toBe("not_instrumented");
    expect(row(build("normal-completed", liveCtx, 3), "tools").status).toBe("not_instrumented");
  });

  it("mid-turn: finished stages are measured, running ones in progress, the rest waiting", () => {
    // user_speech.started, .ended, endpointing.started, stt.started, stt.first
    expect(summary(build("normal-completed", liveCtx, 5))).toEqual({
      user_speech: "measured", endpointing: "in_progress", stt: "in_progress", retrieval: "waiting",
      llm: "waiting", tools: "not_instrumented", tts: "waiting", output_transport: "waiting",
    });
  });

  it("before any event every instrumented stage waits and tools are still not instrumented", () => {
    const wf = buildWaterfall(null, liveCtx);
    expect(summary(wf)).toEqual({
      user_speech: "waiting", endpointing: "waiting", stt: "waiting", retrieval: "waiting", llm: "waiting",
      tools: "not_instrumented", tts: "waiting", output_transport: "waiting",
    });
    expect(wf.slowest).toBeNull();
  });

  it("an instrumented stage that never reported in a finished live turn is 'not observed'", () => {
    const wf = build("interrupted", liveCtx);
    expect(row(wf, "stt").status).toBe("not_observed");
    expect(row(wf, "tools").status).toBe("not_instrumented");
  });

  it("a live turn whose source stopped mid-flight reports what it never finished", () => {
    const wf = build("normal-completed", { ...liveCtx, sourceEnded: true }, 5);
    expect(row(wf, "stt").status).toBe("started_no_end");
    expect(row(wf, "llm").status).toBe("not_observed");
  });
});

describe("speech-to-text overlaps speaking", () => {
  it("counts only the time after speech ended as latency", () => {
    const events = fixtureEvents("normal-completed").map((e) =>
      e.event_name === "stt.started" ? { ...e, timestamp_ns: 1_000_000_000 } : e);   // 800 ms before speech ended
    const stt = row(buildWaterfall(turnOf(events), fixtureCtx), "stt");
    expect(stt).toMatchObject({ startMs: -800, endMs: 120, durationMs: 920, latencyMs: 120 });
    expect(stt.notes).toContain("counts only the time after you stopped talking");
  });

  it("does not credit the time the user spent talking as the slowest stage", () => {
    const events = fixtureEvents("normal-completed").map((e) =>
      e.event_name === "stt.started" ? { ...e, timestamp_ns: 100_000_000 } : e);
    const wf = buildWaterfall(turnOf(events), fixtureCtx);
    expect(wf.slowest?.label).not.toBe("Speech to text");
  });
});

describe("endpointing and voice activity are visible", () => {
  it("labels the VAD row, and the endpointing row with its policy and configured silence", () => {
    const wf = build("normal-completed");
    expect(row(wf, "user_speech").label).toBe("Voice activity (VAD)");
    expect(row(wf, "endpointing").notes).toEqual(["policy: vad_timeout", "configured VAD silence: 400 ms"]);
  });

  it("omits the configured-silence note when the configuration does not carry it", () => {
    const wf = build("normal-completed", { ...fixtureCtx, vadStopSecs: null });
    expect(row(wf, "endpointing").notes).toEqual(["policy: vad_timeout"]);
  });
});

describe("axis", () => {
  it("is zero at speech ending and ignores the trailing terminal event", () => {
    const { axis } = build("normal-completed");
    expect(axis.originLabel).toBe("user speech ended");
    // Data spans −800 … +320 ms; the axis adds 4% of that span on each side, and
    // the trailing turn.completed (at +800 ms) does not stretch it.
    expect(axis.minMs).toBeCloseTo(-800 - 0.04 * 1120, 6);
    expect(axis.maxMs).toBeCloseTo(320 + 0.04 * 1120, 6);
    expect(axis.ticks).toContain(0);
    expect(axis.ticks.every((t) => t >= -800 && t <= 320)).toBe(true);
  });

  it("places every bar inside the lane", () => {
    for (const name of ["normal-completed", "slow-blocking-tool", "interrupted", "failed-tool"] as const) {
      const wf = build(name);
      for (const r of wf.rows) {
        for (const ms of [r.startMs, r.endMs, r.markerMs]) {
          if (ms === null) continue;
          expect(percent(wf.axis, ms)).toBeGreaterThanOrEqual(-1e-9);
          expect(percent(wf.axis, ms)).toBeLessThanOrEqual(100 + 1e-9);
        }
      }
    }
  });

  it("falls back to the first event when speech end was never observed", () => {
    const events = fixtureEvents("normal-completed").filter((e) => e.event_name !== "user_speech.ended");
    expect(buildWaterfall(turnOf(events), fixtureCtx).axis.originLabel).toBe("first event");
  });

  it("picks round ticks", () => {
    expect(niceTicks(0, 320)).toEqual([0, 50, 100, 150, 200, 250, 300]);
    expect(niceTicks(-800, 3270).every((t) => t % 500 === 0)).toBe(true);
    expect(niceTicks(0, 1)).toEqual([0, 1]);
  });
});

describe("determinism", () => {
  it("is independent of event arrival order", () => {
    const events = fixtureEvents("slow-blocking-tool");
    expect(buildWaterfall(turnOf([...events].reverse()), fixtureCtx))
      .toEqual(buildWaterfall(turnOf(events), fixtureCtx));
  });
});

describe("epoch-nanosecond timestamps, as produced live", () => {
  it.each(["normal-completed", "slow-blocking-tool", "interrupted", "failed-tool"] as const)(
    "%s gives the same rows and metric as the small-timestamp fixture", (name) => {
      const small = buildWaterfall(turnOf(fixtureEvents(name)), fixtureCtx);
      const big = buildWaterfall(turnOf(epochFixtureEvents(name)), fixtureCtx);
      expect(big.rows.map((r) => r.status)).toEqual(small.rows.map((r) => r.status));
      for (const [i, r] of big.rows.entries()) {
        for (const key of ["startMs", "endMs", "durationMs", "markerMs"] as const) {
          const a = r[key], b = small.rows[i]![key];
          if (a === null || b === null) expect(a).toBe(b);
          else expect(a).toBeCloseTo(b, 2);      // within 10 µs: doubles resolve ~256 ns here
        }
      }
      expect(big.slowest?.label).toBe(small.slowest?.label);
    });
});

describe("the knowledge lookup row (contract 1.1.0)", () => {
  /** The grounded-answer fixture, with the lookup's outcome replaced. */
  const groundedWith = (attributes: Record<string, unknown>) =>
    (rawFixtureEvents("grounded-answer") as any[])
      .map((e) => (e.event_name === "retrieval.completed" ? { ...e, attributes } : e))
      .map((e) => parseEvent(e)!);

  it("is measured from the fixture, with the count and best match as notes", () => {
    const wf = build("grounded-answer");
    const r = row(wf, "retrieval");
    expect(r.status).toBe("measured");
    expect(r.durationMs).toBeCloseTo(0.4, 3);   // a local search: well under a millisecond
    expect(r.notes).toEqual(["2 notes matched", "best match: library.opening-hours"]);
    expect(wf.rows.map((x) => x.key).slice(0, 5)).toEqual(["user_speech", "endpointing", "stt", "retrieval", "llm"]);
  });

  it("finishes before the model is asked, and adds the wait to nothing else", () => {
    const wf = build("grounded-answer");
    expect(row(wf, "retrieval").endMs!).toBeLessThanOrEqual(row(wf, "llm").startMs!);
  });

  it("says when nothing matched, and never invents a source", () => {
    const r = row(buildWaterfall(turnOf(groundedWith({ "retrieval.match_count": 0, "retrieval.method": "bm25" })), fixtureCtx), "retrieval");
    expect(r.notes).toEqual(["no notes matched"]);
  });

  it("is not in the scenario for an older trace, and not instrumented when the agent has no knowledge", () => {
    expect(row(build("normal-completed"), "retrieval").status).toBe("not_in_scenario");
    const none: WaterfallContext = { ...liveCtx, stages: { ...liveCtx.stages, retrieval: false } };
    expect(row(build("normal-completed", none), "retrieval").status).toBe("not_instrumented");
  });
});
