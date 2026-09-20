/**
 * The tuning panel's logic, apart from the React that draws it.
 *
 * The server owns which settings exist and their limits. This only tracks a
 * draft of the values being edited, and works out what to send and what to warn
 * about. Nothing here has a default of its own.
 */

import type { TuningField } from "../contract/events";

export type Draft = Record<string, number | boolean>;

export const draftFrom = (fields: readonly TuningField[]): Draft =>
  Object.fromEntries(fields.map((f) => [f.key, f.value]));

export const launchDraft = (fields: readonly TuningField[]): Draft =>
  Object.fromEntries(fields.map((f) => [f.key, f.launch]));

/** Only the settings that differ from the launch values: what the server needs. */
export function changedFromLaunch(fields: readonly TuningField[], draft: Draft): Draft {
  return Object.fromEntries(fields.filter((f) => draft[f.key] !== f.launch).map((f) => [f.key, draft[f.key]!]));
}

/** The draft differs from what the server has saved. */
export const isDirty = (fields: readonly TuningField[], draft: Draft): boolean =>
  fields.some((f) => draft[f.key] !== f.value);

/** Every setting in the draft equals its launch value. */
export const atLaunch = (fields: readonly TuningField[], draft: Draft): boolean =>
  fields.every((f) => draft[f.key] === f.launch);

/** Number of settings that currently differ from launch. */
export const changedCount = (fields: readonly TuningField[], draft: Draft): number =>
  fields.filter((f) => draft[f.key] !== f.launch).length;

/** The field's warning, if the drafted value crosses one of its thresholds. */
export function warningFor(field: TuningField, value: number | boolean): string | null {
  if (field.warning === undefined) return null;
  if (typeof value === "boolean") return value ? field.warning : null;
  const below = field.warnBelow !== undefined && value < field.warnBelow;
  const above = field.warnAbove !== undefined && value > field.warnAbove;
  return below || above ? field.warning : null;
}

const decimals = (step: number | undefined) => {
  if (step === undefined || Number.isInteger(step)) return 0;
  const text = String(step);
  return text.includes(".") ? text.split(".")[1]!.length : 0;
};

export function formatValue(field: TuningField, value: number | boolean): string {
  if (typeof value === "boolean") return value ? "On" : "Off";
  const shown = value.toFixed(decimals(field.step));
  return field.unit === undefined ? shown : `${shown} ${field.unit}`;
}

/** Sentences for the server's fixed error codes. Server text is never shown. */
export function saveErrorText(code: string | null): string {
  switch (code) {
    case "stop_first":
      return "Stop listening first, then save.";
    case "out_of_range":
    case "invalid_value":
    case "unknown_setting":
      return "The server did not accept one of those values.";
    default:
      return "Could not save the settings.";
  }
}
