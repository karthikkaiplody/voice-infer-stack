import { describe, expect, it } from "vitest";
import type { TelemetryEvent } from "../../src/contract/events";
import { fixtureEvents, turnOf, type FixtureName } from "../helpers/fixtures";
import { deriveState, eventLog, formatMs, outcomeOf, primaryMetric } from "../../src/model/turn";

/** The state after each successive event, as a live page would show it. */
const progression = (name: FixtureName) =>
  fixtureEvents(name).map((_, i, all) => deriveState(turnOf(all.slice(0, i + 1))));

describe("primary metric: speech ended → output transport accepted first audio", () => {
  it.each([
    ["normal-completed", 320],
    ["slow-blocking-tool", 3270],
    ["interrupted", 270],
  ] as const)("%s measures %d ms from the recorded timestamps", (name, ms) => {
    expect(primaryMetric(turnOf(fixtureEvents(name)))).toEqual({ status: "measured", ms });
  });

  it("is unavailable, with a reason, when no audio was accepted", () => {
    const metric = primaryMetric(turnOf(fixtureEvents("failed-tool")));
    expect(metric).toEqual({
      status: "unavailable",
      reason: "The output transport accepted no audio in this turn.",
    });
  });

  it("waits, rather than reporting a number, while the turn is still running", () => {
    const events = fixtureEvents("normal-completed");
    expect(primaryMetric(turnOf(events.slice(0, 5)))).toEqual({ status: "waiting" });
    expect(primaryMetric(null)).toEqual({ status: "waiting" });
  });

  it("is independent of event arrival order", () => {
    const events = fixtureEvents("normal-completed");
    const shuffled = [...events].reverse();
    expect(primaryMetric(turnOf(shuffled))).toEqual(primaryMetric(turnOf(events)));
  });

  it("does not invent a start when speech end was never observed", () => {
    const events = fixtureEvents("normal-completed").filter((e) => e.event_name !== "user_speech.ended");
    expect(primaryMetric(turnOf(events))).toEqual({
      status: "unavailable",
      reason: "The end of user speech was not observed.",
    });
  });

  it("is unavailable, not negative, when timestamps disagree", () => {
    const events = fixtureEvents("normal-completed").map((e) =>
      e.event_name === "output_transport.first_audio" ? { ...e, timestamp_ns: 1 } : e,
    );
    expect(primaryMetric(turnOf(events)).status).toBe("unavailable");
  });

  it("formats whole milliseconds with a unit", () => {
    expect(formatMs(320)).toBe("320 ms");
    expect(formatMs(3270.4)).toBe("3,270 ms");
  });
});

describe("turn state follows what has been observed", () => {
  it("normal turn", () => {
    expect(progression("normal-completed")).toEqual([
      "Listening",      // user_speech.started
      "Endpointing",    // user_speech.ended
      "Endpointing",    // endpointing.started
      "Transcribing",   // stt.started
      "Transcribing",   // stt.first
      "Endpointing",    // stt.final, endpointing has not resolved yet
      "Thinking",       // endpointing.resolved
      "Thinking",       // llm.started
      "Thinking",       // llm.first_token
      "Speaking",       // tts.started
      "Speaking",       // tts.first_synthesized_sample
      "Speaking",       // output_transport.first_audio
      "Completed",
    ]);
  });

  it("slow blocking tool: the tool holds the turn, then thinking resumes", () => {
    expect(progression("slow-blocking-tool")).toEqual([
      "Listening", "Endpointing", "Thinking", "Thinking",
      "Calling tool", "Calling tool", "Calling tool",   // requested, running, progress
      "Thinking",                                       // tool.completed
      "Speaking", "Speaking", "Speaking", "Completed",
    ]);
  });

  it("interrupted turn ends Interrupted after audio was accepted", () => {
    const states = progression("interrupted");
    expect(states.slice(-3)).toEqual(["Speaking", "Speaking", "Interrupted"]);
  });

  it("failed tool: two attempts, each closes, then the turn fails", () => {
    expect(progression("failed-tool")).toEqual([
      "Listening", "Endpointing", "Thinking",
      "Calling tool", "Calling tool", "Thinking",       // attempt 1: requested, running, error
      "Calling tool", "Calling tool", "Thinking",       // attempt 2
      "Failed",
    ]);
  });

  it("is Waiting with no turn, and stays Listening while STT runs during speech", () => {
    expect(deriveState(null)).toBe("Waiting");
    const events = fixtureEvents("normal-completed");
    const early = [events[0]!, { ...events[3]! }];   // user_speech.started, stt.started
    expect(deriveState(turnOf(early))).toBe("Listening");
  });

  it("reports the outcome only from a terminal event", () => {
    expect(outcomeOf(turnOf(fixtureEvents("normal-completed")))).toBe("completed");
    expect(outcomeOf(turnOf(fixtureEvents("interrupted")))).toBe("interrupted");
    expect(outcomeOf(turnOf(fixtureEvents("failed-tool")))).toBe("failed");
    expect(outcomeOf(turnOf(fixtureEvents("normal-completed").slice(0, 12)))).toBeNull();
    expect(outcomeOf(null)).toBeNull();
  });

  it("is deterministic", () => {
    const turn = turnOf(fixtureEvents("slow-blocking-tool"));
    expect(deriveState(turn)).toBe(deriveState(turn));
    expect(progression("slow-blocking-tool")).toEqual(progression("slow-blocking-tool"));
  });
});

describe("event log", () => {
  it("orders by time and offsets from the first event, without inventing zeros", () => {
    const log = eventLog(turnOf(fixtureEvents("normal-completed")));
    expect(log[0]).toMatchObject({ name: "user_speech.started", offsetMs: null });
    expect(log.find((r) => r.name === "output_transport.first_audio")?.offsetMs).toBe(1120);
    expect(log.map((r) => r.name)).toContain("turn.completed");
  });

  it("lists only events that happened: the failed turn has no audio events", () => {
    const names = eventLog(turnOf(fixtureEvents("failed-tool"))).map((r) => r.name);
    expect(names).not.toContain("output_transport.first_audio");
    expect(names).not.toContain("tts.started");
    expect(names).not.toContain("stt.started");
  });

  it("is empty without a turn", () => {
    expect(eventLog(null)).toEqual([]);
  });

  it("keeps arrival order for events recorded at the same instant", () => {
    const base = fixtureEvents("normal-completed");
    const same: TelemetryEvent[] = [base[1]!, base[2]!];   // both at 1.8 s
    expect(eventLog(turnOf(same)).map((r) => r.name)).toEqual(["user_speech.ended", "endpointing.started"]);
  });
});
