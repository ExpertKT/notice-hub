#!/usr/bin/env python3
"""Exercise the real local Ollama refinement path with representative notices."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qq_digest import Message, refine_with_dashscope  # noqa: E402


ENDPOINT = os.getenv(
    "QQ_DIGEST_LLM_ENDPOINT",
    "http://127.0.0.1:11434/v1/chat/completions",
)
MODEL = os.getenv("QQ_DIGEST_LLM_MODEL", "qwen3.6:35b-a3b-q4_k_m-gpu20")
TIMEOUT = max(10, int(os.getenv("QQ_DIGEST_LLM_TIMEOUT", "600")))


def main() -> None:
    messages = [
        Message(
            dt.datetime(2026, 10, 7, 9, 15),
            "高等数学老师",
            "提醒：高等数学第三章作业请在本周五（10月9日）24:00前提交到学习通，文件命名为班级-学号-姓名。逾期系统关闭。",
            "smoke",
        ),
        Message(
            dt.datetime(2026, 10, 7, 10, 20),
            "辅导员王老师",
            "请2024级转专业到本学院的同学核对教务系统培养方案，只有系统里没有2026年体测成绩的同学需要在今晚20:00前填写补测问卷，其他同学不用填。",
            "smoke",
        ),
        Message(
            dt.datetime(2026, 10, 7, 11, 5),
            "班长",
            "大家收到后回复1，谢谢！",
            "smoke",
        ),
    ]
    items = [
        {
            "message": message,
            "category": "action" if index == 1 else "academic" if index == 2 else "info",
            "deadline": None,
            "deadline_dt": None,
            "score": 5,
        }
        for index, message in enumerate(messages, 1)
    ]
    raw: list[str] = []
    try:
        refined = refine_with_dashscope(
            items,
            api_key=os.getenv("DASHSCOPE_API_KEY", "ollama-local"),
            model=MODEL,
            endpoint=ENDPOINT,
            timeout=TIMEOUT,
            retries=0,
            raw_response=raw,
        )
    finally:
        print("=== RAW RESPONSE ===")
        print(raw[-1] if raw else "<no response captured>")
    print("=== PARSED ITEMS ===")
    print(json.dumps(refined, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
