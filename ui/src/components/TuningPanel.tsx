import { useState } from "react";
import type { TuningField } from "../contract/events";
import {
  changedCount, changedFromLaunch, draftFrom, formatValue, isDirty, launchDraft,
  saveErrorText, warningFor, type Draft,
} from "../model/tuning";
import { useStore } from "../state/store";
import { requestTuning } from "../stream/api";
import panel from "./panel.module.css";
import styles from "./tuning.module.css";

/** Live mode only: change latency thresholds, then start again to watch the waterfall move. */
export function TuningPanel() {
  const { state } = useStore();
  // Kept here, not in the editor: saving changes the snapshot, which remounts the
  // editor, and the confirmation has to survive that.
  const [status, setStatus] = useState<string | null>(null);
  const tuning = state.hello?.tuning ?? null;
  if (tuning === null) return null;
  const locked = state.runtime === "listening" || state.connection !== "open";
  // Remount when the saved values change, so the draft always starts from them.
  const saved = tuning.fields.map((f) => `${f.key}=${String(f.value)}`).join("|");
  return (
    <TuningEditor key={saved} fields={tuning.fields} locked={locked} running={state.runtime === "listening"}
                  status={status} setStatus={setStatus} />
  );
}

interface EditorProps {
  fields: TuningField[];
  locked: boolean;
  running: boolean;
  status: string | null;
  setStatus: (status: string | null) => void;
}

function TuningEditor({ fields, locked, running, status, setStatus }: EditorProps) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(fields));
  const dirty = isDirty(fields, draft);
  const changed = changedCount(fields, draft);

  async function save(next: Draft) {
    const result = await requestTuning(changedFromLaunch(fields, next));
    setStatus(result.ok
      ? "Saved. These settings apply the next time you click Start listening."
      : saveErrorText(result.error));
  }

  return (
    <section className={panel.panel} aria-label="Tuning">
      <div className={panel.head}>
        <h2 className={panel.title}>Tuning</h2>
        <span className={panel.aside}>
          {changed === 0 ? "Launch values" : `${changed} setting${changed === 1 ? "" : "s"} changed from launch`}
        </span>
      </div>
      <p className={styles.intro}>
        Change a threshold, save, then start listening and speak. The waterfall shows what moved.
        {running && " Stop listening to edit."}
      </p>
      <div className={styles.fields}>
        {fields.map((field) => (
          <Field
            key={field.key}
            field={field}
            value={draft[field.key]!}
            disabled={locked}
            onChange={(value) => { setStatus(null); setDraft((d) => ({ ...d, [field.key]: value })); }}
          />
        ))}
      </div>
      <div className={styles.actions}>
        <button type="button" className={styles.primary} disabled={locked || !dirty} onClick={() => save(draft)}>
          Save for next start
        </button>
        <button type="button" disabled={locked || changed === 0} onClick={() => { setStatus(null); setDraft(launchDraft(fields)); }}>
          Reset to launch values
        </button>
        {status !== null && <span className={styles.status} role="status">{status}</span>}
      </div>
    </section>
  );
}

function Field(props: { field: TuningField; value: number | boolean; disabled: boolean; onChange: (v: number | boolean) => void }) {
  const { field, value, disabled, onChange } = props;
  const id = `tune-${field.key}`;
  const warning = warningFor(field, value);
  const differs = value !== field.launch;
  return (
    <div className={`${styles.field} ${differs ? styles.differs : ""}`}>
      <div className={styles.line}>
        <label htmlFor={id} className={styles.name}>{field.label}</label>
        {field.kind === "boolean" ? (
          <input id={id} type="checkbox" checked={value === true} disabled={disabled}
                 onChange={(e) => onChange(e.target.checked)} />
        ) : (
          <input id={id} type="range" min={field.minimum} max={field.maximum} step={field.step}
                 value={typeof value === "number" ? value : field.launch as number} disabled={disabled}
                 onChange={(e) => onChange(Number(e.target.value))} />
        )}
        <output htmlFor={id} className={styles.value}>{formatValue(field, value)}</output>
        <span className={styles.launch}>launch {formatValue(field, field.launch)}</span>
      </div>
      <p className={styles.about}>{field.about}</p>
      {warning !== null && <p className={styles.warning} role="note">{warning}</p>}
    </div>
  );
}
