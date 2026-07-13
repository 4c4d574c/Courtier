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
  return thoughts.reduce<Thought[]>((merged, thought) => {
    const last = merged[merged.length - 1];
    // Only merge when the turn is positive.  Turn 0 is the legacy default for
    // sessions persisted before per-block turn tracking; collapsing all of
    // those thoughts into a single block would destroy their structure.
    if (last && last.turn === thought.turn && thought.turn > 0) {
      return [
        ...merged.slice(0, -1),
        { ...last, text: last.text + thought.text },
      ];
    }
    return [...merged, thought];
  }, []);
}

/**
 * Return parent-agent thoughts that belong to a specific step.
 *
 * Newly streamed thoughts carry an explicit `stepIndex` matching the global
 * `Step.index`, which is more reliable than segment-index ranges.  Historical
 * sessions loaded from the backend may lack `stepIndex`, so we fall back to
 * the legacy segment-range matching in that case.
 */
export function thoughtsForStep(
  allThoughts: Thought[],
  turn: Turn,
  stepIndexInTurn: number,
): Thought[] {
  const step = turn.steps[stepIndexInTurn];
  if (!step) return [];

  const belongsToStep = (t: Thought, targetStep: Step): boolean => {
    if (t.source) return false;
    // Normalize undefined turnIndex values to 0 for backward compatibility
    // with sessions persisted before turn tracking was added.
    const thoughtTurn = t.turnIndex ?? 0;
    const stepTurn = targetStep.turnIndex ?? 0;
    if (thoughtTurn !== stepTurn) return false;
    if (t.stepIndex !== undefined) {
      return t.stepIndex === targetStep.index;
    }
    // Fallback: segment-range matching for thoughts that haven't been
    // assigned a stepIndex yet (e.g. early streaming tokens in a new turn).
    const start = targetStep.startSegmentIndex ?? 0;
    const end = targetStep.endSegmentIndex ?? Infinity;
    const si = t.segmentIndex ?? 0;
    return si >= start && si < end;
  };

  const stepThoughts = allThoughts
    .filter((t) => belongsToStep(t, step))
    .sort((a, b) => (a.segmentIndex ?? 0) - (b.segmentIndex ?? 0));

  const mergedThoughts = mergeThoughtTokens(stepThoughts);

  if (stepIndexInTurn === 0) return mergedThoughts;

  const previousStep = turn.steps[stepIndexInTurn - 1];
  if (!previousStep) return mergedThoughts;

  const previousTexts = new Set(
    mergeThoughtTokens(
      allThoughts.filter((t) => belongsToStep(t, previousStep)),
    ).map((t) => t.text),
  );
  return mergedThoughts.filter((t) => !previousTexts.has(t.text));
}
