"""M3 自检循环的两个判定函数 —— 覆盖率与均衡度。

依据：requirements.md §7.2 / §7.3 / §7.4、docs/ARCHITECTURE.md §6.1 / §6.2、
D-16 / D-17 / D-19 / D-25。

为什么是**两个互不复用**的函数（D-17）：
  * ``coverage_loop()`` —— 循环退出判据。分母 = ``status="normal"`` 的可拆评分点，
    要求 100%（``eligible <= covered`` 的子集判定）。
  * ``balance_loop()``  —— 循环退出判据。``max/min <= BALANCE_LIMIT``。
  * M8 的两套评测口径（人工标注分母 / 归一化极差 ``1-(max-min)/Σ``）**不在这里**，
    在 ``eval/`` 独立实现（D-19）。共用函数就等于自己改自己的卷子。

判定权在代码、不在 LLM（B8）。本模块是纯函数：不读文件、不发消息、不碰 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.models import BALANCE_LIMIT, EFFORT_HOURS_FLOOR, RubricPoint, TaskCard

__all__ = ["CoverageResult", "BalanceResult", "coverage_loop", "balance_loop"]


@dataclass(frozen=True)
class CoverageResult:
    """循环内覆盖率判定结果。

    ``eligible`` = 分母（``status="normal"`` 的可拆点 id，去重排序）；
    ``covered``  = 分子（被任务卡引用、且确实在分母里的 id，去重排序）。
    任务卡多引用模糊点或未知 id 不算错 —— §7.3 明确用子集判定，它们也不进分子。
    """

    eligible: tuple[str, ...]
    covered: tuple[str, ...]

    @property
    def missing(self) -> tuple[str, ...]:
        """未被任何任务卡覆盖的可拆点 id。"""
        covered = set(self.covered)
        return tuple(r for r in self.eligible if r not in covered)

    @property
    def ratio(self) -> float:
        """覆盖率 = 已覆盖的可拆点 / 可拆点。**分母为空 → 0.0**（空分母不是达标）。"""
        if not self.eligible:
            return 0.0
        return len(self.covered) / len(self.eligible)

    @property
    def ok(self) -> bool:
        """循环退出判据：可拆点 100% 覆盖（§7.2 / §7.3）。**空分母 → 不达标**。"""
        return bool(self.eligible) and not self.missing


@dataclass(frozen=True)
class BalanceResult:
    """循环内均衡度判定结果（口径 = ``max/min <= 3``，D-17）。

    ``hours`` 是**已按 0.5 地板抬升**的工时。空 ``cards`` 取空真 ``ok=True``：
    空集由 ``decompose.check()`` 短路判不达标，这里只保证函数全定义、不抛异常。
    """

    hours: tuple[float, ...]

    @property
    def max_hours(self) -> float:
        return max(self.hours, default=0.0)

    @property
    def min_hours(self) -> float:
        return min(self.hours, default=0.0)

    @property
    def ratio(self) -> float:
        if not self.hours:
            return 1.0
        return self.max_hours / self.min_hours

    @property
    def ok(self) -> bool:
        return self.ratio <= BALANCE_LIMIT


def coverage_loop(cards: Sequence[TaskCard], rubric: Sequence[RubricPoint]) -> CoverageResult:
    """循环内覆盖率：分母 = ``status="normal"`` 的可拆评分点（§7.2 / §7.3）。"""
    eligible = sorted({p.id for p in rubric if p.status == "normal"})
    refs = {ref for card in cards for ref in card.rubric_refs}
    covered = sorted(r for r in eligible if r in refs)
    return CoverageResult(eligible=tuple(eligible), covered=tuple(covered))


def balance_loop(cards: Sequence[TaskCard]) -> BalanceResult:
    """循环内均衡度：``max/min <= BALANCE_LIMIT``，工时取 0.5 地板防除零（§7.3 / §7.4）。"""
    hours = tuple(max(card.effort_hours, EFFORT_HOURS_FLOOR) for card in cards)
    return BalanceResult(hours=hours)
