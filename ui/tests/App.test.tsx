import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { App } from "../src/App";
import { parseServerMessage } from "../src/contract/events";
import { epochFixtureEvents, fixtureEvents, fixtureHello, fold, rawFixtureEvents, replayState, telemetry } from "./helpers/fixtures";
import { initialState, reducer, type ViewState } from "../src/state/reducer";
import { StaticStore } from "../src/state/store";
import { autoplayScenario } from "../src/model/scenarios";
import { liveHello } from "./helpers/tuning";

const html = (state: ViewState) =>
  renderToString(<StaticStore state={state}><App /></StaticStore>);

const text = (markup: string) => markup.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

describe("the page", () => {
  it("before any turn: Waiting, no invented number, replay labelled accelerated", () => {
    const page = text(html(fold([fixtureHello()])));
    expect(page).toContain("Waiting");
    expect(page).toContain("Fixture replay");
    expect(page).toContain("Accelerated replay");
    expect(page).toContain("Metadata-only");
    expect(page).toContain("No events yet");
    expect(page).not.toMatch(/\b0 ms\b|NaN|undefined|null/);
  });

  it("shows the safe configuration and the fixture scenarios", () => {
    const markup = html(fold([fixtureHello()]));
    const page = text(markup);
    expect(page).toContain("config_fixture_v1");
    expect(page).toContain("stt: synthetic_stt");
    expect(page).toContain("vad_stop_secs: 0.4");
    for (const title of ["Normal turn", "Slow blocking tool", "Interrupted turn", "Failed tool", "Grounded answer"]) {
      expect(markup).toContain(title);
    }
  });

  it("a completed replay: state, the primary metric, and what it does not mean", () => {
    const page = text(html(replayState("normal-completed")));
    expect(page).toContain("Completed");
    expect(page).toContain("1,250 ms");
    expect(page).toContain("User speech ended → output transport accepted first audio");
    expect(page).toContain("not confirmed audible playback");
    expect(page).not.toMatch(/\baudible playback confirmed|heard\b/i);
  });

  it("the slow tool scenario reports its real, long first-audio time", () => {
    expect(text(html(replayState("slow-blocking-tool")))).toContain("3,270 ms");
  });

  it("an interrupted replay renders Interrupted", () => {
    const page = text(html(replayState("interrupted")));
    expect(page).toContain("Interrupted");
    expect(page).toContain("1,250 ms");
  });

  it("a failed replay is Unavailable with a reason, never 0 ms", () => {
    const page = text(html(replayState("failed-tool")));
    expect(page).toContain("Failed");
    expect(page).toContain("Unavailable");
    expect(page).toContain("accepted no audio");
    expect(page).toContain("tool_unavailable");
    expect(page).not.toMatch(/\b0 ms\b/);
  });

  it("mid-turn: shows the current state and waits for the metric", () => {
    const page = text(html(replayState("normal-completed", 4)));
    expect(page).toContain("Transcribing");
    expect(page).toContain("In progress");
    expect(page).toContain("Waiting");
  });

  it("says 'No outcome observed' for a turn that never ended, and for the one before", () => {
    const stopped = fold(
      [{ kind: "replay_finished", scenario: "normal-completed" }],
      replayState("normal-completed", 7),
    );
    expect(text(html(stopped))).toContain("No outcome observed");
    const next = fold([telemetry(fixtureEvents("interrupted")[0]!)], stopped);
    expect(text(html(next))).toContain("The previous turn: no outcome observed.");
  });

  it("lists the events received in time order", () => {
    const page = text(html(replayState("normal-completed")));
    expect(page.indexOf("user_speech.started")).toBeLessThan(page.indexOf("output_transport.first_audio"));
    expect(page).toContain("+2,050 ms");
  });

  it("shows a fixed notice, not server text, when the pipeline errors", () => {
    const page = text(html(fold([{ kind: "error", classification: "transport_error" }])));
    expect(page).toContain("The local pipeline reported an error (transport_error).");
  });
});

