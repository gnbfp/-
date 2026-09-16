"""M2 方向候选生成 —— B2 三处 LLM 调用点的第 2 处（M1 / M2 / M3）。

依据：`requirements.md` §3(S5) / §4(M2 优先级) / §7.1(投票窗口) / §7.6(「方向」共识) /
D-33 / D-35 / D-36、`docs/ARCHITECTURE.md` §4、M2 方案 §2.1。

边界（B2 / §1）：**只有"生成候选方向"这一步碰 LLM**。计票、门槛、封盘、超时全在
`src/gateway/vote.py`（纯规则、零智能）；`src/gateway/` 里一行 LLM 调用都不许有。

与 M3 同一套范式（`decompose.py` / `llm.py`）：`chat_json` 要一次合法 JSON，`parse`
做 schema 校验；校验不过由 `chat_json` 带失败原因重试，重试用尽抛 `LLMError` —— 降级不猜。

一处口径说明：`DIRECTION_SYSTEM` 给模型的是**写作指引**（title ≤ 24 汉字、note ≤ 40 字），
`_validate_directions()` 硬校验的是方案 §2.1 的**硬线**（40 / 60 字）。指引收紧、判定放宽：
模型写长一点不该整轮重试，但明显超长要拦。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from src.intelligence.llm import LLMClient, LLMOutputError
from src.models import AssignmentMeta, RubricPoint

__all__ = [
    "MIN_DIRECTIONS",
    "MAX_DIRECTIONS",
    "TITLE_MAX",
    "NOTE_MAX",
    "Direction",
    "DirectionResult",
    "DIRECTION_SYSTEM",
    "generate_directions",
    "M2_BODY_SYSTEM",
    "generate_directions_from_body",
]

MIN_DIRECTIONS = 2
MAX_DIRECTIONS = 3
TITLE_MAX = 40          # 硬线（方案 §2.1）；SYSTEM 里给模型的是更紧的 24 字
NOTE_MAX = 60           # 硬线；SYSTEM 里给模型的是更紧的 40 字

DIRECTION_SYSTEM = """你是小组作业机器人里的「M2 方向候选」模块，唯一任务是根据评分点清单给出 2-3 个候选选题大方向。
只输出 JSON 对象，不要解释、不要 markdown 代码块：
{"directions": [{"id": 1, "title": "（一句话说清这份作业打算做的题目）", "note": "（一句话说明它怎么呼应上面的评分点）", "rubric_refs": ["R1"]}]}

字段要求：
- id：从 1 连续编号。
- title：一句话方向，说清"这份作业打算做什么题目"，不超过 24 个汉字。
- note：一句话说明它怎么呼应评分点，不超过 40 字，给全组看。
- rubric_refs：引用的评分点 id，只能是清单里真实存在的 id；允许空数组。

硬约束：
1. 只出 2-3 条：不许只出 1 条，也不许多于 3 条。
2. 每条方向都要从评分点长出来（至少对上"方案设计 / 系统实现 / 报告文档"里的一类要求）。
3. 方向之间要题目本身不同，不是同一个题目的三种叫法。
4. 你不替人做选题：只提供候选，由全组投票拍板。
5. 不写实现细节、不写技术选型、不写任务分解（那是别的模块的事）。
6. 方向要贴合作业本身的性质：作业要交报告就别硬套"做一个系统"，作业要交系统就别写纯论文选题。"""

# 无评分点模式（口径 A，2026-09-16）：作业书里没有评分标准时，「方向」只能从**正文的
# 交付要求 / 任务描述**长出来 —— 与 M3 的 ``M3_BODY_SYSTEM`` 同一条口径。
M2_BODY_SYSTEM = """你是小组作业机器人里的「M2 方向候选」模块。这份作业书里**没有评分标准**，
所以候选方向只能根据**正文里的交付要求和任务描述**给出 2-3 个选题大方向。
只输出 JSON 对象，不要解释、不要 markdown 代码块：
{"directions": [{"id": 1, "title": "（一句话说清这份作业打算做的题目）", "note": "（一句话说明它呼应了正文里的哪条交付要求）", "rubric_refs": []}]}

