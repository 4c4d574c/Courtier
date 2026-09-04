"""Direct unit tests for loop_utils pure helpers."""

from courtier.agent.core.loop_utils import (
    fmt_size,
    normalize_args_for_dedup,
    similarity,
    truncate_data,
)


class TestFmtSize:
    def test_small(self):
        assert fmt_size(42) == "42 字符"

    def test_k_range_one_decimal(self):
        assert fmt_size(1500) == "1.5k 字符"

    def test_10k_and_above_integer_k(self):
        assert fmt_size(10_000) == "10k 字符"
        assert fmt_size(123_456) == "123k 字符"


class TestSimilarity:
    def test_both_empty_is_zero(self):
        # Two empty strings carry no loop-detection signal.
        assert similarity("", "") == 0.0

    def test_one_empty_is_zero(self):
        assert similarity("abc", "") == 0.0

    def test_identical_is_one(self):
        assert similarity("same text", "same text") == 1.0

    def test_different_is_partial(self):
        score = similarity("abcdef", "abcxyz")
        assert 0.0 < score < 1.0


class TestNormalizeArgsForDedup:
    def test_strips_transient_keys(self):
        a = normalize_args_for_dedup(
            {"path": "x.md", "label": "L1", "_timestamp": 1, "_request_id": "r1"}
        )
        b = normalize_args_for_dedup({"path": "x.md", "label": "L2"})
        assert a == b

    def test_key_order_independent(self):
        a = normalize_args_for_dedup({"a": 1, "b": 2})
        b = normalize_args_for_dedup({"b": 2, "a": 1})
        assert a == b

    def test_keeps_semantic_keys(self):
        a = normalize_args_for_dedup({"path": "a.md"})
        b = normalize_args_for_dedup({"path": "b.md"})
        assert a != b


class TestTruncateData:
    def test_short_string_unchanged(self):
        assert truncate_data("short", 100) == "short"

    def test_long_string_sliced_to_char_budget(self):
        out = truncate_data("x" * 1000, 25)
        assert out == "x" * 100  # 25 tokens * 4 chars/token

    def test_list_that_fits_unchanged(self):
        data = [1, 2, 3]
        assert truncate_data(data, 100) == data

    def test_list_over_budget_gets_omitted_marker(self):
        data = [{"big": "y" * 200} for _ in range(5)]
        out = truncate_data(data, 50)
        assert isinstance(out, list)
        assert len(out) < 5
        assert out[-1]["_truncated"] is True
        assert out[-1]["omitted_count"] == 5 - (len(out) - 1)

    def test_dict_that_fits_unchanged(self):
        data = {"a": 1, "b": 2}
        assert truncate_data(data, 100) == data

    def test_dict_over_budget_lists_omitted_keys(self):
        data = {f"k{i}": "v" * 100 for i in range(10)}
        out = truncate_data(data, 50)
        assert out["_truncated"] is True
        assert "_omitted_keys" in out
        assert out["_total_keys"] == 10
        # Every omitted key is accounted for: kept + omitted == all keys.
        kept = set(out) - {"_truncated", "_omitted_keys", "_total_keys"}
        assert kept | set(out["_omitted_keys"]) == set(data)

    def test_non_string_scalar_becomes_sliced_str(self):
        # Ints fall through to the str branch: 2 tokens ≈ 8 chars.
        assert truncate_data(12345678, 2) == "12345678"
