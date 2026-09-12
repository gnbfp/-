"""命令行主链路入口 —— 作业书 → 评分点 → 任务卡 → 核对清单 + 覆盖率。

依据：requirements.md §1(B6) / §7.1、docs/ARCHITECTURE.md §8.1 / §12.3。
D5 门 ②「主链路命令行跑通」验的就是它：

    python -m src.main --file 作业书.pdf

退出码：0 = 跑完且自检达标；1 = 跑完但自检未达标（按 D-18 交人决定）；
2 = 硬失败（配置缺失 / 文件拒收 / LLM 连续失败）。落盘只有仓库根 ``data/``（D-30）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.config import ConfigError, load_config
from src.intelligence.decompose import decompose
from src.intelligence.extract import ExtractError, check_weight_sum, extract_text
from src.intelligence.llm import LLMClient, LLMError
from src.intelligence.parse import parse_assignment
from src.report.checklist import render_checklist
from src.storage import JsonStore


def _progress(message: str) -> None:
    print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="作业书 → 评分点 → 任务卡 → 核对清单")
    parser.add_argument("--file", required=True, help="作业书文件（PDF / DOCX / TXT）")
    args = parser.parse_args(argv)

    try:
        config = load_config()
        config.check_llm()
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    try:
        text = extract_text(Path(args.file))
    except ExtractError as exc:
        print(f"[拒收] {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[读文件失败] {exc}", file=sys.stderr)
        return 2
    _progress(f"[M1 前置] 抽取文本 {len(text)} 字")

    client = LLMClient.from_config(config)
    try:
        _progress("[M1] 解析评分点…")
        parsed = parse_assignment(text, client)
        _progress(f"[M1] 评分点 {len(parsed.points)} 条、作业元信息已抽出")
        _progress("[M3] 拆解 + 自检循环…")
        result = decompose(parsed.points, client)
    except LLMError as exc:
        print(f"[LLM 失败，降级不猜] {exc}", file=sys.stderr)
        return 2

    store = JsonStore(config.data_dir)
    store.ensure_dirs()
    store.save_assignment(parsed.meta)
    store.save_rubric(list(parsed.points))
    store.save_cards(list(result.cards))
    _progress(f"[落盘] {store.root}")

    print(render_checklist(parsed.meta, parsed.points, result.cards, result))
    warning = check_weight_sum(parsed.points)
    if warning:
        print(f"\n[软警告] {warning}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
