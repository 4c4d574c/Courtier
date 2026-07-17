"""Long-session memory recall benchmark.

This benchmark measures how much the LLM context window shrinks when
retrieval-augmented memory is used instead of keeping the full message history.

Usage:
    cd courtier
    uv run python benchmarks/long_session_memory_benchmark.py \
        --turns 30 --recall-turns "5,10,15,20,25"

The script synthesizes a conversation where each turn appends a chunk of
domain text.  It compares:

  baseline: all messages kept in the working context.
  with_memory: only a system prompt, the latest user query, and the top-k
               recalled memories are included.

The metric is an approximate token count (1 token ~= 4 characters for CJK/
mixed text).  A real deployment should replace this with the configured
model's tokenizer.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import tempfile
from pathlib import Path

from courtier.agent.core.memory_manager import MemoryManager, MemoryQuery
from courtier.agent.core.state import Message

SYSTEM_PROMPT = (
    "你是 Courtier 文档审核助手。请根据用户提供的政府公文和相关规则完成审核任务。"
)

USER_TASK = "请检查这份公文是否符合 GB/T 9704-2012 格式要求。"

DOMAIN_FACTS = [
    "公文版头应包含发文机关标志、发文字号和签发人。",
    "发文字号由发文机关代字、年份和序号组成，年份应标全称。",
    "正文结构层次序数依次用一、（一）1.（1）标注。",
    "成文日期中的数字用阿拉伯数字将年、月、日标全。",
    "附件说明在正文下空一行左空二字编排。",
    "印章端正、居中下压发文机关署名和成文日期。",
    "页码用4号半角宋体阿拉伯数字，单页居右空一字，双页居左空一字。",
    "抄送机关左右各空一字，后标全角冒号。",
]


def approx_tokens(text: str) -> int:
    """Approximate token count.

    Uses 4 characters per token as a rough estimator for mixed CJK/ASCII
    text.  Replace with tiktoken or the model tokenizer for accurate numbers.
    """
    return max(1, len(text) // 4)


def build_messages(full_history: list[Message]) -> list[Message]:
    """Baseline: keep the entire conversation history."""
    return [Message(role="system", content=SYSTEM_PROMPT), *full_history]


async def build_messages_with_memory(
    manager: MemoryManager,
    full_history: list[Message],
    top_k: int,
) -> list[Message]:
    """Memory-augmented: keep system prompt + last turn + recalled memories."""
    memories = await manager.retrieve(MemoryQuery(text=USER_TASK, top_k=top_k))
    memory_block = "\n".join(
        f"- {m.value}" for m in memories
    )
    messages: list[Message] = [Message(role="system", content=SYSTEM_PROMPT)]
    if memory_block:
        messages.append(
            Message(
                role="system",
                content=f"[相关记忆]\n{memory_block}",
            )
        )
    # Keep only the latest user turn so the model can answer the current task.
    last_user = next(
        (m for m in reversed(full_history) if m.role == "user"),
        None,
    )
    if last_user:
        messages.append(last_user)
    return messages


async def run_turn(
    turn_index: int,
    manager: MemoryManager,
    full_history: list[Message],
    top_k: int,
) -> tuple[int, int]:
    """Run one synthetic turn and return (baseline_tokens, memory_tokens)."""
    fact = random.choice(DOMAIN_FACTS)
    full_history.append(Message(role="user", content=f"第{turn_index + 1}轮：{USER_TASK}"))
    full_history.append(
        Message(
            role="assistant",
            content=f"已记录。补充规则：{fact}",
        )
    )

    # Store each new fact into long-term memory.
    await manager.long_term_set(f"fact_{turn_index}", fact)

    baseline = build_messages(full_history)
    with_memory = await build_messages_with_memory(manager, full_history, top_k)

    baseline_tokens = sum(approx_tokens(m.content or "") for m in baseline)
    memory_tokens = sum(approx_tokens(m.content or "") for m in with_memory)
    return baseline_tokens, memory_tokens


async def benchmark(turns: int, recall_turns: list[int], top_k: int) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MemoryManager(cache_dir=str(Path(tmpdir) / "cache"))
        full_history: list[Message] = []

        baseline_series: list[int] = []
        memory_series: list[int] = []

        for turn in range(turns):
            baseline_tok, memory_tok = await run_turn(
                turn, manager, full_history, top_k
            )
            baseline_series.append(baseline_tok)
            memory_series.append(memory_tok)

        final_baseline = baseline_series[-1]
        final_memory = memory_series[-1]
        reduction = (
            (final_baseline - final_memory) / final_baseline * 100
            if final_baseline
            else 0.0
        )

        print(f"Turns: {turns}")
        print(f"Top-k memories per recall: {top_k}")
        print(f"Final baseline tokens: {final_baseline}")
        print(f"Final memory tokens:   {final_memory}")
        print(f"Reduction:             {reduction:.1f}%")
        print(
            f"Mean tokens/turn baseline: {statistics.mean(baseline_series):.0f}"
        )
        print(
            f"Mean tokens/turn memory:   {statistics.mean(memory_series):.0f}"
        )

        if recall_turns:
            print("\nPer-recall-turn snapshot:")
            for rt in recall_turns:
                if 1 <= rt <= turns:
                    idx = rt - 1
                    print(
                        f"  turn {rt:3d}: baseline={baseline_series[idx]:5d} "
                        f"memory={memory_series[idx]:5d}"
                    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark long-session memory recall token savings."
    )
    parser.add_argument("--turns", type=int, default=30, help="Number of turns.")
    parser.add_argument(
        "--recall-turns",
        type=str,
        default="5,10,15,20,25,30",
        help="Comma-separated turn numbers to print snapshots for.",
    )
    parser.add_argument(
        "--top-k", type=int, default=3, help="Number of memories to recall."
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    args = parser.parse_args()

    random.seed(args.seed)
    recall_turns = [int(x) for x in args.recall_turns.split(",") if x.strip()]
    asyncio.run(benchmark(args.turns, recall_turns, args.top_k))


if __name__ == "__main__":
    main()
