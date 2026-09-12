"""M7 简版：命令行「评分点核对清单」渲染（纯模板，零智能）。

依据：requirements.md §7.2 / §7.3、docs/ARCHITECTURE.md §8.1 / §12.4 第 5 步、D-18。

这是 D5 门 ② 要看的产物：评分点 → 覆盖它的任务卡，以及**循环口径**的覆盖率数字。
展示口径与判定口径必须一致：分母 = ``status="normal"`` 的可拆点（§7.2）。
模糊点单列 "[?] 需组长确认"，不进分母、也不假装被解决。

完整版执行报告（分配总表 + 甘特图 + 全员核对清单）是 M7 的后续工作，不在这里。
"""

from __future__ import annotations

from typing import Sequence

from src.intelligence.coverage import balance_loop, coverage_loop
from src.intelligence.decompose import DecomposeResult
from src.models import BALANCE_LIMIT, AssignmentMeta, RubricPoint, TaskCard

__all__ = ["render_checklist"]


def render_checklist(
    meta: AssignmentMeta,
    points: Sequence[RubricPoint],
    cards: Sequence[TaskCard],
    result: DecomposeResult,
) -> str:
    owners: dict[str, list[str]] = {}
    for card in cards:
        for ref in card.rubric_refs:
            owners.setdefault(ref, []).append(card.task_id)

    coverage = coverage_loop(cards, points)
    lines = [
        f"《{meta.title}》 {meta.course}｜交付：{meta.submission}｜截止：{meta.deadline}",
        "",
        "评分点核对清单",
    ]
    for point in points:
        weight = f"（{point.weight:g}）" if point.weight is not None else ""
        tasks = owners.get(point.id, [])
        if point.status != "normal":
            tail = (
                "需组长确认（模糊要求，未进循环分母）"
                if not tasks
                else f"需组长确认（模糊要求；已被 {'、'.join(tasks)} 引用）"
            )
            mark = "[?]"
        else:
            mark = "[x]" if tasks else "[ ]"
            tail = "→ " + "、".join(tasks) if tasks else "未覆盖"
        lines.append(f"- {mark} {point.id}{weight}{tail}")
        lines.append(f"      原文：{point.quote}")

    balance = balance_loop(cards)
    if coverage.eligible:
        coverage_line = (
            f"覆盖率：{len(coverage.covered)}/{len(coverage.eligible)} = {coverage.ratio:.0%}"
            "（循环口径：分母 = status=normal 的可拆点）"
        )
    else:
        coverage_line = "覆盖率：无可拆点 → 拒拆（不是 100%）"
    lines += [
        "",
        coverage_line,
        f"工时均衡：max/min = {balance.ratio:.2f}（上限 {BALANCE_LIMIT:g}）；"
        f"任务卡 {len(cards)} 张；生成 {result.generations} 轮",
    ]
    if result.failures:
        lines.append("自检未达标（按 D-18 交人决定）：" + "；".join(result.failures))
    else:
        lines.append("自检通过：可拆评分点全覆盖、工时均衡。")
    return "\n".join(lines)
