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

__all__ = ["check", "decompose", "decompose_from_body", "DecomposeResult", "M3_SYSTEM", "M3_BODY_SYSTEM"]

M3_BODY_SYSTEM = """你是小组作业机器人里的「M3 任务拆解」模块。这份作业书里**没有评分标准**
（没有任何"评分项"可引用），所以任务卡只能**从正文的交付要求 / 任务描述 / 章节**来建。
只输出 JSON 对象，不要解释、不要 markdown 代码块：

{"cards": [{"task_id": "T1", "module_name": "撰写水质对比章节", "rubric_refs": [],
            "effort_hours": 6, "depends_on": [], "deliverable": "报告的第二章",
            "acceptance": "该卡来自正文要求，不是评分点，需组长确认：正文要求『…』"}]}

字段要求：
- task_id：从 T1 起连续编号，全局唯一。
- module_name：动词开头的模块名（如"撰写实验报告""整理监测数据"）。
- rubric_refs：**必须是空数组 []**。这份作业书没有评分点，**不许编造 R1/R2 之类的编号**。
- effort_hours：人时估计，最小 0.5；只用于均衡计算，不对外承诺。
- depends_on：前置 task_id 数组；空数组表示可立即开始。
- deliverable：这一块交什么（一个源文件 / 一段报告章节 / 一页 PPT）。
- acceptance：以「该卡来自正文要求，不是评分点，需组长确认」开头，再写怎么算做完。

硬约束：
1. 每条卡对应正文里一个**明确的交付物或任务**（要交的文件、要写的章节、要做的分析、
   要准备的演示…）。**不要凭空发明**作业书里没提到的任务。
2. 卡片数量以"每个组员都领得到活"为准；正文里的交付要求很少时，宁可少拆几张，
   也不要为凑数把一句话拆成四张。
3. 各卡 effort_hours 尽量均衡，最大 / 最小不超过 3 倍。
4. 依赖顺序合理：被依赖的卡先做。
5. 只做拆解：不要评价作业书、不要补写内容、不要提改进建议。"""

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
3. status="ambiguous" 的点：可以为它建卡（它对应的交付物可能真实存在），但卡的 acceptance 必须显式标注"该评分点为模糊要求，需组长/教师确认后方可核对"，不得声称已满足。模糊点不进覆盖率的分子分母，但它的卡照常计入均衡计算。
4. 卡片数量以"每个组员都领得到活"为准，不要为凑数拆碎，也不要漏掉交付物。
5. 依赖顺序合理：被依赖的卡先做。"""


@dataclass(frozen=True)
class DecomposeResult:
    """M3 产出：任务卡 + 最终自检结果。

    ``failures`` 非空 = 未达标，按 D-18 **输出当前最优 + 标注未达标项，由人决定**，
    不在这里替人拍板（B9）。

    ``mode``：``"rubric"`` = 常规（按评分点拆）；``"body"`` = **无评分点模式**
    （作业书里没有评分标准、组长确认「按正文拆」之后，从正文的交付要求建卡）。
    **纯内存字段、不落盘** —— 落盘侧的标记是 ``rubric.json`` 为空数组。
    """

    cards: tuple[TaskCard, ...]
    failures: tuple[str, ...]
    generations: int
    mode: str = "rubric"

    @property
    def ok(self) -> bool:
        return not self.failures


def _refusal(rubric: Sequence[RubricPoint], *, allow_empty_rubric: bool = False) -> str | None:
    """两种"不该拆"的清单，返回拒拆原因；可拆则 ``None``。

    * ``rubric`` 为空 → M1 没找到评分标准（D-48），**不许拿正文要求凑数**；
      但组长确认「按正文拆」之后走**无评分点模式**（``allow_empty_rubric=True``，
      口径 A，2026-09-16）—— 由正文交付要求建卡，卡片显式标注"不是评分点"；
    * 非空但全部 ``ambiguous`` → 没有可核对的点（§7.3 病态边界，与 T13 同类）。
      **无评分点模式不覆盖这一档**：那不是"没有评分标准"，而是"M1 一条都没判成可拆"，
      仍然交人确认（转人工，别自动往下走）。

    与 ``decompose()`` 共用同一份文案：拒拆理由只此一处，不会两边漂移。
    """
    if not rubric:
        if allow_empty_rubric:
            return None
        return "没有解析到评分点（作业书里没找到评分标准）→ 拒拆，转人工"
    if not any(point.status == "normal" for point in rubric):
        return "没有任何可拆评分点（全部为 ambiguous）→ 拒拆，转人工确认"
    return None


def check(
    cards: Sequence[TaskCard],
    rubric: Sequence[RubricPoint],
    *,
    allow_empty_rubric: bool = False,
) -> list[str]:
    """M3 自检循环的判定函数（D-16）。空列表 = 达标；非空 = 失败原因，喂回 LLM 重拆。

    规则（§7.3）：
      * ``rubric`` 为空 / 全部 ``ambiguous`` → **拒拆**（``_refusal()``）
      * ``cards`` 为空 → **短路判不达标**，不调 ``max()``（待定义-31，D-25）
      * 可拆点必须 100% 覆盖（分母 = ``status="normal"``）—— **无评分点模式跳过这条**
        （没有分母就没法算覆盖率，`checklist` 里显式标「不适用」，绝不假装 100%）
      * ``max/min <= BALANCE_LIMIT``；工时取 0.5 地板防除零（``coverage.py``）
      * ``depends_on`` 不能成环（F4）—— 成环的排期永远开不了工，必须喂回重拆
    """
    reason = _refusal(rubric, allow_empty_rubric=allow_empty_rubric)
    if reason:
        return [reason]
    if not cards:
        return ["没有任何任务卡（cards 为空）"]

    failures: list[str] = []
    if not allow_empty_rubric:
        coverage = coverage_loop(cards, rubric)
        if coverage.missing:
            failures.append(f"未覆盖的评分点：{'、'.join(coverage.missing)}")

    balance = balance_loop(cards)
    if not balance.ok:
        failures.append(
            f"工时不均衡：max/min = {balance.ratio:.2f} > {BALANCE_LIMIT:g}"
            f"（max={balance.max_hours:g}h，min={balance.min_hours:g}h）"
        )

    cycle = _find_cycle(cards)
    if cycle:
        failures.append(f"依赖成环：{' → '.join(cycle)}")
    return failures


def _find_cycle(cards: Sequence[TaskCard]) -> list[str] | None:
    """按 ``depends_on`` 构图找环（F4），返回环路径（如 ``[T1, T2, T1]``），无环则 ``None``。

    只看卡片之间的边：悬空 ID 已由 ``_validate_cards`` 挡下，自依赖也已由
    ``TaskCard.validate()`` 拒掉 —— 这里只负责"多节点互相等待"这种自检漏网的环。
    路径上不只是点名：把环写成 ``T1 → T2 → T1`` 喂回 LLM，比一句"有环"好修。
    """
    ids = {card.task_id for card in cards}
    graph = {
        card.task_id: [dep for dep in (card.depends_on or ()) if dep in ids]
        for card in cards
    }
    white, grey, black = 0, 1, 2
    color = {task_id: white for task_id in graph}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = grey
        stack.append(node)
        for dep in graph.get(node, ()):
            if color[dep] == grey:                 # 碰到正在走的点 ⇒ 成环
                return stack[stack.index(dep):] + [dep]
            if color[dep] == white:
                found = visit(dep)
                if found is not None:
                    return found
        stack.pop()
        color[node] = black
        return None

    for task_id in graph:
        if color[task_id] == white:
            found = visit(task_id)
            if found is not None:
                return found
    return None


def decompose(
    rubric: Sequence[RubricPoint], client: LLMClient, *, max_generations: int = 3
) -> DecomposeResult:
    """评分点清单 → 任务卡：生成 → ``check()`` → 失败详情喂回重拆。

    最多 ``max_generations`` 次生成（默认 3 = 初拆 + 两次带失败反馈的重拆，
    对齐 D-16 伪代码的 2 次 ``check()`` + 2 次重拆）；空 rubric 或全部 ambiguous
    时**不花 token**，直接拒拆（D-48 / §7.3 病态边界）。
    """
    reason = _refusal(rubric)
    if reason:
        return DecomposeResult(cards=(), failures=(reason,), generations=0)

    cards = _generate(rubric, client, feedback=None)
    generations = 1
    failures = tuple(check(cards, rubric))
    while failures and generations < max_generations:
        cards = _generate(rubric, client, feedback=failures, previous=cards)
        generations += 1
        failures = tuple(check(cards, rubric))
    return DecomposeResult(tuple(cards), failures, generations)


def decompose_from_body(
    text: str, client: LLMClient, *, max_generations: int = 3
) -> DecomposeResult:
    """**无评分点模式**（口径 A，2026-09-16）：正文 → 任务卡。

    只在一种情形下被调用：作业书里**没有评分标准**（M1 返回空 ``rubric``）、
    且**组长已经发过「按正文拆」确认**（网关侧的三重校验见 ``router._confirm_no_rubric``）。
    这里不自己判"能不能拆"，只负责"怎么拆"——判定权一直在代码与人手上（B8 / B9）。

    与 ``decompose()`` 的区别只有三处：
      * 提示词换 ``M3_BODY_SYSTEM``（从正文的交付要求建卡，卡片显式标注"不是评分点"）；
      * 卡片的 ``rubric_refs`` **必须是空数组**（没有评分点可溯源，不许编造编号）；
      * 自检跳过覆盖率那一条（没有分母 —— ``checklist`` 里标「不适用」，不假装 100%）。
    自检循环、重拆反馈、均衡与成环判据都**复用同一套代码**，不另写一份。

    仍是 **B2 的第三处 LLM 点（M3）**，没有新增调用点。
    """
    cards = _generate_from_body(text, client, feedback=None)
    generations = 1
    failures = tuple(check(cards, [], allow_empty_rubric=True))
    while failures and generations < max_generations:
        cards = _generate_from_body(text, client, feedback=failures, previous=cards)
        generations += 1
        failures = tuple(check(cards, [], allow_empty_rubric=True))
    return DecomposeResult(tuple(cards), failures, generations, mode="body")


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


def _generate_from_body(
    text: str,
    client: LLMClient,
    *,
    feedback: Sequence[str] | None = None,
    previous: Sequence[TaskCard] | None = None,
) -> list[TaskCard]:
    """无评分点模式的生成（正文直灌 + ``M3_BODY_SYSTEM``）。

    正文可能很长（几万字），但这里**不做截断**：截断等于让模型看不见后半段的交付要求，
    比多花点 token 更糟。真要限长，应该由调用方在抽取层决定。
    """
    user = "作业书正文如下（这份文件里没有评分标准）：\n\n" + text
    if feedback:
        user += "\n\n上一次拆解未通过自检：\n" + "\n".join(f"- {item}" for item in feedback)
    if feedback and previous:
        user += "\n\n上一次的任务卡（请在此基础上修正）：\n" + json.dumps(
            [card.to_dict() for card in previous], ensure_ascii=False
        )
    user += '\n\n请输出 JSON：{"cards": [...]}'

    def parse(response) -> list[TaskCard]:
        return _validate_cards(response, (), require_empty_refs=True)

    return client.chat_json(M3_BODY_SYSTEM, user, parse)


def _validate_cards(
    payload, rubric: Sequence[RubricPoint], *, require_empty_refs: bool = False
) -> list[TaskCard]:
    """schema 校验（不是覆盖率判定）：字段齐、取值合法、引用真实存在。

    ``require_empty_refs=True``（无评分点模式）：``rubric_refs`` 必须是空数组 ——
    这份作业书没有评分点，任何 ``R1``/``R2`` 都是模型编的。校验出来就喂回重拆，
    而不是"悄悄把编的引用清掉"（那等于替模型擦屁股、还掩盖了它不听话这件事）。
    """
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
        if require_empty_refs:
            if card.rubric_refs:
                problems.append(
                    f"cards[{index}]({card.task_id}) 的 rubric_refs 必须是空数组"
                    f"（这份作业书没有评分点，得到 {card.rubric_refs}）"
                )
        else:
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
