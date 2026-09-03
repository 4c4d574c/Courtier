"""T4: media materialization at the openai_backend wire boundary."""

import base64
import io

import pytest

from courtier.agent.core.backends.openai_backend import (
    OpenAIModelBackend,
    _normalize_image_bytes,
)
from courtier.agent.core.content_parts import MediaPart, TextPart
from courtier.agent.core.protocol import ChatMessage, ChatRequest
from courtier.agent.core.state import AgentState

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def _png_bytes(size: tuple[int, int] = (4, 4), color=(200, 30, 30)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size: tuple[int, int] = (4, 4)) -> bytes:
    img = Image.new("RGB", size, (10, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _resolver(payload: bytes, mime: str):
    async def resolve(file_id: str, kind: str) -> tuple[bytes, str]:
        return payload, mime

    return resolve


def _backend(resolver=None, modalities: tuple = ("vision", "audio", "video")):
    return OpenAIModelBackend(
        base_url="http://x/v1",
        api_key="k",
        media_resolver=resolver,
        declared_modalities=modalities,
        image_max_edge=64,
        image_jpeg_quality=80,
    )


def _media_request(*content) -> ChatRequest:
    return ChatRequest(
        model="m",
        messages=(ChatMessage(role="user", content=list(content)),),
    )


def _b64_data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


class TestMaterializeImage:
    @pytest.mark.skipif(not HAS_PIL, reason="Pillow missing")
    async def test_small_png_stays_png(self):
        png = _png_bytes()
        backend = _backend(_resolver(png, "image/png"))
        params = backend._build_params(
            _media_request(TextPart("看"), MediaPart("image", "f1", "a.png")),
            await backend._materialize_messages(
                (ChatMessage(role="user", content=[TextPart("看"), MediaPart("image", "f1", "a.png")]),)
            ),
        )
        parts = params["messages"][0]["content"]
        assert parts[0] == {"type": "text", "text": "看"}
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"] == _b64_data_url(png, "image/png")

    @pytest.mark.skipif(not HAS_PIL, reason="Pillow missing")
    async def test_oversized_image_resized_to_jpeg(self):
        big = _png_bytes((300, 150))
        backend = _backend(_resolver(big, "image/png"))
        wire = await backend._materialize_messages(
            (ChatMessage(role="user", content=[MediaPart("image", "f1", "big.png")]),)
        )
        part = wire[0]["content"][0]
        assert part["type"] == "image_url"
        url = part["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        raw = base64.b64decode(url.split(",", 1)[1])
        img = Image.open(io.BytesIO(raw))
        assert max(img.size) <= 64

    async def test_jpeg_passthrough(self):
        jpg = _jpeg_bytes()
        backend = _backend(_resolver(jpg, "image/jpeg"))
        wire = await backend._materialize_messages(
            (ChatMessage(role="user", content=[MediaPart("image", "f1", "a.jpg")]),)
        )
        assert wire[0]["content"][0]["image_url"]["url"] == _b64_data_url(jpg, "image/jpeg")


class TestMaterializeAudioVideo:
    async def test_audio_input_audio_part(self):
        wav = b"RIFFxxxxWAVEfmt " + b"\x00" * 8
        backend = _backend(_resolver(wav, "audio/wav"))
        wire = await backend._materialize_messages(
            (ChatMessage(role="user", content=[MediaPart("audio", "f2", "n.wav")]),)
        )
        part = wire[0]["content"][0]
        assert part == {
            "type": "input_audio",
            "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"},
        }

    async def test_video_data_uri(self):
        mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
        backend = _backend(_resolver(mp4, "video/mp4"))
        wire = await backend._materialize_messages(
            (ChatMessage(role="user", content=[MediaPart("video", "f3", "c.mp4")]),)
        )
        part = wire[0]["content"][0]
        assert part["type"] == "video_url"
        assert part["video_url"]["url"] == _b64_data_url(mp4, "video/mp4")


class TestDegradation:
    async def test_missing_modality_degrades_to_placeholder(self):
        backend = _backend(_resolver(b"x", "image/png"), modalities=())
        wire = await backend._materialize_messages(
            (ChatMessage(role="user", content=[MediaPart("image", "f1", "old.png")]),)
        )
        assert wire[0]["content"] == [{"type": "text", "text": "[附件: old.png（图片，file_id=f1）已省略]"}]

    async def test_no_resolver_degrades(self):
        backend = _backend(None)
        wire = await backend._materialize_messages(
            (ChatMessage(role="user", content=[MediaPart("image", "f1", "a.png")]),)
        )
        assert wire[0]["content"][0]["type"] == "text"

    async def test_resolver_failure_degrades(self):
        async def boom(file_id: str, kind: str):
            raise FileNotFoundError(file_id)

        backend = _backend(boom)
        wire = await backend._materialize_messages(
            (ChatMessage(role="user", content=[MediaPart("audio", "f2", "gone.wav")]),)
        )
        assert wire[0]["content"] == [{"type": "text", "text": "[附件: gone.wav（音频，file_id=f2）已省略]"}]

    async def test_plain_messages_untouched(self):
        backend = _backend(None)
        wire = await backend._materialize_messages(
            (ChatMessage(role="user", content="hello"), ChatMessage(role="assistant", content="hi"))
        )
        assert wire[0]["content"] == "hello"
        assert wire[1]["content"] == "hi"


class TestNormalizeImage:
    @pytest.mark.skipif(not HAS_PIL, reason="Pillow missing")
    def test_within_cap_png_untouched(self):
        png = _png_bytes((32, 16))
        out, mime = _normalize_image_bytes(png, "image/png", 64, 80)
        assert (out, mime) == (png, "image/png")

    @pytest.mark.skipif(not HAS_PIL, reason="Pillow missing")
    def test_no_cap_returns_original(self):
        png = _png_bytes((300, 300))
        out, mime = _normalize_image_bytes(png, "image/png", None, 80)
        assert (out, mime) == (png, "image/png")

    def test_garbage_bytes_pass_through(self):
        out, mime = _normalize_image_bytes(b"not-an-image", "image/png", 64, 80)
        assert (out, mime) == (b"not-an-image", "image/png")


class TestMessageConstruction:
    def test_initial_with_media_parts(self):
        state = AgentState.initial(
            task="分析",
            system_prompt="sys",
            media_parts=(MediaPart("image", "f1", "a.png"),),
        )
        user = state.messages[-1]
        assert user.role == "user"
        assert isinstance(user.content, list)
        text = user.content[0].text
        assert "分析" in text and "[附件: a.png（图片，file_id=f1）]" in text
        assert user.content[1] == MediaPart("image", "f1", "a.png")

    def test_initial_without_media_is_plain_str(self):
        state = AgentState.initial(task="任务", system_prompt="s")
        assert state.messages[-1].content == "任务"
