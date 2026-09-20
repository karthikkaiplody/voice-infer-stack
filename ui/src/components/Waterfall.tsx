import type { StageKey } from "../contract/events";
import { buildWaterfall, formatDuration, formatOffset, percent, type Axis, type Row, type RowStatus } from "../model/waterfall";
import { outcomeView } from "../state/selectors";
import { useStore } from "../state/store";
import panel from "./panel.module.css";
import styles from "./waterfall.module.css";

const STATUS_TEXT: Record<Exclude<RowStatus, "measured">, string> = {
  waiting: "Waiting",
  in_progress: "In progress",
  not_instrumented: "Not instrumented",
  not_in_scenario: "Not in this scenario",
  not_observed: "Not observed in this turn",
  started_no_end: "Started · end not observed",
};

// Every stage needs a colour: an unlisted one falls back to the same grey as voice activity.
const BAR_CLASS: Record<StageKey, string | undefined> = {
  user_speech: styles.vad,
  endpointing: styles.endpointing,
  stt: styles.stt,
  retrieval: styles.retrieval,
  llm: styles.llm,
  tools: styles.tools,
  tts: styles.tts,
  output_transport: styles.output,
};

function statusText(row: Row): string {
  if (row.status !== "measured") return STATUS_TEXT[row.status];
  if (row.stage === "user_speech") return `speech lasted ${formatDuration(row.durationMs ?? 0)}`;
  if (row.durationMs !== null && row.latencyMs !== null && row.latencyMs !== row.durationMs) {
    return `${formatDuration(row.latencyMs)} after speech ended`;
  }
  return formatDuration(row.durationMs ?? 0);
}

/** A stage's bar, from two observed timestamps. Never drawn for a stage that has none. */
function Bar({ row, axis }: { row: Row; axis: Axis }) {
  if (row.startMs === null) return null;
  const from = percent(axis, row.startMs);
  const to = row.endMs === null ? 100 : percent(axis, row.endMs);
  const open = row.endMs === null;
  return (
    <div
      className={`${styles.bar} ${BAR_CLASS[row.stage] ?? ""} ${open ? styles.open : ""}`}
      style={{ left: `${from}%`, width: open && row.status === "started_no_end" ? "6px" : `${Math.max(to - from, 0.4)}%` }}
      aria-hidden="true"
    />
  );
}

export function Waterfall() {
  const { state } = useStore();
  const hello = state.hello;
  if (hello === null) {
    return (
      <section className={panel.panel} aria-label="Turn waterfall">
        <div className={panel.head}><h2 className={panel.title}>Turn waterfall</h2></div>
        <p className={panel.empty}>Waiting for the local server.</p>
      </section>
    );
  }
  const settings = hello.configuration.endpointing_settings;
  const vad = settings.vad_stop_secs;
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  // With the smart-turn model deciding, the silence-timer settings are not what ended the turn.
  const timers = settings.use_smart_turn !== true;
  const view = outcomeView(state);
  const wf = buildWaterfall(state.turn, {
    mode: hello.mode,
    stages: hello.capabilities.stages,
    sourceEnded: view.kind === "no_outcome_observed",
    vadStopSecs: num(vad),
    speechTimeoutSecs: timers ? num(settings.user_speech_timeout) : null,
    sttTimerSecs: timers ? num(settings.stt_ttfs_p99) : null,
  });
  const { axis } = wf;

  return (
    <section className={panel.panel} aria-label="Turn waterfall">
      <div className={panel.head}>
        <h2 className={panel.title}>Turn waterfall</h2>
        <span className={panel.aside}>
          {wf.slowest === null
            ? "Slowest measured stage: none yet"
            : `Slowest measured stage: ${wf.slowest.label} · ${formatDuration(wf.slowest.latencyMs)}`}
        </span>
      </div>

      <div className={styles.axisRow}>
        <div className={styles.labelCol}>ms from {axis.originLabel}</div>
        <div className={styles.lane}>
          {axis.ticks.map((tick) => (
            <span key={tick} className={styles.tickLabel} style={{ left: `${percent(axis, tick)}%` }}>
              {tick === 0 ? "0" : tick}
            </span>
          ))}
        </div>
      </div>

      <ol className={styles.rows}>
        {wf.rows.map((row) => (
          <li key={row.key} className={`${styles.row} ${styles[`s_${row.status}`] ?? ""}`}>
            <div className={styles.labelCol} title={row.about}>
              <strong>{row.label}</strong>
              <span className={styles.status}>{statusText(row)}</span>
              {row.notes.map((note) => (
                <span key={note} className={styles.note}>{note}</span>
              ))}
            </div>
            <div className={styles.lane}>
              {axis.ticks.map((tick) => (
                <i key={tick} className={`${styles.grid} ${tick === 0 ? styles.zero : ""}`} style={{ left: `${percent(axis, tick)}%` }} />
              ))}
              <Bar row={row} axis={axis} />
              {row.markerMs !== null && (
                <span
                  className={styles.marker}
                  style={{ left: `${percent(axis, row.markerMs)}%` }}
                  title={`${row.markerLabel} at ${formatOffset(row.markerMs)}`}
                />
              )}
            </div>
          </li>
        ))}
      </ol>
      <p className={styles.legend}>
        Bars are drawn from recorded timestamps only. ◆ marks an observed instant. Stages overlap, so their
        durations do not add up to the total. A stage this source cannot report has no bar and no number.
      </p>
    </section>
  );
}
