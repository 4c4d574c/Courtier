import type { Ref } from "vue";
import type { Session, Turn } from "../../types/agent";
import type { PendingSubagent } from "../subagentTree";

export interface MutableState {
  currentTurn: Turn | null;
  currentTurnIndex: number;
  currentStepIndex: number;
  segmentIndex: number;
  currentSegmentType: "observe" | "tool_result";
  currentThoughtTurn: number;
  observedSinceLastStep: boolean;
  thoughtIdCounter: number;
  toolIdCounter: number;
  /** Sub-agent thought id counters keyed by handle id. */
  subagentThoughtCounters: Record<string, number>;
  /** Sub-agent start events that arrived before the current step existed. */
  pendingSubagents: PendingSubagent[];
}

export interface HandlerDeps {
  state: MutableState;
  session: Session;
  turnVersion: Ref<number>;
}

export type DepsFn = () => HandlerDeps;
