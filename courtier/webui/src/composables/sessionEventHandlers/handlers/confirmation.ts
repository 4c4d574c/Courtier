import type { DepsFn } from "../types";

/** confirmation_requested: 挂起一条工具确认（弹确认卡）。 */
export function handleConfirmationRequested(deps: DepsFn, event: { confirmationId?: string; toolName?: string; message?: string }): void {
  const { session } = deps();
  if (!event.confirmationId) return;
  session.pendingConfirmations = [
    ...(session.pendingConfirmations ?? []),
    {
      confirmationId: event.confirmationId,
      toolName: event.toolName || "",
      message: event.message || "",
    },
  ];
}

/** confirmation_resolved: 撤下已裁决的确认卡（幂等）。 */
export function handleConfirmationResolved(deps: DepsFn, event: { confirmationId?: string }): void {
  const { session } = deps();
  if (!event.confirmationId) return;
  session.pendingConfirmations = (session.pendingConfirmations ?? []).filter(
    (c) => c.confirmationId !== event.confirmationId,
  );
}
