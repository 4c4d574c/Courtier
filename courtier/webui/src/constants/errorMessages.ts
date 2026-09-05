/**
 * 错误码 → 用户文案表。
 *
 * 后端 HTTP/SSE 错误返回结构化 {code, params}（见 courtier/agent/api/ui_errors.py），
 * 前端按 code 渲染；未知 code 回退到域级通用文案。新增后端 code 时在这里补一条。
 */

export interface UiMessage {
  /** 渲染模板：{param} 占位符由 params 填充。 */
  text: string;
}

const MESSAGES: Record<string, UiMessage> = {
  // auth
  "auth.invalid_credentials": { text: "用户名或密码错误" },
  "auth.account_locked": {
    text: "账号已被临时锁定，请在 {seconds} 秒后重试",
  },
  "auth.pending_approval": { text: "账号尚未通过审批，请等待管理员审核" },
  "auth.disabled": { text: "账号已被禁用" },
  "auth.username_taken": { text: "用户名已存在" },
  "auth.missing_token": { text: "请先登录" },
  "auth.token_expired": { text: "登录已过期，请重新登录" },
  "auth.token_invalid": { text: "登录状态无效，请重新登录" },
  "auth.token_missing_sub": { text: "登录状态无效，请重新登录" },
  "auth.unsupported_algorithm": { text: "服务器认证配置错误，请联系管理员" },
  "auth.secret_not_configured": { text: "服务器认证未配置，请联系管理员" },
  // profile
  "profile.current_password_required": { text: "修改密码时需要提供当前密码" },
  "profile.wrong_current_password": { text: "当前密码错误" },
  "profile.no_fields": { text: "没有提供需要更新的字段" },
  // session
  "session.task_required": { text: "任务内容不能为空" },
  "session.not_found": { text: "会话不存在" },
  "session.edit_turn_requires_session": { text: "编辑重发仅用于已有会话" },
  "session.running_conflict": { text: "会话正在运行中，请先停止" },
  // setup
  "setup.not_db_mode": { text: "当前部署无需初始化向导" },
  "setup.key_required": { text: "需要正确的初始化密钥（COURTIER_SETUP_KEY）" },
  "setup.invalid_username": { text: "用户名仅允许字母、数字与 _ . -（3-64 位）" },
  "setup.already_done": { text: "初始化已完成，向导已关闭" },
};

/** Domain fallbacks: unknown code in a known domain renders this. */
const DOMAIN_FALLBACKS: Record<string, string> = {
  auth: "认证失败，请重试",
  profile: "个人资料更新失败",
  session: "会话操作失败",
  setup: "初始化失败",
};

function fill(text: string, params: Record<string, unknown>): string {
  return text.replace(/\{(\w+)\}/g, (_, key: string) =>
    params[key] !== undefined ? String(params[key]) : `{${key}}`,
  );
}

/** Render a structured {code, params} error detail into user-facing text. */
export function renderUiError(code: string, params: Record<string, unknown> = {}): string {
  const msg = MESSAGES[code];
  if (msg) return fill(msg.text, params);
  const domain = code.split(".")[0];
  const fallback = DOMAIN_FALLBACKS[domain];
  if (fallback) return fill(fallback, params);
  return fill(code, params); // last resort: the code itself
}