describe("privacy: hostile server data never reaches the page", () => {
  it("drops unknown fields and unsafe values from hello and events", () => {
    const hostile = parseServerMessage({
      kind: "hello",
      mode: "fixture",
      configuration: {
        snapshot_id: "config_ok",
        provider_ids: { stt: "CANARY transcript text" },
        model_ids: { llm: "/Users/canary/model.gguf" },
        audio_device: "CANARY MacBook Microphone",
        source_revision: "canary.internal.example.com",
      },
      capabilities: {},
      scenarios: ["normal-completed"],
      replay: { pacing: "accelerated", max_gap_ms: 350 },
    })!;
    const events = rawFixtureEvents("normal-completed").map((e: any) => ({
      ...e,
      attributes: { ...e.attributes, transcript: "CANARY spoken words", prompt: "CANARY prompt",
                    "gen_ai.request.model": "CANARY-model" },
      extra_field: "CANARY extra",
    }));
    let state = fold([hostile]);
    for (const event of events) {
      const message = parseServerMessage({ kind: "telemetry", event });
      if (message !== null) state = fold([message], state);
    }
    const page = html(state);
    expect(page).not.toMatch(/canary/i);
    expect(page).not.toContain("Users");
    expect(page).toContain("Not available");
  });

  it("escapes markup rather than interpreting it", () => {
    const page = html({ ...initialState, notice: "<img src=x onerror=alert(1)>" });
    expect(page).not.toContain("<img");
    expect(page).toContain("&lt;img");
  });
});

describe("live controls", () => {
  const liveHello = (running: boolean) => ({ ...fixtureHello(), mode: "live" as const, scenarios: [], replay: null, running });

  it("appear only in live mode, and the fixture picker does not", () => {
    const live = text(html(fold([liveHello(false)])));
    expect(live).toContain("Local live");
    expect(live).toContain("Start listening");
    expect(live).not.toContain("Accelerated replay");
    expect(text(html(fold([fixtureHello()])))).not.toContain("Start listening");
  });

  it("show the pipeline as listening once the server says it is running", () => {
    expect(text(html(fold([liveHello(true)])))).toContain("Listening on this machine");
  });
});

describe("when the server is gone", () => {
  it("disables the controls and says so instead of claiming to listen", () => {
    const liveRunning = fold([{ ...fixtureHello(), mode: "live" as const, scenarios: [], replay: null, running: true }]);
    const lost = reducer(liveRunning, { type: "connection", status: "reconnecting" });
    const markup = html(lost);
    expect(text(markup)).toContain("Lost the connection to the local server");
    expect(text(markup)).not.toContain("Listening on this machine");
    const controls = markup.match(/<section[^>]*aria-label="Live controls".*?<\/section>/s)![0];
    expect(controls.match(/<button[^>]*disabled/g)?.length).toBe(2);
    // the source switch cannot be used either, and says which option is current
    expect(markup).toMatch(/aria-pressed="true"[^>]*>Local live/);
    expect(markup).toMatch(/<button[^>]*disabled[^>]*>Fixture replay/);
  });
});

describe("the source switch", () => {
  const live = (running: boolean) => ({ ...fixtureHello(), mode: "live" as const, scenarios: [], replay: null, running });
  const button = (markup: string, label: string) => markup.match(new RegExp(`<button[^>]*>${label}</button>`))![0];

  it("offers both sources, with the current one pressed and the other usable", () => {
    const markup = html(fold([fixtureHello()]));
    expect(button(markup, "Fixture replay")).toContain('aria-pressed="true"');
    expect(button(markup, "Local live")).toContain('aria-pressed="false"');
    expect(button(markup, "Local live")).not.toContain("disabled");
  });

  it("from live it offers the fixtures, unless the agent is listening", () => {
    const idle = html(fold([live(false)]));
    expect(button(idle, "Local live")).toContain('aria-pressed="true"');
    expect(button(idle, "Fixture replay")).not.toContain("disabled");
    const listening = html(fold([live(true)]));
    expect(button(listening, "Fixture replay")).toContain("disabled");
    expect(button(listening, "Fixture replay")).toContain('title="Stop listening first"');
  });

  it("is absent until the server has said hello", () => {
    expect(text(html(initialState))).not.toContain("Fixture replay");
  });
});

