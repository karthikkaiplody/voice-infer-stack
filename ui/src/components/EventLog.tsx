import { eventLog, formatMs } from "../model/turn";
import { useStore } from "../state/store";
import styles from "./panel.module.css";

/**
 * The events received for this turn, in time order. Offsets are measured from
 * the turn's own first recorded event. This is the raw material the waterfall
 * will be drawn from.
 */
export function EventLog() {
  const { state } = useStore();
  const rows = eventLog(state.turn);
  return (
    <section className={styles.panel} aria-label="Events received">
      <div className={styles.head}>
        <h2 className={styles.title}>Events received</h2>
        <span className={styles.aside}>Recorded timestamps, offset from the first event</span>
      </div>
      {rows.length === 0 ? (
        <p className={styles.empty}>No events yet.</p>
      ) : (
        <ol className={styles.log}>
          {rows.map((row) => (
            <li key={row.id}>
              <span>{row.name}</span>
              <span className={styles.offset}>{row.offsetMs === null ? "start" : `+${formatMs(row.offsetMs)}`}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
