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
};
