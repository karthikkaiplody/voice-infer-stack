import { useState } from "react";
import type { Mode } from "../contract/events";
import { useStore } from "../state/store";
import { requestMode } from "../stream/api";
import styles from "./layout.module.css";

const OPTIONS: { mode: Mode; label: string }[] = [
  { mode: "fixture", label: "Fixture replay" },
  { mode: "live", label: "Local live" },
];

/**
 * Switch the page between recorded replays and the live agent. The server
 * answers by sending a fresh `hello`, and the page already resets on that, so
 * this holds no state of its own beyond "a switch is in flight".
 */
export function ModeSwitch() {
  const { state, dispatch } = useStore();
  const [pending, setPending] = useState<Mode | null>(null);
  const current = state.hello?.mode;
  if (current === undefined) return null;

  const running = state.runtime === "listening";
  const locked = state.connection !== "open" || running || pending !== null;

  async function choose(mode: Mode) {
    setPending(mode);
    const { ok, error } = await requestMode(mode);
    setPending(null);
    if (!ok) {
      dispatch({
        type: "notice",
        text: error === "stop_first"
          ? "Stop listening before switching."
          : "Could not switch. Check the terminal running the server.",
      });
    }
  }

  return (
    <div className={styles.switch} role="group" aria-label="Source">
      {OPTIONS.map(({ mode, label }) => (
        <button
          key={mode}
          type="button"
          className={styles.switchOption}
          aria-pressed={mode === current}
          disabled={mode !== current && locked}
          title={running && mode !== current ? "Stop listening first" : undefined}
          onClick={() => mode !== current && choose(mode)}
        >
          {pending === mode ? "Switching…" : label}
        </button>
      ))}
    </div>
  );
}
