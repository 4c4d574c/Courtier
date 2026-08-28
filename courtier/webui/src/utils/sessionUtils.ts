import type { Thought, Turn, Step } from "../types/agent";

/**
 * Merge consecutive parent-agent tokens that belong to the same thinking turn.
 *
 * The runtime merges tokens into one thought block while `currentThoughtTurn`
 * stays the same (see sessionEventHandlers.ts).  The backend persists each
 * token as a separate ThoughtRecord, so history rendering must perform the
 * same merge to match the streaming layout.
 */
function mergeThoughtTokens(thoughts: Thought[]): Thought[] {
  const merged: Thought[] = [];
  for (const thought of thoughts) {
    const last = merged[merged.length - 1];
    // Only merge when the turn is positive.  Turn 0 is the legacy default for
    // sessions persisted before per-block turn tracking; collapsing all of
    // those thoughts into a single block would destroy their structure.
    if (last && last.turn === thought.turn && thought.turn > 0) {
      merged[merged.length - 1] = { ...last, text: last.text + thought.text };
    } else {
      merged.push(thought);
    }
  }
  return merged;
}

/**
 * Whether `t` belongs to `step` — same rule as the previous per-step scan.
 *
 * Newly streamed thoughts carry an explicit `stepIndex` matching the global
 * `Step.index`, which is more reliable than segment-index ranges.  Historical
 * sessions loaded from the backend may lack `stepIndex`, so we fall back to
 * the legacy segment-range matching in that case.
 */
function belongsToStep(t: Thought, step: Step): boolean {
  if (t.source) return false;
  // Normalize undefined turnIndex values to 0 for backward compatibility
  // with sessions persisted before turn tracking was added.
  const thoughtTurn = t.turnIndex ?? 0;
  const stepTurn = step.turnIndex ?? 0;
  if (thoughtTurn !== stepTurn) return false;
  if (t.stepIndex !== undefined) {
    return t.stepIndex === step.index;
  }
  // Fallback: segment-range matching for thoughts that haven't been
  // assigned a stepIndex yet (e.g. early streaming tokens in a new turn).
  const start = step.startSegmentIndex ?? 0;
  const end = step.endSegmentIndex ?? Infinity;
  const si = t.segmentIndex ?? 0;
  return si >= start && si < end;
}

/**
 * Return parent-agent thoughts for EVERY step of `turn` in one pass.
 *
 * Semantically identical to mapping `thoughtsForStep` over the turn's step
 * indices, but scans `allThoughts` once and carries the previous step's
 * merged texts through the loop instead of re-deriving them per step —
 * the old form was O(steps × thoughts) per rebuild and is the dominant
 * streaming cost under deep reactivity (see
 * docs/architecture/webui-streaming-perf-plan.md).
 *
 * A step's thoughts are deduped against the previous step's merged texts
 * (intermediate assistant text repeats there until the boundary moves).
 */
export function thoughtsForSteps(
  turn: Turn,
  allThoughts: Thought[],
): Thought[][] {
  const results: Thought[][] = [];
  let previousTexts = new Set<string>();

  turn.steps.forEach((step, stepIndexInTurn) => {
    const stepThoughts = allThoughts
      .filter((t) => belongsToStep(t, step))
      .sort((a, b) => (a.segmentIndex ?? 0) - (b.segmentIndex ?? 0));

    const mergedThoughts = mergeThoughtTokens(stepThoughts);

    // The first step never dedupes; later steps drop blocks whose whole
    // merged text matches a previous step's block.
    results.push(
      stepIndexInTurn === 0
        ? mergedThoughts
        : mergedThoughts.filter((t) => !previousTexts.has(t.text)),
    );
    // Texts of the un-deduped merge — matches what the old per-call path
    // re-derived from scratch for each previous step.
    previousTexts = new Set(mergedThoughts.map((t) => t.text));
  });

  return results;
}

/**
 * Return parent-agent thoughts that belong to a specific step.
 * Delegates to the one-pass `thoughtsForSteps` so both entry points share
 * the same matching/dedup semantics by construction.
 */
export function thoughtsForStep(
  allThoughts: Thought[],
  turn: Turn,
  stepIndexInTurn: number,
): Thought[] {
  if (!turn.steps[stepIndexInTurn]) return [];
  return thoughtsForSteps(turn, allThoughts)[stepIndexInTurn] ?? [];
}
