"""M7 简版：命令行「评分点核对清单」渲染（纯模板，零智能）。

依据：requirements.md §7.2 / §7.3、docs/ARCHITECTURE.md §8.1 / §12.4 第 5 步、D-18。

这是 D5 门 ② 要看的产物：评分点 → 覆盖它的任务卡，以及**循环口径**的覆盖率数字。
展示口径与判定口径必须一致：分母 = ``status="normal"`` 的可拆点（§7.2）。
模糊点单列 "[?] 需组长确认"，不进分母、也不假装被解决。

M7 执行报告复用的就是本函数：多传一个 ``assignments`` 就多出执行阶段的
「负责人 + 完成」两列（**执行**口径）；默认不传，输出与 M3 时代逐字一致。
注意别把两件事混了 —— 原来的 ``[x] / [ ] / [?]`` 是**覆盖**（这个评分点有没有
被任务卡接住，拆解阶段），新加的「完成 n/m」是**执行**（卡有没有被标完成）。
"""

from __future__ import annotations

from typing import Sequence

from src.intelligence.coverage import balance_loop, coverage_loop
from src.intelligence.decompose import DecomposeResult
from src.models import BALANCE_LIMIT, AssignmentMeta, AssignmentRecord, Roster, RubricPoint, TaskCard

__all__ = ["render_checklist"]


def render_checklist(
    meta: AssignmentMeta,
    points: Sequence[RubricPoint],
    cards: Sequence[TaskCard],
    result: DecomposeResult,
    *,
    assignments: Sequence[AssignmentRecord] = (),
    roster: Roster | None = None,
) -> str:
    """``assignments`` / ``roster`` 都是**可选**的 keyword-only：

    传了 ``assignments`` 才渲染执行阶段那两列（M7 用）；``roster`` 只影响"负责人"显示
    人名还是 ``ou_xxx``（纯展示）。不传 = M3 时代的老输出，一个字都不变。
    """
    owners: dict[str, list[str]] = {}
    for card in cards:
        for ref in card.rubric_refs:
            owners.setdefault(ref, []).append(card.task_id)
    by_task = {record.task_id: record for record in (assignments or ())}

    coverage = coverage_loop(cards, points)
    # 空 deadline 不许显示成空字符串 —— 肉眼看不出来（D-49）
    deadline = (meta.deadline or "").strip() or "未标注"
    lines = [
        f"《{meta.title}》 {meta.course}｜交付：{meta.submission}｜截止：{deadline}",
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
        line = f"- {mark} {point.id}{weight}{tail}"
        if assignments:
            line += _execution_suffix(tasks, by_task, roster)
        lines.append(line)
        lines.append(f"      原文：{point.quote}")

    balance = balance_loop(cards)
    if coverage.eligible:
        coverage_line = (
            f"覆盖率：{len(coverage.covered)}/{len(coverage.eligible)} = {coverage.ratio:.0%}"
            "（循环口径：分母 = status=normal 的可拆点）"
        )
    else:
        coverage_line = "覆盖率：无可拆点 → 拒拆（不是 100%）"
    balance_line = (
        f"工时均衡：max/min = {balance.ratio:.2f}（上限 {BALANCE_LIMIT:g}）；"
        f"任务卡 {len(cards)} 张"
    )
    if result.generations:
        # M7 的执行报告是从盘上重读的产物，没有"这一版拆了几轮"这回事（generations=0）
        balance_line += f"；生成 {result.generations} 轮"
    lines += ["", coverage_line, balance_line]
    if result.failures:
        lines.append("自检未达标（按 D-18 交人决定）：" + "；".join(result.failures))
    else:
        lines.append("自检通过：可拆评分点全覆盖、工时均衡。")
    return "\n".join(lines)


def _execution_suffix(
    tasks: Sequence[str], by_task: dict[str, AssignmentRecord], roster: Roster | None
) -> str:
    """执行阶段的两列：``｜负责人：张三 ｜完成 1/2``（没人接就都是 ``—``）。"""
    if not tasks:
        return " ｜负责人：— ｜完成 —"
    names: list[str] = []
    done = 0
    for task_id in tasks:
        record = by_task.get(task_id)
        name = _name(record.assignee, roster) if record else "未分配"
        if name not in names:
            names.append(name)
        if record is not None and record.completed_at:
            done += 1
    return f" ｜负责人：{'、'.join(names)} ｜完成 {done}/{len(tasks)}"


def _name(open_id: str, roster: Roster | None) -> str:
    for member in getattr(roster, "members", None) or ():
        if member.open_id == open_id and member.name:
            return member.name
    return (open_id[:8] if open_id else "未分配")
