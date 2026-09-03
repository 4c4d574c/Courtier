// Mirror of /shared/file-upload-limits.json. Keep in sync with the backend.
export const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50 MB

export const ALLOWED_EXTENSIONS = [
  ".pdf",
  ".docx",
  ".bmp",
  ".jpg",
  ".jpeg",
  ".png",
  ".gif",
  ".tif",
  ".tiff",
  ".mp3",
  ".wav",
  ".m4a",
  ".flac",
  ".mp4",
  ".mov",
  ".webm",
  ".mkv",
  ".avi",
];

export const ALLOWED_EXTS = ALLOWED_EXTENSIONS.join(",");

export const MIME_TYPES: Record<string, string[]> = {
  ".pdf": ["application/pdf"],
  ".docx": [
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/wps-office.docx",
  ],
  ".bmp": ["image/bmp"],
  ".jpg": ["image/jpeg"],
  ".jpeg": ["image/jpeg"],
  ".png": ["image/png"],
  ".gif": ["image/gif"],
  ".tif": ["image/tiff"],
  ".tiff": ["image/tiff"],
  ".mp3": ["audio/mpeg"],
  ".wav": ["audio/wav", "audio/x-wav", "audio/vnd.wave", "audio/wave"],
  ".m4a": ["audio/mp4", "audio/x-m4a"],
  ".flac": ["audio/flac", "audio/x-flac"],
  ".mp4": ["video/mp4"],
  ".mov": ["video/quicktime"],
  ".webm": ["video/webm"],
  ".mkv": ["video/x-matroska"],
  ".avi": ["video/x-msvideo", "video/avi"],
};

// 附件类型（与后端 file_service.infer_kind 一致）。
export type FileKind = "document" | "image" | "audio" | "video";

const IMAGE_EXTS = new Set([".bmp", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff"]);
const AUDIO_EXTS = new Set([".mp3", ".wav", ".m4a", ".flac"]);
const VIDEO_EXTS = new Set([".mp4", ".mov", ".webm", ".mkv", ".avi"]);

export function kindForFile(name: string): FileKind {
  const ext = `.${name.split(".").pop()?.toLowerCase() ?? ""}`;
  if (IMAGE_EXTS.has(ext)) return "image";
  if (AUDIO_EXTS.has(ext)) return "audio";
  if (VIDEO_EXTS.has(ext)) return "video";
  return "document";
}

// 每类大小上限（字节）；document 沿用全局上限。
export const KIND_LIMITS: Record<FileKind, number> = {
  document: 50 * 1024 * 1024,
  image: 20 * 1024 * 1024,
  audio: 25 * 1024 * 1024,
  video: 50 * 1024 * 1024,
};

export function limitForFile(name: string): number {
  return KIND_LIMITS[kindForFile(name)] ?? MAX_FILE_SIZE;
}

// 每条消息各媒体类型数量上限（与后端 gate_media_attachments 一致）。
export const KIND_COUNT_LIMITS: Record<string, number> = {
  image: 4,
  audio: 1,
  video: 1,
};

// 附件类型 → 模型能力声明名（PoolModelConfig.modalities）。
export const KIND_TO_MODALITY: Record<string, string> = {
  image: "vision",
  audio: "audio",
  video: "video",
};
