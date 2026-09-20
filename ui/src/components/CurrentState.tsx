import { deriveState, type TurnState } from "../model/turn";
import { useStore } from "../state/store";
import styles from "./layout.module.css";

const TONE: Partial<Record<TurnState, string>> = {
  Completed: styles.ok,
  Interrupted: styles.warn,
  Failed: styles.bad,
  Waiting: styles.valueMuted,
};

export function CurrentState() {
  const { state } = useStore();
  const current = deriveState(state.turn);
  return (
    <div className={styles.card}>
      <p className={styles.label}>Current turn</p>
      <p className={`${styles.value} ${TONE[current] ?? ""}`} role="status" aria-live="polite">
        {current}
      </p>
    </div>
  );
}
