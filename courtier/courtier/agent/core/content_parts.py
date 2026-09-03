"""Content-part contract for multimodal messages.

Internal logical form for message content that carries media attachments:
a user message may hold a list of ``TextPart``/``MediaPart`` instead of a
plain string. Media parts reference uploaded files by id — media bytes never
travel through agent state, compaction, persistence, or events;
``backends.openai_backend`` materializes them into provider content parts at
the wire boundary (base64 data URIs).

Assistant/tool/system messages remain plain strings — providers return text
only, so only user messages may carry part lists (enforced in the
``ChatMessage``/``Message`` constructors via :func:`ensure_parts_allowed`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

MediaKind = Literal["image", "audio", "video"]
MEDIA_KINDS: tuple[str, ...] = ("image", "audio", "video")

# 结构性标记沿用 core 既有惯例（「# 输入数据」同款），供模型阅读的占位说明。
_KIND_LABELS: dict[str, str] = {"image": "图片", "audio": "音频", "video": "视频"}


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class MediaPart:
    kind: MediaKind
    file_id: str
    name: str = ""


ContentPart = TextPart | MediaPart
MessageContent = str | list[ContentPart] | None


def content_to_plain_text(content: MessageContent) -> str:
    """Render content as plain text — media parts become readable markers.

    Single helper for every string view of message content: compaction
    summaries, plain-text echoes, placeholder rendering. ``str``/``None``
    pass through unchanged.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    chunks: list[str] = []
    for part in content:
        if isinstance(part, TextPart):
            chunks.append(part.text)
        else:
            label = _KIND_LABELS.get(part.kind, part.kind)
            name = part.name or part.file_id
            chunks.append(f"[附件: {name}（{label}）]")
    return "".join(chunks)


def iter_media_parts(content: MessageContent) -> list[MediaPart]:
    """Return the media parts of a message content (empty for plain strings)."""
    if not isinstance(content, list):
        return []
    return [part for part in content if isinstance(part, MediaPart)]


def parse_content(raw: Any) -> MessageContent:
    """Tolerantly parse persisted/wire content into the internal form.

    Strings and ``None`` pass through. A list is parsed item by item:
    ``{"type": "text", "text": ...}`` → ``TextPart``, ``{"type": "media",
    "kind": ..., "file_id": ..., "name": ...}`` → ``MediaPart``. Anything
    else is preserved losslessly as a JSON text part — parsing never raises
    (same philosophy as ``_parse_wire_arguments``).
    """
    if raw is None or isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return str(raw)
    parts: list[ContentPart] = []
    for item in raw:
        if isinstance(item, (TextPart, MediaPart)):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            parts.append(TextPart(text=str(item)))
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(TextPart(text=item["text"]))
        elif (
            item.get("type") == "media"
            and item.get("kind") in MEDIA_KINDS
            and isinstance(item.get("file_id"), str)
        ):
            parts.append(
                MediaPart(
                    kind=item["kind"],
                    file_id=item["file_id"],
                    name=str(item.get("name", "")),
                )
            )
        elif isinstance(item.get("text"), str):
            parts.append(TextPart(text=item["text"]))
        else:
            parts.append(TextPart(text=json.dumps(item, ensure_ascii=False)))
    return parts


def parts_to_internal(content: MessageContent) -> Any:
    """Serialize internal content to its JSON-safe logical form.

    ``str``/``None`` pass through; part lists become the internal dict form.
    This is NOT the provider wire format — media materialization happens in
    ``openai_backend``; this form is what persistence and logical wire
    conversion carry.
    """
    if content is None or isinstance(content, str):
        return content
    out: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            out.append({"type": "text", "text": part.text})
        else:
            out.append(
                {
                    "type": "media",
                    "kind": part.kind,
                    "file_id": part.file_id,
                    "name": part.name,
                }
            )
    return out


def ensure_parts_allowed(role: str, content: MessageContent) -> None:
    """Enforce "only user messages carry part lists" as a hard contract."""
    if isinstance(content, list) and role != "user":
        raise ValueError(
            f"only user messages may carry content parts (got role={role!r})"
        )
