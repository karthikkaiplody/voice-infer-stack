import { useState } from "react";
import { useStore } from "../state/store";
import { requestControl } from "../stream/api";
import styles from "./layout.module.css";

/** Live mode only: start and stop the local microphone pipeline. */
export function LiveControls() {
  const { state, dispatch } = useStore();
  const [starting, setStarting] = useState(false);
  if (state.hello?.mode !== "live") return null;

  const connected = state.connection === "open";
  const running = connected && state.runtime === "listening";
  const busy = connected && starting && !running;

  async function control(action: "start" | "stop") {
    if (action === "start") setStarting(true);
    const ok = await requestControl(action);
    if (!ok) {
      setStarting(false);
      dispatch({ type: "notice", text: `Could not ${action} the local pipeline.` });
    } else if (action === "stop") {
      setStarting(false);
    }
  }

  return (
    <section className={styles.controls} aria-label="Live controls">
      <button type="button" className={styles.primary} onClick={() => control("start")} disabled={!connected || running || busy}>
        {busy ? "Starting…" : "Start listening"}
      </button>
      <button type="button" onClick={() => control("stop")} disabled={!connected || (!running && !busy)}>
        Stop
      </button>
      <span className={styles.hint}>
        {!connected
          ? "Lost the connection to the local server. Check the terminal running the server (make live or make demo); restart it and reload this page."
          : running
          ? "Listening on this machine's microphone. Use headphones, or the agent hears itself."
          : busy
            ? "Loading models. The first start can take a while."
            : "Uses this machine's microphone and speakers, not the browser's."}
      </span>
    </section>
  );
}