describe("the waterfall on the page", () => {
  const liveHello = () => ({ ...fixtureHello(), mode: "live" as const, scenarios: [], replay: null, running: true,
    capabilities: { ...fixtureHello().capabilities, stages: { ...fixtureHello().capabilities.stages, tools: false },
                    outcomes: { completed: true, interrupted: false, failed: true } } });

  it("draws every stage of a completed replay, with VAD and endpointing explicit", () => {
    const page = text(html(replayState("normal-completed")));
    for (const label of ["Voice activity (VAD)", "Endpointing", "Speech to text", "Language model",
                         "Text to speech", "Output transport"]) {
      expect(page).toContain(label);
    }
    expect(page).toContain("speech lasted 800 ms");
    expect(page).toContain("policy: vad_timeout");
    expect(page).toContain("configured VAD silence: 400 ms");
    expect(page).toContain("Slowest measured stage: Endpointing · 600 ms");
    expect(page).toContain("ms from user speech ended");
  });

  it("a grounded answer shows the knowledge lookup and what it found", () => {
    const page = text(html(replayState("grounded-answer")));
    expect(page).toContain("Knowledge lookup");
    expect(page).toContain("2 notes matched");
    expect(page).toContain("best match: library.opening-hours");
  });

  it("names the live agent in the header, and shows no agent for a fixture", () => {
    const live = { ...liveHello(), agent: { id: "library", title: "Riverside Public Library information line" } };
    expect(text(html(fold([live])))).toContain("Agent: Riverside Public Library information line");
    expect(text(html(fold([fixtureHello()])))).not.toContain("Agent:");
  });

  it("says 'Not in this scenario' for stages a fixture leaves out", () => {
    const page = text(html(replayState("interrupted")));
    expect(page).toContain("Not in this scenario");
    expect(page).not.toContain("Not instrumented");
  });

  it("says 'Not instrumented' for tools when the live source cannot report them", () => {
    const state = fold([telemetry(fixtureEvents("normal-completed")[0]!)], fold([liveHello()]));
    const page = text(html(state));
    expect(page).toContain("Tool calls");
    expect(page).toContain("Not instrumented");
    expect(page).toContain("Waiting");
  });

  it("shows in-progress stages mid-turn and never a duration for them", () => {
    const page = text(html(replayState("normal-completed", 5)));
    expect(page).toContain("In progress");
    expect(page).not.toMatch(/\b0 ms\b|NaN|undefined/);
  });

  it("reports the failed turn's tool attempts and a model that never produced a token", () => {
    const page = text(html(replayState("failed-tool")));
    expect(page).toContain("Tool: synthetic_lookup (attempt 1)");
    expect(page).toContain("Tool: synthetic_lookup (attempt 2)");
    expect(page).toContain("Started · end not observed");
    expect(page).toContain("ended: error");
  });

  it("names the slow tool as the slowest stage", () => {
    expect(text(html(replayState("slow-blocking-tool"))))
      .toContain("Slowest measured stage: Tool: synthetic_lookup (attempt 1) · 3,010 ms");
  });

  it("never claims audible playback", () => {
    const page = text(html(replayState("normal-completed")));
    expect(page).toContain("not confirmed audible playback");
    expect(page).not.toMatch(/\bheard\b|was played/i);
  });
});

describe("live-scale timestamps (epoch nanoseconds)", () => {
  it.each([
    ["normal-completed", "1,250 ms", "Completed"],
    ["slow-blocking-tool", "3,270 ms", "Completed"],
    ["interrupted", "1,250 ms", "Interrupted"],
    ["false-endpoint", "1,050 ms", "Interrupted"],
  ] as const)("%s renders the same numbers as the small-timestamp fixture", (name, metric, outcome) => {
    const state = fold([fixtureHello(), { kind: "replay_reset", scenario: name },
                        ...epochFixtureEvents(name).map(telemetry)]);
    const page = text(html(state));
    expect(page).toContain(metric);
    expect(page).toContain(outcome);
    expect(page).toContain("Voice activity (VAD)");
    expect(page).not.toMatch(/Waiting|NaN|undefined/);
  });
});

describe("the tuning panel", () => {
  const live = (over: Parameters<typeof liveHello>[0] = {}) => fold([liveHello(over)]);

  it("appears in live mode with each setting, its launch value and what it does", () => {
    const page = text(html(live()));
    for (const label of ["VAD silence window", "Speech timeout", "Smart-turn model", "Reply length cap"]) {
      expect(page).toContain(label);
    }
    expect(page).toContain("launch 0.40 s");
    expect(page).toContain("launch 60 tokens");
    expect(page).toContain("Silence needed before the turn ends.");
    expect(page).toContain("Launch values");
    expect(page).toContain("Save for next start");
  });

  it("is absent in fixture mode", () => {
    expect(text(html(fold([fixtureHello()])))).not.toContain("Tuning");
  });

  it("shows changed settings and only the warnings they earn", () => {
    const page = text(html(live({ values: { vad_stop_secs: 0.2, use_smart_turn: true } })));
    expect(page).toContain("2 settings changed from launch");
    expect(page).toContain("Below about 0.3 s a pause can split one question into two turns.");
    expect(page).toContain("The model loads on the first turn.");
    expect(page).not.toContain("Replies may be cut off.");
    expect(text(html(live()))).not.toContain("Below about 0.3 s");
  });

  it("is locked while the pipeline is running, and says how to edit", () => {
    const markup = html(live({ running: true }));
    expect(text(markup)).toContain("Stop listening to edit.");
    const tuningHtml = markup.slice(markup.indexOf('aria-label="Tuning"'));
    expect(tuningHtml.match(/<input[^>]*disabled/g)?.length).toBe(4);
    expect(tuningHtml.match(/<button[^>]*disabled/g)?.length).toBe(2);
  });

  it("is locked when the connection is lost", () => {
    const markup = html(reducer(live(), { type: "connection", status: "reconnecting" }));
    expect(markup.slice(markup.indexOf('aria-label="Tuning"')).match(/<button[^>]*disabled/g)?.length).toBe(2);
  });

  it("the waterfall states the configured endpointing values it is running with", () => {
    const withTurn = fold(fixtureEvents("normal-completed").map(telemetry), live());
    const page = text(html(withTurn));
    expect(page).toContain("configured VAD silence: 400 ms");
    expect(page).toContain("configured speech timeout: 200 ms");
    expect(page).toContain("configured STT safety timer: 150 ms");
  });

  it("does not show timer settings as the cause when the smart-turn model decides", () => {
    const base = liveHello();
    const hello = { ...base, configuration: { ...base.configuration,
      endpointing_settings: { vad_stop_secs: 0.4, user_speech_timeout: 0.2, stt_ttfs_p99: 0.15, use_smart_turn: true } } };
    const page = text(html(fold(fixtureEvents("normal-completed").map(telemetry), fold([hello]))));
    expect(page).toContain("configured VAD silence: 400 ms");
    expect(page).not.toContain("configured speech timeout");
    expect(page).not.toContain("configured STT safety timer");
  });
});

