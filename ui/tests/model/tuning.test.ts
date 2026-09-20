import { describe, expect, it } from "vitest";
import type { TuningField } from "../../src/contract/events";
import { liveHello } from "../helpers/tuning";
import {
  atLaunch, changedCount, changedFromLaunch, draftFrom, formatValue, isDirty, launchDraft,
  saveErrorText, warningFor,
} from "../../src/model/tuning";

const fields = (values: Record<string, unknown> = {}): TuningField[] => liveHello({ values }).tuning!.fields;
const field = (key: string, values: Record<string, unknown> = {}) => fields(values).find((f) => f.key === key)!;

describe("draft", () => {
  it("starts from the saved values and knows the launch values", () => {
    const f = fields({ vad_stop_secs: 0.25 });
    expect(draftFrom(f)).toMatchObject({ vad_stop_secs: 0.25, user_speech_timeout: 0.2, use_smart_turn: false });
    expect(launchDraft(f)).toMatchObject({ vad_stop_secs: 0.4 });
  });

  it("sends only what differs from launch, and nothing when everything is at launch", () => {
    const f = fields();
    const draft = { ...draftFrom(f), vad_stop_secs: 0.25, use_smart_turn: true };
    expect(changedFromLaunch(f, draft)).toEqual({ vad_stop_secs: 0.25, use_smart_turn: true });
    expect(changedFromLaunch(f, launchDraft(f))).toEqual({});
    expect(atLaunch(f, launchDraft(f))).toBe(true);
    expect(changedCount(f, draft)).toBe(2);
  });

  it("is dirty only when it differs from what the server has saved, not from launch", () => {
    const saved = fields({ vad_stop_secs: 0.25 });
    expect(isDirty(saved, draftFrom(saved))).toBe(false);
    expect(isDirty(saved, { ...draftFrom(saved), vad_stop_secs: 0.3 })).toBe(true);
    expect(isDirty(saved, launchDraft(saved))).toBe(true);
  });
});

describe("warnings", () => {
  it("warn only past a threshold the server declared", () => {
    const vad = field("vad_stop_secs");
    expect(warningFor(vad, 0.2)).toContain("split one question");
    expect(warningFor(vad, 0.3)).toBeNull();
    expect(warningFor(vad, 0.4)).toBeNull();
    expect(warningFor(field("llm_max_tokens"), 15)).toContain("cut off");
    expect(warningFor(field("user_speech_timeout"), 0)).toBeNull();     // no warning declared
  });

  it("warn on a boolean only while it is on", () => {
    expect(warningFor(field("use_smart_turn"), true)).toContain("loads on the first turn");
    expect(warningFor(field("use_smart_turn"), false)).toBeNull();
  });
});

describe("formatting", () => {
  it("uses each setting's own precision and unit", () => {
    expect(formatValue(field("vad_stop_secs"), 0.4)).toBe("0.40 s");
    expect(formatValue(field("llm_max_tokens"), 60)).toBe("60 tokens");
    expect(formatValue(field("use_smart_turn"), true)).toBe("On");
    expect(formatValue(field("use_smart_turn"), false)).toBe("Off");
  });

  it("shows fixed sentences for the server's error codes, never its text", () => {
    expect(saveErrorText("stop_first")).toBe("Stop listening first, then save.");
    expect(saveErrorText("out_of_range")).toBe("The server did not accept one of those values.");
    expect(saveErrorText("<script>alert(1)</script>")).toBe("Could not save the settings.");
    expect(saveErrorText(null)).toBe("Could not save the settings.");
  });
});
