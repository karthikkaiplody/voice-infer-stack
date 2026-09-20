/** Same-origin calls to the local server. It only answers to 127.0.0.1. */
import type { Mode } from "../contract/events";

/** Ask the server to replay a synthetic fixture. Resolves `false` on any failure. */
export async function requestReplay(scenario: string): Promise<boolean> {
  try {
    const response = await fetch(`/replay/${encodeURIComponent(scenario)}`, { method: "POST" });
    return response.ok;
  } catch {
    return false;
  }
}

/** Ask the live server to start or stop the local pipeline. `false` on any failure. */
export async function requestControl(action: "start" | "stop"): Promise<boolean> {
  try {
    const response = await fetch(`/${action}`, { method: "POST" });
    return response.ok && (await response.json()).ok === true;
  } catch {
    return false;
  }
}

/** Save tuning changes (the settings that differ from launch). Applies at the next start. */
export async function requestTuning(values: Record<string, number | boolean>): Promise<{ ok: boolean; error: string | null }> {
  try {
    const response = await fetch("/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const body: unknown = await response.json();
    const ok = response.ok && typeof body === "object" && body !== null && (body as { ok?: unknown }).ok === true;
    const error = !ok && typeof body === "object" && body !== null && typeof (body as { error?: unknown }).error === "string"
      ? (body as { error: string }).error
      : null;
    return { ok, error };
  } catch {
    return { ok: false, error: null };
  }
}

/**
 * Switch the server between fixture replay and the live agent. On success the
 * server sends every page a fresh `hello`, so there is nothing to update here.
 */
export async function requestMode(mode: Mode): Promise<{ ok: boolean; error: string | null }> {
  try {
    const response = await fetch(`/mode/${mode}`, { method: "POST" });
    const body: unknown = await response.json();
    const record = typeof body === "object" && body !== null ? (body as { ok?: unknown; error?: unknown }) : {};
    const ok = response.ok && record.ok === true;
    return { ok, error: !ok && typeof record.error === "string" ? record.error : null };
  } catch {
    return { ok: false, error: null };
  }
}
