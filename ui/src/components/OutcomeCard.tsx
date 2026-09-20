import { outcomeView } from "../state/selectors";
import { useStore } from "../state/store";
import styles from "./layout.module.css";

const OUTCOME_TEXT = { completed: "Completed", interrupted: "Interrupted", failed: "Failed" } as const;
const OUTCOME_TONE = { completed: styles.ok, interrupted: styles.warn, failed: styles.bad } as const;

export function OutcomeCard() {
  const { state } = useStore();
  const view = outcomeView(state);
  let text = "—";
  let tone = styles.valueMuted;
  if (view.kind === "observed") {
    text = OUTCOME_TEXT[view.outcome];
    tone = OUTCOME_TONE[view.outcome] ?? "";
  } else if (view.kind === "in_progress") {
    text = "In progress";
  } else if (view.kind === "no_outcome_observed") {
    text = "No outcome observed";
    tone = styles.warn;
  }
  return (
    <div className={styles.card}>
      <p className={styles.label}>Outcome</p>
      <p className={`${styles.value} ${tone}`}>{text}</p>
      {view.kind === "observed" && view.classification !== null && (
        <p className={styles.note}>Reason code: <code>{view.classification}</code></p>
      )}
      {view.kind === "no_outcome_observed" && (
        <p className={styles.note}>The turn never reached completed, interrupted or failed.</p>
      )}
      {state.previousWithoutOutcome && (
        <p className={styles.note}>The previous turn: no outcome observed.</p>
      )}
    </div>
  );
}
