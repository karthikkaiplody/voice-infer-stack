import type { ReactNode } from "react";
import { useStore } from "../state/store";
import styles from "./panel.module.css";

const NOT_AVAILABLE = <span className={styles.unavailable}>Not available</span>;

const joinPairs = (map: Record<string, string | number | boolean>): ReactNode => {
  const entries = Object.entries(map);
  return entries.length === 0 ? NOT_AVAILABLE : entries.map(([k, v]) => `${k}: ${String(v)}`).join(" · ");
};

/** The safe configuration snapshot, as identifiers only. */
export function ConfigSummary() {
  const { state } = useStore();
  const config = state.hello?.configuration;
  if (config === undefined) {
    return (
      <section className={styles.panel}>
        <div className={styles.head}><h2 className={styles.title}>Configuration</h2></div>
        <p className={styles.empty}>Waiting for the local server.</p>
      </section>
    );
  }
  const runtime = [config.execution_engine_id, config.hardware_class, config.cpu_architecture, config.os_version]
    .filter((part): part is string => part !== null)
    .join(" · ");
  const rows: [string, ReactNode][] = [
    ["Snapshot", config.snapshot_id ?? NOT_AVAILABLE],
    ["Source revision", config.source_revision ?? NOT_AVAILABLE],
    ["Providers", joinPairs(config.provider_ids)],
    ["Models and voice", joinPairs(config.model_ids)],
    ["Endpointing", joinPairs(config.endpointing_settings)],
    ["Runtime", runtime === "" ? NOT_AVAILABLE : runtime],
    ["Privacy mode", "Metadata-only"],
  ];
  return (
    <section className={styles.panel} aria-label="Configuration">
      <div className={styles.head}>
        <h2 className={styles.title}>Configuration</h2>
        <span className={styles.aside}>
          No audio, transcripts, prompts, replies, tool data or device names are captured or shown.
        </span>
      </div>
      <dl className={styles.list}>
        {rows.map(([name, value]) => (
          <div key={name} className={styles.row}>
            <dt>{name}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
