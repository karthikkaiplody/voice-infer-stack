import { useStore } from "../state/store";
import styles from "./layout.module.css";

/**
 * Fixture mode only. The scenarios are pre-recorded examples, so every number on
 * the page is example data, and the page says so where it cannot be missed.
 */
export function SyntheticBanner() {
  const { state } = useStore();
  if (state.hello?.mode !== "fixture") return null;
  return (
    <p className={styles.synthetic} role="note">
      <strong>Synthetic example data.</strong> These are pre-recorded turns, not measurements from
      your machine. Replay waits are shortened, but every timing shown comes from the recorded
      timestamps. Switch to <em>Local live</em> to measure your own agent.
    </p>
  );
}
