import { describe, expect, it } from "vitest";
import type { Hello, ServerMessage } from "../../src/contract/events";
import { deriveState, outcomeOf, primaryMetric } from "../../src/model/turn";
import { fixtureEvents, fixtureHello, fold, replayState, telemetry } from "../helpers/fixtures";
import { configurationMessage, liveHello } from "../helpers/tuning";
import { initialState, reducer } from "../../src/state/reducer";
import { outcomeView } from "../../src/state/selectors";

const hello = (): Hello => fixtureHello();
const reset = (scenario: string): ServerMessage => ({ kind: "replay_reset", scenario });

describe("replay", () => {
  it("ends a normal replay Completed with the measured metric", () => {
    const state = replayState("normal-completed");
    expect(deriveState(state.turn)).toBe("Completed");
    expect(primaryMetric(state.turn)).toEqual({ status: "measured", ms: 320 });
    expect(outcomeView(state)).toEqual({ kind: "observed", outcome: "completed", classification: null });
  });

  it("carries the failure's reason code on a failed replay", () => {
    expect(outcomeView(replayState("failed-tool"))).toEqual({
      kind: "observed", outcome: "failed", classification: "tool_unavailable",
    });
  });

  it("is deterministic: the same messages always give the same state", () => {
    expect(replayState("slow-blocking-tool")).toEqual(replayState("slow-blocking-tool"));
  });

  it("can replay the same scenario again from the start", () => {
    const first = replayState("interrupted");
    const again = fold([reset("interrupted"), ...fixtureEvents("interrupted").map(telemetry)], first);
    expect(again.turn).toEqual(first.turn);
    expect(again.previousWithoutOutcome).toBe(false);
  });
});

describe("a new turn clears stale state", () => {
  const [a, b] = [fixtureEvents("normal-completed"), fixtureEvents("interrupted")];

  it("replaces the turn and marks an unfinished one as having no outcome", () => {
    const state = fold([...a.slice(0, 6).map(telemetry), telemetry(b[0]!)], fold([hello()]));
    expect(state.turn?.turnId).toBe(b[0]!.identities.turn_id);
    expect(state.turn?.events).toHaveLength(1);
    expect(state.previousWithoutOutcome).toBe(true);
    expect(deriveState(state.turn)).toBe("Listening");
    expect(primaryMetric(state.turn)).toEqual({ status: "waiting" });
  });

  it("does not flag a previous turn that did finish", () => {
    const state = fold([...a.map(telemetry), telemetry(b[0]!)], fold([hello()]));
    expect(state.previousWithoutOutcome).toBe(false);
  });

  it("ignores late events from an earlier turn", () => {
    const state = fold([
      ...a.slice(0, 3).map(telemetry), telemetry(b[0]!), telemetry(a[3]!), telemetry(a[4]!),
    ], fold([hello()]));
    expect(state.turn?.turnId).toBe(b[0]!.identities.turn_id);
    expect(state.turn?.events).toHaveLength(1);
  });

  it("does not let the first turn's numbers leak into the second", () => {
    const state = fold([...a.map(telemetry), ...b.map(telemetry)], fold([hello()]));
    expect(primaryMetric(state.turn)).toEqual({ status: "measured", ms: 270 });
    expect(outcomeOf(state.turn)).toBe("interrupted");
  });
});

describe("duplicate and out-of-place events", () => {
  const events = fixtureEvents("normal-completed");

  it("ignores a repeated event", () => {
    const once = fold([...events.slice(0, 4).map(telemetry)]);
    const twice = fold([...events.slice(0, 4).map(telemetry), telemetry(events[3]!)]);
    expect(twice.turn?.events).toHaveLength(once.turn!.events.length);
  });

  it("ignores anything after a terminal outcome", () => {
    const done = replayState("normal-completed");
    const late = reducer(done, { type: "message", message: telemetry({ ...events[4]!, event_id: "evt_late" }) });
    expect(late).toBe(done);
  });
});

