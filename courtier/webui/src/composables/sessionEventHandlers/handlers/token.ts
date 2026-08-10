import type { AgentEvent } from "../../../types/agent";
import { appendThought } from "../state";
import type { HandlerDeps } from "../types";

export function handleTokenEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  if (event.text) appendThought(deps, event.text);
}

export function handleConclusionTokenEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { state: s, session, turnVersion } = deps();
  if (!event.text) return;
  // 先进运行中缓冲：这段文本可能是某 step 的中间结论（observe 时后端发
  // step_verdict 转正），也可能是最终结论（complete 时落定）。直接进
  // conclusion 会让中间过程文本全部堆积到底部结论区。
  if (!session.pendingVerdict) {
    // 新一段文本开始：记下归属边界。文本由当前 think 产生，将转正为当前
    // think 对应 step 的 verdict，渲染在该 step 工具框之前。流式路径下
    // 后端在生成前先发 text_response，前端据此创建占位 step 并把
    // state.currentStepIndex 指向它（随后的 tool_calls 也补丁到它）——
    // 所以边界取"当前 step 的前一个"，而不是"已有 step 总数"（占位
    // step 本身不算在边界内）。无当前 step 时退化为已有 step 总数。
    session.pendingVerdictAfterStepIndex =
      s.currentStepIndex > 0 ? s.currentStepIndex - 1 : session.steps.length;
  }
  session.pendingVerdict = (session.pendingVerdict ?? "") + event.text;
  turnVersion.value++;
}
