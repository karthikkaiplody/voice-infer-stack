import { useState } from "react";
import { scenarioBlurb, scenarioTitle } from "../model/scenarios";
import { useStore } from "../state/store";
import { requestReplay } from "../stream/api";
import styles from "./layout.module.css";

/** Fixture mode only: pick a synthetic scenario and replay it. */
export function ScenarioControls() {
  const { state, dispatch } = useStore();
  const hello = state.hello;
  const [selected, setSelected] = useState<string | null>(null);
  if (hello === null || hello.mode !== "fixture") return null;

  const scenario = selected ?? hello.scenarios[0] ?? null;
  const gap = hello.replay?.max_gap_ms;

  async function replay() {
    if (scenario === null) return;
    const ok = await requestReplay(scenario);
    if (!ok) dispatch({ type: "notice", text: "Could not start the replay. Is the local server still running?" });
  }

  return (
    <section className={styles.controls} aria-label="Fixture replay">
      <label>
        Scenario
        <select value={scenario ?? ""} onChange={(e) => setSelected(e.target.value)}>
          {hello.scenarios.map((id) => (
            <option key={id} value={id}>{scenarioTitle(id)}</option>
          ))}
        </select>
      </label>
      <button type="button" className={styles.primary} onClick={replay} disabled={scenario === null || state.connection !== "open"}>
        {state.playback === "playing" ? "Replay again" : "Replay"}
      </button>
      {hello.replay !== null && (
        <span
          className={styles.badge}
          title={`Waits between events are capped at ${gap} ms. The timings shown come from the recorded timestamps, not from how long the replay takes.`}
        >
          Accelerated replay
        </span>
      )}
      {scenario !== null && scenarioBlurb(scenario) !== null && (
        <span className={styles.hint}>{scenarioBlurb(scenario)}</span>
      )}
    </section>
  );
}
