/** Human names for the Phase 1 fixtures. Unknown scenarios fall back to their ID. */
const NAMES: Record<string, { title: string; blurb: string }> = {
  "normal-completed": {
    title: "Normal turn",
    blurb: "Every stage runs, and the reply is spoken to the end.",
  },
  "slow-blocking-tool": {
    title: "Slow blocking tool",
    blurb: "The model calls a tool that takes seconds, so the first audio waits for it.",
  },
  interrupted: {
    title: "Interrupted turn",
    blurb: "The user cuts in after the first audio, so the turn ends interrupted.",
  },
  "failed-tool": {
    title: "Failed tool",
    blurb: "A tool fails twice, no audio is produced, and the turn ends failed.",
  },
  "grounded-answer": {
    title: "Grounded answer",
    blurb: "The agent looks up its notes before the model answers, so a knowledge lookup runs between endpointing and the model.",
  },
};

export function scenarioTitle(id: string): string {
  return NAMES[id]?.title ?? id.replaceAll("-", " ");
}

export function scenarioBlurb(id: string): string | null {
  return NAMES[id]?.blurb ?? null;
}