describe("no outcome observed", () => {
  const events = fixtureEvents("normal-completed").slice(0, 7);

  it("is in progress while the source is running", () => {
    const state = fold([hello(), reset("normal-completed"), ...events.map(telemetry)]);
    expect(outcomeView(state)).toEqual({ kind: "in_progress" });
  });

  it("is reported when a replay finishes without a terminal event", () => {
    const state = fold([hello(), reset("normal-completed"), ...events.map(telemetry), { kind: "replay_finished", scenario: "normal-completed" }]);
    expect(outcomeView(state)).toEqual({ kind: "no_outcome_observed" });
  });

  it("is reported when the live pipeline stops or errors mid-turn", () => {
    const running = fold([{ ...hello(), mode: "live", running: true }, ...events.map(telemetry)]);
    expect(outcomeView(fold([{ kind: "stopped" }], running))).toEqual({ kind: "no_outcome_observed" });
    expect(outcomeView(fold([{ kind: "error", classification: "transport_error" }], running)))
      .toEqual({ kind: "no_outcome_observed" });
  });

  it("is remembered for the turn before when a replay is reset", () => {
    const state = fold([reset("interrupted")], fold([hello(), reset("normal-completed"), ...events.map(telemetry)]));
    expect(state.turn).toBeNull();
    expect(state.previousWithoutOutcome).toBe(true);
  });

  it("is not claimed when there is no turn", () => {
    expect(outcomeView(initialState)).toEqual({ kind: "none" });
  });
});

describe("connection and errors", () => {
  it("keeps the turn on screen while reconnecting", () => {
    const state = reducer(replayState("normal-completed"), { type: "connection", status: "reconnecting" });
    expect(state.connection).toBe("reconnecting");
    expect(deriveState(state.turn)).toBe("Completed");
  });

  it("a fresh hello resets the turn, and flags an unfinished one", () => {
    const partial = replayState("normal-completed", 5);
    const state = fold([hello()], partial);
    expect(state.turn).toBeNull();
    expect(state.previousWithoutOutcome).toBe(true);
    expect(state.connection).toBe("open");
  });

  it("turns a pipeline error into a fixed sentence built from a controlled code", () => {
    const state = fold([{ kind: "error", classification: "transport_error" }]);
    expect(state.runtime).toBe("error");
    expect(state.notice).toBe("The local pipeline reported an error (transport_error).");
  });

  it("a hello that says the pipeline is running shows it as listening", () => {
    expect(fold([{ ...hello(), mode: "live", running: true }]).runtime).toBe("listening");
  });
});

describe("losing the local server", () => {
  const listening = () => fold([{ ...hello(), mode: "live", running: true }, ...fixtureEvents("normal-completed").slice(0, 5).map(telemetry)]);

  it("stops claiming the pipeline is listening, and reports the unfinished turn", () => {
    const lost = reducer(listening(), { type: "connection", status: "reconnecting" });
    expect(lost.runtime).toBe("stopped");
    expect(outcomeView(lost)).toEqual({ kind: "no_outcome_observed" });
  });

  it("recovers from the next hello", () => {
    const back = fold([{ ...hello(), mode: "live", running: false }], reducer(listening(), { type: "connection", status: "reconnecting" }));
    expect(back.connection).toBe("open");
    expect(back.runtime).toBe("idle");
  });
});

describe("saving tuning", () => {
  it("a configuration message updates the snapshot and the saved values, keeping the turn", () => {
    const before = fold([liveHello(), ...fixtureEvents("normal-completed").map(telemetry)]);
    const after = fold([configurationMessage({ vad_stop_secs: 0.25 })], before);
    expect(after.hello?.configuration.snapshot_id).toBe("config_tuned");
    expect(after.hello?.tuning?.fields.find((f) => f.key === "vad_stop_secs")?.value).toBe(0.25);
    expect(after.hello?.tuning?.fields.find((f) => f.key === "vad_stop_secs")?.launch).toBe(0.4);
    expect(after.turn).toBe(before.turn);
    expect(after.hello?.mode).toBe("live");
  });

  it("is ignored before the server has said hello", () => {
    expect(fold([configurationMessage({ vad_stop_secs: 0.25 })])).toEqual(initialState);
  });
});
