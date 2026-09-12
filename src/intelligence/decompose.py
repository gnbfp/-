"""M3 任务拆解 —— 自检判定与（后续）LLM 生成。

依据：requirements.md §7.3 / §7.4、docs/ARCHITECTURE.md §6.1 / §6.2、D-16 / D-18 / D-25。

分工写死在这里（B8）：**LLM 只生成任务卡，判定权在 ``check()``（代码）**。
所以本模块的判定部分是纯函数，可以完全离线、不花 token 地测。

``check()`` 返回**失败原因列表**而不是布尔：requirements §7.3 的流程要求
"失败详情喂回重拆"，布尔带不了详情；``if not check(...)`` 的用法与伪代码一致。
"""

from __future__ import annotations

from typing import Sequence

from src.intelligence.coverage import balance_loop, coverage_loop
from src.models import BALANCE_LIMIT, RubricPoint, TaskCard

__all__ = ["check"]


def check(cards: Sequence[TaskCard], rubric: Sequence[RubricPoint]) -> list[str]:
    """M3 自检循环的判定函数（D-16）。空列表 = 达标；非空 = 失败原因，喂回 LLM 重拆。

    规则（§7.3）：
      * ``cards`` 为空 → **短路判不达标**，不调 ``max()``（待定义-31，D-25）
      * 可拆点必须 100% 覆盖（分母 = ``status="normal"``）
      * ``max/min <= BALANCE_LIMIT``；工时取 0.5 地板防除零（``coverage.py``）
      * 全部评分点都是 ``ambiguous`` → 拒拆，转人工确认（§7.3 病态边界，与 T13 同类）
    """
    if not cards:
        return ["没有任何任务卡（cards 为空）"]

    coverage = coverage_loop(cards, rubric)
    if not coverage.eligible:
        return ["没有任何可拆评分点（全部为 ambiguous）→ 拒拆，转人工确认"]

    failures: list[str] = []
    if coverage.missing:
        failures.append(f"未覆盖的评分点：{'、'.join(coverage.missing)}")

    balance = balance_loop(cards)
    if not balance.ok:
        failures.append(
            f"工时不均衡：max/min = {balance.ratio:.2f} > {BALANCE_LIMIT:g}"
            f"（max={balance.max_hours:g}h，min={balance.min_hours:g}h）"
        )
    return failures
