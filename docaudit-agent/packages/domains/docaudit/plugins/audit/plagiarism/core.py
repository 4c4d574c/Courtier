"""Document plagiarism detection based on similarity and substring matching.

Plagiarism ("查重") detection using SequenceMatcher similarity scoring,
Tukey's IQR dynamic thresholding, and optional MinHash pre-screening
via datasketch for large reference libraries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher

import numpy as np

logger = logging.getLogger(__name__)

# Named constants — empirically calibrated defaults
DEFAULT_SIMILARITY_THRESHOLD = 0.5
DEFAULT_IQR_MULTIPLIER = 1.5
DEFAULT_MIN_SUBSTRING_LENGTH = 20

@dataclass(frozen=True)
class PlagiarismResult:
    """Result of plagiarism detection for a single document."""

    is_plagiarism: bool
    max_similarity: float
    dynamic_threshold: float
    matched_doc_index: int
    matched_substring_length: int
    matched_text: str
    reason: str

# ======================== 相似度与长度计算函数 ========================

def compute_similarity_and_match(
    text1: str,
    text2: str,
) -> tuple[float, int, str]:
    """计算两段文本的相似度及最长公共子串。

    Args:
        text1: 待比较的字符串
        text2: 待比较的字符串

    Returns:
        (similarity, substring_length, matched_text)
        similarity: 0~1 之间的浮点数，表示字符级匹配程度
        substring_length: 最长连续匹配子串的字符数
        matched_text: 最长连续匹配子串的文本
    """
    sm = SequenceMatcher(None, text1, text2)
    match = sm.find_longest_match(0, len(text1), 0, len(text2))
    matched_text = text2[match.b : match.b + match.size]
    similarity = sm.ratio()
    return similarity, match.size, matched_text

# ======================== 文档库预处理 ========================

def build_library_distribution(
    library_docs: list[str],
    min_sim: float = 0.0,
) -> np.ndarray:
    """构建文档库内部的相似度分布（所有两两文档对的相似度）。

    注意：时间复杂度为 O(N^2 * M^2)，其中 N 为文档数量、M 为平均文档长度。
    对于大规模文档库（>500篇），建议使用 MinHash/LSH 预筛选，
    或将 library_similarities 缓存后增量更新。

    Args:
        library_docs: 文档库列表，每个元素为一个字符串
        min_sim: 只保留相似度大于该阈值的对，避免存储过多接近 0 的值

    Returns:
        numpy 数组，包含所有内部相似度分数
    """
    n = len(library_docs)
    similarities: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            sim, _, _ = compute_similarity_and_match(library_docs[i], library_docs[j])
            if sim > min_sim:
                similarities.append(sim)
    return np.array(similarities)

def compute_dynamic_threshold(
    similarities: np.ndarray,
    k: float = DEFAULT_IQR_MULTIPLIER,
) -> float:
    """基于 Tukey's IQR 方法计算动态阈值。

    阈值上限不超过 1.0（相似度为比率值，范围 [0, 1]）。

    Args:
        similarities: 相似度数组
        k: IQR 倍数，通常 1.5 用于温和离群点，2.0 或 3.0 更严格

    Returns:
        动态阈值，若输入数组为空则返回 DEFAULT_SIMILARITY_THRESHOLD
    """
    if len(similarities) == 0:
        return DEFAULT_SIMILARITY_THRESHOLD
    q1 = float(np.percentile(similarities, 25))
    q3 = float(np.percentile(similarities, 75))
    iqr = q3 - q1
    threshold = q3 + k * iqr
    # Clamp to valid similarity range [0, 1]
    return min(max(threshold, 0.0), 1.0)

# ======================== 核心检测函数 ========================

def _minhash_prescreen(
    new_doc: str,
    library_docs: list[str],
    k: int = 20,
) -> list[tuple[int, float]]:
    """Use MinHash LSH to find top-k candidate similar documents.

    Falls back to empty list if datasketch is not installed.
    Uses character 3-grams for MinHash signatures.
    """
    try:
        from datasketch import MinHash, MinHashLSH
    except ImportError:
        return []

    lsh = MinHashLSH(threshold=0.3, num_perm=128)
    minhashes = {}

    for idx, doc in enumerate(library_docs):
        m = MinHash(num_perm=128)
        for i in range(len(doc) - 2):
            m.update(doc[i:i + 3].encode("utf-8"))
        lsh.insert(f"doc_{idx}", m)
        minhashes[idx] = m

    query = MinHash(num_perm=128)
    for i in range(len(new_doc) - 2):
        query.update(new_doc[i:i + 3].encode("utf-8"))

    result = lsh.query(query)
    candidates = []
    for key in result:
        idx = int(key.split("_")[1])
        sim = query.jaccard(minhashes[idx])
        candidates.append((idx, sim))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:k]

def detect_plagiarism(
    new_doc: str,
    library_docs: list[str],
    library_similarities: np.ndarray | None = None,
    k: float = DEFAULT_IQR_MULTIPLIER,
    min_substring_length: int = DEFAULT_MIN_SUBSTRING_LENGTH,
) -> PlagiarismResult:
    """检测一篇新文档是否抄袭文档库中的内容。

    同时满足以下两个条件才判定为抄袭：
    1. 最大相似度超过动态阈值
    2. 最长公共子串长度超过最小长度约束

    Args:
        new_doc: 待检测的文本
        library_docs: 文档库列表
        library_similarities: 预先计算的库内相似度分布（若为 None 则自动计算）
        k: IQR 倍数
        min_substring_length: 最小连续匹配字符数（超过此长度才判为抄袭）

    Returns:
        PlagiarismResult 包含判定结果和详细信息

    Raises:
        ValueError: 如果 new_doc 为空字符串或 library_docs 为空列表
    """
    if not new_doc:
        raise ValueError("new_doc must not be empty")
    if not library_docs:
        raise ValueError("library_docs must not be empty")

    # 1. 若未提供内部相似度分布，则自动计算
    if library_similarities is None:
        library_similarities = build_library_distribution(library_docs)

    # 2. 计算动态阈值
    threshold = compute_dynamic_threshold(library_similarities, k)

    # 3. 将新文档与库内每篇文档比较
    # Use MinHash pre-screening for large libraries (>100 docs)
    if len(library_docs) > 100:
        candidates = _minhash_prescreen(new_doc, library_docs, k=20)
        docs_to_check = [(idx, library_docs[idx]) for idx, _ in candidates]
    else:
        docs_to_check = list(enumerate(library_docs))

    best_sim = 0.0
    best_idx = -1
    best_substring_length = 0
    best_matched_text = ""

    for idx, lib_doc in docs_to_check:
        if not lib_doc:
            continue
        sim, substring_length, matched_text = compute_similarity_and_match(
            new_doc, lib_doc
        )
        if sim > best_sim:
            best_sim = sim
            best_idx = idx
            best_substring_length = substring_length
            best_matched_text = matched_text

    # 4. 应用动态阈值与长度约束
    if best_sim <= threshold:
        return PlagiarismResult(
            is_plagiarism=False,
            max_similarity=best_sim,
            dynamic_threshold=threshold,
            matched_doc_index=best_idx,
            matched_substring_length=best_substring_length,
            matched_text=best_matched_text,
            reason="similarity below dynamic threshold",
        )

    if best_substring_length < min_substring_length:
        return PlagiarismResult(
            is_plagiarism=False,
            max_similarity=best_sim,
            dynamic_threshold=threshold,
            matched_doc_index=best_idx,
            matched_substring_length=best_substring_length,
            matched_text=best_matched_text,
            reason=f"substring length {best_substring_length} < {min_substring_length}",
        )

    return PlagiarismResult(
        is_plagiarism=True,
        max_similarity=best_sim,
        dynamic_threshold=threshold,
        matched_doc_index=best_idx,
        matched_substring_length=best_substring_length,
        matched_text=best_matched_text,
        reason="exceeds both threshold and substring length",
    )
