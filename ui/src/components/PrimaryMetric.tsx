import { formatMs, primaryMetric } from "../model/turn";
import { useStore } from "../state/store";
import styles from "./layout.module.css";

export function PrimaryMetric() {
  const { state } = useStore();
  const metric = primaryMetric(state.turn);
  return (
    <div className={styles.card}>
      <p className={styles.label}>User speech ended → output transport accepted first audio</p>
      {metric.status === "measured" ? (
        <p className={styles.value}>{formatMs(metric.ms)}</p>
      ) : (
        <p className={`${styles.value} ${styles.valueMuted}`}>
          {metric.status === "waiting" ? "Waiting" : "Unavailable"}
        </p>
      )}
      <p className={styles.note}>
        {metric.status === "unavailable"
          ? metric.reason
          : "The output transport accepted an audio frame. That is not confirmed audible playback."}
      </p>
    </div>
  );
}
