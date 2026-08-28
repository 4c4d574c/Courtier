/** User-facing messages — extracted for i18n readiness. */

export const MESSAGES = {
  /** SSE connection dropped after exceeding retry limit */
  CONNECTION_LOST: "连接中断，请重试",
  /** Re-attaching to a background run failed */
  ATTACH_FAILED: "接续后台任务失败，请重新打开会话",
  /** Background task is queued server-side (per-user concurrency limit) */
  QUEUED_HINT: "排队中，前面还有 {n} 个任务",
  /** Run died with a server restart */
  INTERRUPTED_HINT: "该任务因服务重启已中断，可重新发起",
  /** Delete session confirmation dialog */
  DELETE_CONFIRM: "确定要删除该会话吗？此操作不可撤销。",
  /** Session stopped by user */
  SESSION_STOPPED: "会话已中断",
  SESSION_STOPPED_DETAIL: "由用户手动停止",
  /** Session error card */
  SESSION_ERROR_TITLE: "会话异常终止",
  /** Loading state */
  LOADING: "加载中...",
  LOADING_CONNECTING: "正在连接审核引擎...",
  /** Empty state */
  NO_HISTORY: "暂无历史会话",
  /** File size exceeded */
  FILE_TOO_LARGE: (sizeMB: string) =>
    `文件过大（${sizeMB} MB），请上传小于 50 MB 的文件`,
  /** History */
  HISTORY_TITLE: "历史会话",
  /** File upload */
  UPLOAD_LABEL: "上传文档",
  /** Send button */
  SEND_LABEL: "开始审核",
  STOP_LABEL: "中断",
  UPLOADING_LABEL: "上传中...",
  /** Sidebar */
  SIDEBAR_TOGGLE: "切换思考面板",
  /** Status labels */
  STATUS_RUNNING: "运行中",
  STATUS_PAUSED: "已暂停",
  STATUS_COMPLETED: "已完成",
  STATUS_ERROR: "错误",
  /** Conclusion */
  CONCLUSION_LABEL: "审核结论",
  CHAT_CONCLUSION_SOURCES: "参考来源",
  CITATION_UNTITLED: "未命名文档",
  /** New session */
  NEW_SESSION: "新会话",
  EXPORT: "导出",
  /** Welcome / empty state */
  WELCOME_TITLE: "审衡智能体平台",
  WELCOME_DESC: "审以明辨，衡以持正",
  /** Chat UI */
  CHAT_NEW_SESSION: "新会话",
  CHAT_HISTORY_TITLE: "历史会话",
  CHAT_NO_HISTORY: "暂无历史会话",
  CHAT_SHOW_MORE: "显示更多",
  CHAT_SEND: "发送",
  CHAT_STOP: "停止",
  CHAT_UPLOADING: "上传中...",
  CHAT_ATTACH: "上传文档",
  CHAT_PLACEHOLDER: "输入审核任务描述，例如：审核这份通知的格式规范",
  CHAT_THINKING: "思考过程",
  CHAT_THINKING_ACTIVE: "分析中…",
  CHAT_MODEL: "模型",
  CHAT_THEME_TOGGLE: "切换主题",
  CHAT_TOGGLE_SIDEBAR: "展开/收起侧边栏",
  CHAT_SETTINGS: "个人设置",
  CHAT_LOGOUT: "退出登录",
  CHAT_USER_MANAGE: "用户管理",
  CHAT_APPROVALS: "注册审批",
  CHAT_PREVIEW_DOWNLOAD: "下载",
  CHAT_PREVIEW_CLOSE: "关闭预览",
  CHAT_PREVIEW_UNSUPPORTED: "当前文件格式不支持浏览器预览，请下载后查看。",
  CHAT_MENU_MORE: "更多操作",
  CHAT_MENU_RENAME: "编辑标题",
  CHAT_MENU_PIN: "置顶",
  CHAT_MENU_UNPIN: "取消置顶",
  CHAT_MENU_DELETE: "删除",
  CHAT_COMPACTING: "正在压缩上下文…",
  /** User bubble actions */
  CHAT_COPY: "复制",
  CHAT_COPIED: "已复制",
  CHAT_EDIT: "编辑",
  CHAT_EDIT_CONFIRM: "确认并发送",
  CHAT_EDIT_CANCEL: "取消",
  CHAT_EDIT_COMPACTED_HINT: "该会话历史已压缩，不支持编辑",
} as const;

/** Tool status to Chinese display label mapping. */
export const STATUS_LABELS: Record<string, string> = {
  pending: "等待中",
  running: "执行中",
  done: "已完成",
  error: "错误",
  warning: "警告",
  cancelled: "已中断",
};
