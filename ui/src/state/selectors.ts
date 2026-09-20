import type { Outcome } from "../contract/events";
import { outcomeOf } from "../model/turn";
import type { ViewState } from "./reducer";

export type OutcomeView =
  | { kind: "none" }
  | { kind: "in_progress" }
  | { kind: "observed"; outcome: Outcome; classification: string | null }
  | { kind: "no_outcome_observed" };

/**
 * What to say about how the current turn ended.
 *
 * "No outcome observed" means the source stopped (or a replay finished)
 * without a completed / interrupted / failed event for a turn that had begun.
 * It is a UI-only state, not something the telemetry contract carries.
 */
export function outcomeView(state: ViewState): OutcomeView {
  const { turn } = state;
  if (turn === null) return { kind: "none" };
  const outcome = outcomeOf(turn);
  if (outcome !== null) {
    const terminal = turn.events.find((e) => e.event_name.startsWith("turn."));
    const code = terminal?.attributes["error.classification"];
    return { kind: "observed", outcome, classification: typeof code === "string" ? code : null };
  }
  const sourceEnded =
    state.runtime === "stopped" ||
    state.runtime === "error" ||
    (state.hello?.mode === "fixture" && state.playback === "finished");
  return sourceEnded ? { kind: "no_outcome_observed" } : { kind: "in_progress" };
}
