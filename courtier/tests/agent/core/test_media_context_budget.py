"""T5: media token accounting and stale-media stripping in compaction."""

from courtier.agent.core.content_parts import MediaPart, TextPart
from courtier.agent.core.context_manager import (
    ContextManager,
    _build_summary,
    _strip_stale_media,
)
from courtier.agent.core.state import Message


def _mgr(**kwargs) -> ContextManager:
    defaults = dict(
        media_image_tokens=1000,
        media_audio_tokens_per_second=50.0,
        media_video_tokens_per_second=100.0,
    )
    defaults.update(kwargs)
    return ContextManager(cache_dir=None or ".agent_cache_test_media", **defaults)


class TestMediaTokenEstimation:
    def test_text_only_unchanged(self):
        mgr = _mgr()
        msg = Message(role="user", content="hello")
        assert mgr.estimate_tokens((msg,)) == mgr._compute_message_tokens(msg)
        # 5 ASCII chars × 0.25
        assert mgr._compute_message_tokens(msg) == 1

    def test_image_part_uses_constant(self):
        mgr = _mgr()
        msg = Message(role="user", content=[TextPart("分析"), MediaPart("image", "f1", "a.png")])
        # 2 CJK chars × 0.65 = 1.3 → 1 + image 1000
        assert mgr._compute_message_tokens(msg) == 1001

    def test_audio_video_uses_duration(self):
        mgr = _mgr()
        audio = Message(
            role="user", content=[MediaPart("audio", "f2", "a.wav", duration_seconds=10.0)]
        )
        video = Message(
            role="user", content=[MediaPart("video", "f3", "b.mp4", duration_seconds=30.0)]
        )
        assert mgr._compute_message_tokens(audio) == 500
        assert mgr._compute_message_tokens(video) == 3000

    def test_media_without_duration_counts_one_second(self):
        mgr = _mgr()
        msg = Message(role="user", content=[MediaPart("video", "f4", "c.mp4")])
        assert mgr._compute_message_tokens(msg) == 100

    def test_estimate_cache_does_not_break_media_messages(self):
        mgr = _mgr()
        msg = Message(role="user", content=[TextPart("x"), MediaPart("image", "f1", "a.png")])
        assert mgr.estimate_tokens((msg,)) == mgr.estimate_tokens((msg,))


class TestStripStaleMedia:
    def test_newest_media_message_kept(self):
        old = Message(role="user", content=[TextPart("旧的"), MediaPart("image", "f1", "old.png")])
        new = Message(role="user", content=[TextPart("新的"), MediaPart("image", "f2", "new.png")])
        out = _strip_stale_media((old, new))
        assert out[1].content == [TextPart("新的"), MediaPart("image", "f2", "new.png")]
        assert isinstance(out[0].content, str)
        assert "旧的" in out[0].content
        assert "[附件: old.png（图片，file_id=f1）已省略]" in out[0].content

    def test_no_media_is_noop(self):
        msgs = (Message(role="user", content="a"), Message(role="assistant", content="b"))
        assert _strip_stale_media(msgs) == msgs

    def test_single_media_message_untouched(self):
        msg = Message(role="user", content=[TextPart("任务"), MediaPart("audio", "f9", "x.wav")])
        out = _strip_stale_media((msg,))
        assert out[0] == msg

    def test_stripped_text_is_plain_string(self):
        old = Message(
            role="user", content=[MediaPart("video", "f1", "v.mp4", duration_seconds=5.0)]
        )
        newest = Message(role="user", content=[MediaPart("video", "f2")])
        stripped, kept = _strip_stale_media((old, newest))
        assert isinstance(stripped.content, str)
        assert "已省略" in stripped.content
        assert kept == newest


class TestSummaryTolerance:
    def test_build_summary_handles_part_list(self):
        msgs = (
            Message(role="user", content=[TextPart("看图"), MediaPart("image", "f1", "a.png")]),
            Message(role="assistant", content="好的"),
        )
        text = _build_summary(msgs)
        assert "[USER] 看图[附件: a.png（图片，file_id=f1）]" in text
        assert "[ASSISTANT] 好的" in text


class TestBudgetKwargsWiring:
    def test_context_budget_kwargs_carries_media_estimates(self):
        from types import SimpleNamespace

        from courtier.agent.api.services.agent_service import _context_budget_kwargs

        settings = SimpleNamespace(
            llm_context_window_tokens=32768,
            media_image_token_estimate=999,
            media_audio_tokens_per_second=11.0,
            media_video_tokens_per_second=22.0,
        )
        kwargs = _context_budget_kwargs(settings)
        assert kwargs["media_image_tokens"] == 999
        assert kwargs["media_audio_tokens_per_second"] == 11.0
        assert kwargs["media_video_tokens_per_second"] == 22.0
