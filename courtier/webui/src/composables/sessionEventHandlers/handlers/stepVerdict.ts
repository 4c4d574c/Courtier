import type { AgentEvent } from "../../../types/agent";
import { replaceStepAt } from "../state";
import type { HandlerDeps } from "../types";

/**
 * step_verdict：后端在 observe 时把本 step 的中间结论文本刷进持久化并
 * 显式告知事件流（单一事实来源）。前端据此把对应 step 填上 verdict，
 * 并清空运行中缓冲——这些文本不再参与最终结论。
 */
export function handleStepVerdictEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { session } = deps();
  if (!event.text) return;
  let target = session.steps.findIndex((st) => st.index === event.stepIndex);
  if (target < 0) {
    // import.meta.env 由 Vite 注入；esbuild 打包的测试环境无此属性，需可选链。
    if (import.meta.env?.DEV) {
      console.warn(
        `step_verdict: step index ${event.stepIndex} 未找到，回退到最后一个 step`,
      );
    }
    target = session.steps.length - 1;
  }
  if (target < 0) return;
  const step = session.steps[target];
  replaceStepAt(deps, target, { ...step, verdict: event.text });
  session.pendingVerdict = "";
  session.pendingVerdictAfterStepIndex = 0;
}
