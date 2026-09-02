import type { AgentEvent } from "../../../types/agent";
import type { DepsFn } from "../types";

/**
 * think_retry（refusal 重试）：丢弃被拒尝试已流出的推理文本与结论缓冲，
 * 同一 step 内重新接收重试流的 token。丢弃条件=最后一条 thought 属于当前
 * think 轮（currentThoughtTurn），避免误删历史内容。
 */
export function handleThinkRetryEvent(deps: DepsFn, _event: AgentEvent): void {
  const { state: s, session } = deps();
  const last = session.thoughts[session.thoughts.length - 1];
  if (last && last.turn === s.currentThoughtTurn) {
    session.thoughts.pop();
  }
  session.pendingVerdict = "";
}

/** refusal_exhausted：重试耗尽仍拒绝——横幅提示当前模型可能不可用。 */
export function handleRefusalExhaustedEvent(deps: DepsFn, event: AgentEvent): void {
  const { session } = deps();
  session.refusalNotice = event.text || "模型多次拒绝执行该任务，当前模型可能不可用。";
}
