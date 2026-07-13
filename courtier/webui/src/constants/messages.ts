/** User-facing messages — extracted for i18n readiness. */

export const MESSAGES = {
  /** SSE connection dropped after exceeding retry limit */
  CONNECTION_LOST: "连接中断，请重试",
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
  /** New session */
  NEW_SESSION: "新会话",
  EXPORT: "导出",
  /** Welcome / empty state */
  WELCOME_TITLE: "SDTAgent 文档审计",
  WELCOME_DESC: "输入审核任务开始审计公文文档",
  QUICK_FORMAT: "格式审核",
  QUICK_CONTENT: "内容审核",
  QUICK_FULL: "全面审核",
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
