"""M3 任务拆解 —— 自检判定与 LLM 生成。

依据：requirements.md §7.3 / §7.4、docs/ARCHITECTURE.md §6.2、定稿文档 §3.4、
D-16 / D-18 / D-25。

分工写死在这里（B8）：**LLM 只生成任务卡，判定权在 ``check()``（代码）**。
所以判定部分是纯函数，可以完全离线、不花 token 地测；``decompose()`` 才碰网络。

``check()`` 返回**失败原因列表**而不是布尔：requirements §7.3 的流程要求
"失败详情喂回重拆"，布尔带不了详情；``if not check(...)`` 的用法与伪代码一致。

**few-shot 待补（9/14 排期）**：定稿 §3.4 要求放 2–3 份人工精拆范例进 prompt，
这是"最值得打磨的资产"。目前 ``tests/fixtures/`` 为空、尚无真实范例，
所以 ``M3_SYSTEM`` 里**不放编造的示例**；范例入库后追加到 ``M3_SYSTEM`` 之后即可。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from src.intelligence.coverage import balance_loop, coverage_loop
from src.intelligence.llm import LLMClient, LLMOutputError, as_number
from src.models import BALANCE_LIMIT, RubricPoint, SchemaError, TaskCard

__all__ = ["check", "decompose", "DecomposeResult", "M3_SYSTEM"]

M3_SYSTEM = """你是小组作业机器人里的「M3 任务拆解」模块，唯一任务是把评分点清单拆成人能认领的任务卡。
只输出 JSON 对象，不要解释、不要 markdown 代码块：

{"cards": [{"task_id": "T1", "module_name": "实现登录模块", "rubric_refs": ["R1", "R3"],
            "effort_hours": 6, "depends_on": [], "deliverable": "一个源文件",
            "acceptance": "从 R1 原文改写，可被核对清单复用"}]}

字段要求：
- task_id：从 T1 起连续编号，全局唯一。
- module_name：动词开头的模块名（如"实现登录模块""撰写实验报告"）。
- rubric_refs：必填，只能引用清单里真实存在的评分点 id，不能为空数组。
- effort_hours：人时估计，最小 0.5；只用于均衡计算，不对外承诺。
- depends_on：前置 task_id 数组；空数组表示可立即开始。
- deliverable：这一块交什么（一个源文件 / 一段报告章节 / 一页 PPT）。
- acceptance：怎么算做完，从对应评分点原文改写，能直接进核对清单。

硬约束：
1. 每一条 status="normal" 的评分点至少被一张卡引用，必须全覆盖（这是硬性验收项）。
2. 各卡 effort_hours 尽量均衡，最大 / 最小不超过 3 倍。
3. status="ambiguous" 的点不要硬拆，不要在卡里假装解决了它。
4. 卡片数量以"每个组员都领得到活"为准，不要为凑数拆碎，也不要漏掉交付物。
5. 依赖顺序合理：被依赖的卡先做。"""


@dataclass(frozen=True)
class DecomposeResult:
    """M3 产出：任务卡 + 最终自检结果。

    ``failures`` 非空 = 未达标，按 D-18 **输出当前最优 + 标注未达标项，由人决定**，
    不在这里替人拍板（B9）。
    """

    cards: tuple[TaskCard, ...]
    failures: tuple[str, ...]
    generations: int

    @property
    def ok(self) -> bool:
        return not self.failures


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


def decompose(
    rubric: Sequence[RubricPoint], client: LLMClient, *, max_rounds: int = 2
) -> DecomposeResult:
    """评分点清单 → 任务卡：生成 → ``check()`` →失败详情喂回重拆。

    "最多 2 轮"按 **至多 2 次生成**（初拆 + 一次带失败反馈的重拆）实现；
    全部 ambiguous 时不花 token，直接拒拆（§7.3 病态边界）。
    """
    if not any(point.status == "normal" for point in rubric):
        return DecomposeResult(
            cards=(),
            failures=("没有任何可拆评分点（全部为 ambiguous）→ 拒拆，转人工确认",),
            generations=0,
        )

    cards = _generate(rubric, client, feedback=None)
    generations = 1
    failures = tuple(check(cards, rubric))
    while failures and generations < max_rounds:
        cards = _generate(rubric, client, feedback=failures, previous=cards)
        generations += 1
        failures = tuple(check(cards, rubric))
    return DecomposeResult(tuple(cards), failures, generations)


def _generate(
    rubric: Sequence[RubricPoint],
    client: LLMClient,
    *,
    feedback: Sequence[str] | None = None,
    previous: Sequence[TaskCard] | None = None,
) -> list[TaskCard]:
    payload = [point.to_dict() for point in rubric]
    user = "评分点清单（JSON）：\n" + json.dumps(payload, ensure_ascii=False)
    if feedback:
        user += "\n\n上一次拆解未通过自检：\n" + "\n".join(f"- {item}" for item in feedback)
    if feedback and previous:
        user += "\n\n上一次的任务卡（请在此基础上修正）：\n" + json.dumps(
            [card.to_dict() for card in previous], ensure_ascii=False
        )
    user += '\n\n请输出 JSON：{"cards": [...]}'

    def parse(response) -> list[TaskCard]:
        return _validate_cards(response, rubric)

    return client.chat_json(M3_SYSTEM, user, parse)


def _validate_cards(payload, rubric: Sequence[RubricPoint]) -> list[TaskCard]:
    """schema 校验（不是覆盖率判定）：字段齐、取值合法、引用真实存在。"""
    problems: list[str] = []
    if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list) or not payload["cards"]:
        raise LLMOutputError("顶层必须是 {\"cards\": [...]}，且 cards 非空")

    known = {point.id for point in rubric}
    cards: list[TaskCard] = []
    for index, item in enumerate(payload["cards"]):
        if not isinstance(item, dict):
            problems.append(f"cards[{index}] 不是对象")
            continue
        data = dict(item)
        if "effort_hours" in data:
            data["effort_hours"] = as_number(data["effort_hours"])
        try:
            card = TaskCard.from_dict(data)
            card.validate()
        except (SchemaError, TypeError, ValueError) as exc:
            problems.append(f"cards[{index}] 字段不合法：{exc}")
            continue
        for name in ("module_name", "deliverable", "acceptance"):
            if not getattr(card, name).strip():
                problems.append(f"cards[{index}]({card.task_id}) 的 {name} 不能为空")
        unknown = sorted(set(card.rubric_refs) - known)
        if unknown:
            problems.append(f"cards[{index}]({card.task_id}) 引用了不存在的评分点：{unknown}")
        cards.append(card)

    ids = [card.task_id for card in cards]
    duplicates = sorted({tid for tid in ids if ids.count(tid) > 1})
    if duplicates:
        problems.append(f"task_id 重复：{duplicates}")
    dangling = sorted({dep for card in cards for dep in card.depends_on} - set(ids))
    if dangling:
        problems.append(f"depends_on 指向不存在的 task_id：{dangling}")

    if problems:
        raise LLMOutputError("；".join(problems))
    return cards