字段要求：
- id：从 1 连续编号。
- title：一句话方向，说清"这份作业打算做什么题目"，不超过 24 个汉字。
- note：一句话说明它呼应了正文里的哪条交付要求，不超过 40 字，给全组看。
- rubric_refs：**必须是空数组 []**。这份作业书没有评分点，**不许编造 R1/R2 之类的编号**。

硬约束：
1. 只出 2-3 条：不许只出 1 条，也不许多于 3 条。
2. 每条方向都要落在**正文明确要求的交付物**上（要交的报告 / PPT / 数据集 / 调查…），
   不要凭空发明作业书里没提的产出。
3. 方向之间要题目本身不同，不是同一个题目的三种叫法。
4. 你不替人做选题：只提供候选，由全组投票拍板。
5. 不写实现细节、不写技术选型、不写任务分解（那是别的模块的事）。
6. 方向要贴合作业本身的性质：作业要交报告就别硬套"做一个系统"，作业要交系统就别写纯论文选题。"""


@dataclass(frozen=True)
class Direction:
    """一个候选方向 —— 落 ``state.vote.candidates`` 与 ``data/direction.json``。"""

    id: int
    title: str
    note: str = ""
    rubric_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "note": self.note,
            "rubric_refs": list(self.rubric_refs),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Direction":
        return cls(
            id=int(data.get("id")),
            title=str(data.get("title") or "").strip(),
            note=str(data.get("note") or "").strip(),
            rubric_refs=tuple(str(ref) for ref in (data.get("rubric_refs") or ())),
        )


@dataclass(frozen=True)
class DirectionResult:
    """M2 产出：候选方向 + 校验失败原因（形状照 ``DecomposeResult``）。

    ``failures`` 非空 = 没生成出合法候选。空 rubric 时**不调 LLM**，直接带着原因返回。
    """

    directions: tuple[Direction, ...]
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def generate_directions(
    points: Sequence[RubricPoint],
    meta: AssignmentMeta | None,
    client: LLMClient,
) -> DirectionResult:
    """评分点清单 → 2–3 个候选方向。

    **空 rubric 不调 LLM**，直接给出拒做原因（与 M3 的 D-48 口径一致，不花 token）。
    模型连续输出不合法时 ``chat_json`` 会抛 ``LLMError``，由 app 层兜底回一句人话。
    """
    points = list(points or ())
    if not points:
        return DirectionResult(
            directions=(),
            failures=("没有解析到评分点（作业书里没找到评分标准）→ 不生成候选方向",),
        )
    user = _render_user(points, meta)

    def parse(response) -> tuple[Direction, ...]:
        return _validate_directions(response, points)

    return DirectionResult(directions=client.chat_json(DIRECTION_SYSTEM, user, parse))


def generate_directions_from_body(
    text: str,
    meta: AssignmentMeta | None,
    client: LLMClient,
) -> DirectionResult:
    """**无评分点模式**（口径 A，2026-09-16）：正文 → 2–3 个候选方向。

    只在 ``rubric.json`` 为空数组（即上一轮走的是「按正文拆」）时被调用。
    与 ``generate_directions()`` 的区别：提示词换 ``M2_BODY_SYSTEM``、
    候选的 ``rubric_refs`` **必须是空数组**（没有评分点可引用，不许编造编号）。

    仍是 **B2 的第二处 LLM 点（M2）**，没有新增调用点。
    """
    user = _render_body_user(text, meta)

    def parse(response) -> tuple[Direction, ...]:
        return _validate_directions(response, (), require_empty_refs=True)

    return DirectionResult(directions=client.chat_json(M2_BODY_SYSTEM, user, parse))


def _render_body_user(text: str, meta: AssignmentMeta | None) -> str:
    """user 消息 = 作业元信息 + **正文**（无评分标准时，候选只能从正文长出来）。"""
    parts: list[str] = []
    if meta is not None:
        parts.append(
            "作业元信息（JSON）：\n"
            + json.dumps(
                {"course": meta.course, "title": meta.title, "submission": meta.submission},
                ensure_ascii=False,
            )
        )
    parts.append("作业书正文如下（这份文件里没有评分标准）：\n\n" + text)
    parts.append('请输出 JSON：{"directions": [...]}')
    return "\n\n".join(parts)


def _render_user(points: Sequence[RubricPoint], meta: AssignmentMeta | None) -> str:
    """user 消息 = 作业元信息 + 评分点清单（id / weight / 原文）。"""
    parts: list[str] = []
    if meta is not None:
        parts.append(
            "作业元信息（JSON）：\n"
            + json.dumps(
                {
                    "course": meta.course,
                    "title": meta.title,
                    "submission": meta.submission,
                },
                ensure_ascii=False,
            )
        )
    parts.append(
        "评分点清单（JSON）：\n"
        + json.dumps([point.to_dict() for point in points], ensure_ascii=False)
    )
    parts.append('请输出 JSON：{"directions": [...]}')
    return "\n\n".join(parts)


def _validate_directions(
    payload, points: Sequence[RubricPoint], *, require_empty_refs: bool = False
) -> tuple[Direction, ...]:
    """schema 校验（不是"方向好不好"的判定）：条数 2–3、id 从 1 连续、引用真实存在。

    不合法一律抛 ``LLMOutputError`` —— 它就是"触发重试"的信号（``chat_json`` 的契约）。

    ``require_empty_refs=True``（无评分点模式）：``rubric_refs`` 必须是空数组 ——
    这份作业书没有评分点，任何 ``R1``/``R2`` 都是模型编的。校验出来就喂回重拆，
    而不是"悄悄把编的引用清掉"。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("directions"), list):
        raise LLMOutputError('顶层必须是 {"directions": [...]}')
    raw = payload["directions"]
    if not MIN_DIRECTIONS <= len(raw) <= MAX_DIRECTIONS:
        raise LLMOutputError(
            f"候选条数必须是 {MIN_DIRECTIONS}–{MAX_DIRECTIONS} 条，得到 {len(raw)} 条"
        )

    known = {point.id for point in points}
    problems: list[str] = []
    directions: list[Direction] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"directions[{index}] 不是对象")
            continue
        try:
            direction = Direction.from_dict(item)
        except (TypeError, ValueError) as exc:
            problems.append(f"directions[{index}] 字段不合法：{exc}")
            continue
        if direction.id != index + 1:
            problems.append(
                f"directions[{index}] 的 id 应是从 1 起的连续编号（应为 {index + 1}），"
                f"得到 {direction.id}"
            )
        if not direction.title:
            problems.append(f"directions[{index}] 的 title 不能为空")
        elif len(direction.title) > TITLE_MAX:
            problems.append(
                f"directions[{index}] 的 title 超过 {TITLE_MAX} 字（{len(direction.title)} 字）"
            )
        if len(direction.note) > NOTE_MAX:
            problems.append(
                f"directions[{index}] 的 note 超过 {NOTE_MAX} 字（{len(direction.note)} 字）"
            )
        unknown = sorted(set(direction.rubric_refs) - known)
        if require_empty_refs:
            if direction.rubric_refs:
                problems.append(
                    f"directions[{index}] 的 rubric_refs 必须是空数组"
                    f"（这份作业书没有评分点，得到 {list(direction.rubric_refs)}）"
                )
        elif unknown:
            problems.append(f"directions[{index}] 引用了不存在的评分点：{unknown}")
        directions.append(direction)

    if problems:
        raise LLMOutputError("；".join(problems))
    return tuple(directions)
