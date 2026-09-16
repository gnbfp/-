"""命令行主链路入口 —— 作业书 → 评分点 → 任务卡 → 核对清单 + 覆盖率。

依据：requirements.md §1(B6) / §7.1、docs/ARCHITECTURE.md §8.1 / §12.3。
D5 门 ②「主链路命令行跑通」验的就是它：

    python -m src.main --file 作业书.pdf

M7 执行报告也能离线复现（只读 ``data/``、不联网、零 token）：

    python -m src.main --report

退出码：0 = 跑完且自检达标；1 = 跑完但自检未达标（按 D-18 交人决定）；
2 = 硬失败（配置缺失 / 文件拒收 / LLM 连续失败）。落盘只有仓库根 ``data/``（D-30）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.config import ConfigError, load_config
from src.gateway import allocation
from src.intelligence.decompose import DecomposeResult, check, decompose
from src.intelligence.extract import (
    ExtractError,
    check_deadline,
    check_meta_fields,
    check_weight_sum,
    extract_text,
)
from src.intelligence.llm import LLMClient, LLMError
from src.intelligence.parse import parse_assignment
from src.report.checklist import render_checklist
from src.report.gantt import render_gantt
from src.storage import GANTT, JsonStore


def _progress(message: str) -> None:
    print(message, file=sys.stderr)


def _report(config) -> int:
    """M7 执行报告（D-64 / D-65）—— 只读 ``data/``，命令行复现群里那份产物。"""
    store = JsonStore(config.data_dir)
    meta = store.load_assignment()
    points = store.load_rubric()
    cards = store.load_cards()
    assignments = store.load_assignments()
    roster = store.load_members()
    if meta is None or not points or not cards or not assignments:
        print(
            "[报告] data/ 里还缺产物（作业书 / 评分点 / 任务卡 / 分配）—— 先跑主链路与 M4。",
            file=sys.stderr,
        )
        return 2
    # 自检项按现状重算：报告是快照，不是拆解（generations=0）
    result = DecomposeResult(
        cards=tuple(cards), failures=tuple(check(cards, points)), generations=0
    )
    print(
        "\n".join(
            [
                allocation.render_board(
                    assignments, cards, roster, store.load_preferences(), show_completion=True
                ),
                "",
                render_checklist(
                    meta, points, cards, result, assignments=assignments, roster=roster
                ),
            ]
        )
    )
    gantt = render_gantt(cards, assignments, meta, store.path(GANTT), roster)
    _progress(f"[落盘] {gantt}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="作业书 → 评分点 → 任务卡 → 核对清单")
    parser.add_argument("--file", help="作业书文件（PDF / DOCX / TXT）")
    parser.add_argument(
        "--report",
        action="store_true",
        help="只读 data/ 打印 M7 执行报告 + 生成 data/gantt.png（不联网、零 token）",
    )
    args = parser.parse_args(argv)
    if not args.file and not args.report:
        parser.error("要么给 --file（主链路），要么给 --report（执行报告）")

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    if args.report:
        return _report(config)

    try:
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
        parsed = parse_assignment(text, client, source_file=Path(args.file).name)
        _progress(f"[M1] 评分点 {len(parsed.points)} 条、作业元信息已抽出")
        _progress("[M3] 拆解 + 自检循环…")
        result = decompose(parsed.points, client)
    except LLMError as exc:
        print(f"[LLM 失败，降级不猜] {exc}", file=sys.stderr)
        return 2

    if parsed.points:
        store = JsonStore(config.data_dir)
        store.ensure_dirs()
        store.save_assignment(parsed.meta)
        store.save_rubric(list(parsed.points))
        store.save_cards(list(result.cards))
        _progress(f"[落盘] {store.root}")
    else:
        # 没找到评分标准 → 拒拆不写盘，保留上一份产物（D-49 ④，与 gateway 同口径）
        _progress("[落盘] 没找到评分标准 → 拒拆不写盘，保留上一份产物")

    print(render_checklist(parsed.meta, parsed.points, result.cards, result))
    for warning in (
        check_weight_sum(parsed.points),
        check_deadline(parsed.meta),
        check_meta_fields(parsed.meta),
    ):
        if warning:
            print(f"\n[软警告] {warning}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