describe("a silent microphone", () => {
  const listening = () => fold([{ ...fixtureHello(), mode: "live" as const, scenarios: [], replay: null, running: true }]);
  const warning = "No sound is reaching the agent";

  it("is explained, with the way out, while the agent is listening", () => {
    const page = text(html(fold([{ kind: "input_silent" }], listening())));
    expect(page).toContain(warning);
    expect(page).toContain("System Settings → Sound → Input");        // the first thing to try
    expect(page).toContain("Stop listening");
    expect(page).toContain("make devices");                            // the fallback
    expect(page).toContain("VOICE_AUDIO_DEVICE=&lt;number&gt; make live");   // escaped, as all text is
  });

  it("does not name the microphone: the page never learns its name", () => {
    const page = text(html(fold([{ kind: "input_silent" }], listening())));
    expect(page).not.toMatch(/MacBook|AirPods|USB|Built-in/i);
  });

  it("goes away when sound returns, and when listening stops", () => {
    const silent = fold([{ kind: "input_silent" }], listening());
    expect(text(html(fold([{ kind: "input_ok" }], silent)))).not.toContain(warning);
    expect(text(html(fold([{ kind: "stopped" }], silent)))).not.toContain(warning);
  });

  it("is not shown when the agent is not listening", () => {
    expect(text(html(fold([{ kind: "input_silent" }], fold([fixtureHello()]))))).not.toContain(warning);
  });
});

describe("the synthetic-data banner", () => {
  const banner = "Synthetic example data.";

  it("is at the top of the fixture replay, above the controls and the numbers", () => {
    const markup = html(replayState("normal-completed"));
    const page = text(markup);
    expect(page).toContain(banner);
    expect(page).toContain("not measurements from your machine");
    expect(markup.indexOf(banner)).toBeLessThan(markup.indexOf('aria-label="Fixture replay"'));
    expect(markup.indexOf(banner)).toBeLessThan(markup.indexOf("Current turn"));
  });

  it("is there before any scenario has been replayed", () => {
    expect(text(html(fold([fixtureHello()])))).toContain(banner);
  });

  it("is never shown for the live agent, whose numbers are measured", () => {
    const live = fold([{ ...fixtureHello(), mode: "live" as const, scenarios: [], replay: null, running: false }]);
    expect(text(html(live))).not.toContain("Synthetic example data");
  });

  it("is not shown before the server has said which source this is", () => {
    expect(text(html(initialState))).not.toContain("Synthetic example data");
  });
});

describe("opening the page on recorded turns", () => {
  it("starts the first scenario by itself, and only while nothing is on screen", () => {
    expect(autoplayScenario(fold([fixtureHello()]))).toBe("normal-completed");
    expect(autoplayScenario(replayState("normal-completed", 3))).toBeNull();
    expect(autoplayScenario(fold([fixtureHello(), { kind: "replay_reset", scenario: "normal-completed" }]))).toBeNull();
  });

  it("does nothing in live mode or before the server has said hello", () => {
    expect(autoplayScenario(initialState)).toBeNull();
    expect(autoplayScenario(fold([{ ...fixtureHello(), mode: "live" as const, scenarios: [], replay: null }]))).toBeNull();
  });
});
