"""落盘数据模型 —— 全项目唯一的接口契约。

为什么契约写在代码里、不写在文档里：文档是散文，三个人（或者几天后的你自己）
读同一段散文会抽出不同的接口。数据结构只有一份、而且可执行，才不会漂移。

依据：requirements.md §6（数据模型）、§9（决策台账）、docs/ARCHITECTURE.md §4。

这里用类型硬性 enforce 的几条已拍板规矩：
  *  rubric_refs 落盘只存 id 数组，不存原文          —— D-03
  *  截止时间是作业级一根线，任务卡没有 due_date      —— D-23
  *  assignment.source 只有四个取值                  —— D-20
  *  花名册：组长有且只有一个，组员至少 2 人          —— §6.6

规矩：任何模块都不许直接读写裸 dict，一律经过本文件的类型。
"""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Optional

__all__ = [
    "SchemaError",
    "AssignmentMeta",
    "RubricPoint",
    "TaskCard",
    "Preference",
    "AssignmentRecord",
    "Member",
    "Roster",
    "RUBRIC_STATUS",
    "ASSIGNMENT_SOURCE",
    "parse_deadline",
]

RUBRIC_STATUS = ("normal", "ambiguous")
ASSIGNMENT_SOURCE = ("volunteer_1", "volunteer_2", "auto", "leader")

EFFORT_HOURS_FLOOR = 0.5          # 工时地板，避免除零（§7.3）
BALANCE_LIMIT = 3.0               # 循环内均衡判据 max/min <= 3（§7.4）


class SchemaError(ValueError):
    """落盘数据字段缺失或取值非法。"""


def _build(cls: type, data: dict) -> Any:
    """按 dataclass 字段构造；缺必填字段直接报错，不猜。"""
    missing = [
        f.name
        for f in fields(cls)
        if f.name not in data and f.default is MISSING and f.default_factory is MISSING
    ]
    if missing:
        raise SchemaError(f"{cls.__name__}: 缺少必填字段 {missing}")
    return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})


@dataclass
class _Base:
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Any:
        return _build(cls, data)

    def validate(self) -> None:
        """子类覆写。只检查文档里写死的硬规则，不做业务判断。"""


@dataclass
class AssignmentMeta(_Base):
    """作业元信息 —— M1 产出，落 data/assignment.json（§6.0）。

    deadline 是作业级唯一截止线（D-23）：任务卡不设 due_date。
    deadline **可为空字符串**（D-49）：作业书没写截止时间就不要编 —— 编出来的
    占位值会污染 M6 催办 / M7 甘特图 / M8 基线，报告里按「未标注」显示。
    注意：deadline 的字符串格式文档没有定义，这里暂按 ISO 风格
    'YYYY-MM-DDTHH:MM' 存 —— 这属于待拍板项，别当成已定论。
    """

    course: str
    title: str
    submission: str
    deadline: str
    source_file: str

    def validate(self) -> None:
        # deadline 不在必填里：允许空（D-49），空值交给 check_deadline() 出软警告
        for name in ("course", "title", "submission", "source_file"):
            if not getattr(self, name):
                raise SchemaError(f"AssignmentMeta.{name} 不能为空")


def parse_deadline(value) -> datetime | None:
    """``deadline`` → ``datetime``；空 / 脏 / "未标注" → ``None``（D-49）。

    ``value`` 可以是 ``AssignmentMeta``，也可以是原始字符串。截止时间的字符串格式
    文档没有定义（见上面 AssignmentMeta 的说明），所以这里**只认 ISO 风格**，
    认不出来就当没有 —— M6 宁可漏催、不可乱催，M7 甘特图则不画截止线。
    """
    if hasattr(value, "deadline"):
        value = getattr(value, "deadline", "")
    text = str(value or "").strip()
    if not text or text == "未标注":
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class RubricPoint(_Base):
    """评分点 —— M1 产出，落 data/rubric.json（§6.1）。

    quote 必须是作业书原文，作用就是防 LLM 编造评分点（D-03）。
    weight 是可选字段：作业书未必标占比，缺失的点不参与均衡计算。
    """

    id: str
    quote: str
    observable: str
    status: str = "normal"
    weight: Optional[float] = None

    def validate(self) -> None:
        if not self.id:
            raise SchemaError("RubricPoint.id 不能为空")
        if not self.quote.strip():
            raise SchemaError(f"RubricPoint({self.id}): quote 必须是作业书原文，不能为空")
        if self.status not in RUBRIC_STATUS:
            raise SchemaError(
                f"RubricPoint({self.id}): status 必须是 {RUBRIC_STATUS}，得到 {self.status!r}"
            )
        if self.weight is not None and self.weight < 0:
            raise SchemaError(f"RubricPoint({self.id}): weight 不能为负")


@dataclass
class TaskCard(_Base):
    """任务卡 —— M3 产出，落 data/cards.json（§6.2，共 7 字段）。

    rubric_refs 的形态是这套文档里唯一一处真矛盾：
      *  定稿 §3.4 写的是"对应评分点编号 + 原文引用"
      *  requirements §6.2 / D-03 写的是"落盘只存 id 数组，展示时才 join 出原文"
    这里按 D-03 enforce：因为 §7.3 的 check() 是 id 集合运算（eligible <= covered），
    数组里塞不得原文。validate() 会把"塞了原文"的写法直接拒掉。
    """

    task_id: str
    module_name: str
    rubric_refs: list[str]
    effort_hours: float
    deliverable: str
    acceptance: str
    depends_on: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.task_id:
            raise SchemaError("TaskCard.task_id 不能为空")
        if not self.rubric_refs:
            raise SchemaError(
                f"TaskCard({self.task_id}): rubric_refs 是必填溯源字段，不能为空"
            )
        for ref in self.rubric_refs:
            if not isinstance(ref, str):
                raise SchemaError(
                    f"TaskCard({self.task_id}): rubric_refs 只存 id 字符串（D-03），"
                    f"得到 {type(ref).__name__}；原文引用只在展示时 join"
                )
        if self.effort_hours < EFFORT_HOURS_FLOOR:
            raise SchemaError(
                f"TaskCard({self.task_id}): effort_hours 不得低于地板 {EFFORT_HOURS_FLOOR}"
            )
        if self.task_id in self.depends_on:
            raise SchemaError(f"TaskCard({self.task_id}): depends_on 不能依赖自己")


@dataclass
class Preference(_Base):
    """志愿 —— M4 收，落 data/preferences.json（§6.3）。"""

    user_id: str
    ranked_task_ids: list[str]
    submitted_at: str

    def validate(self) -> None:
        if not self.user_id:
            raise SchemaError("Preference.user_id 不能为空")
        if not self.ranked_task_ids:
            raise SchemaError(f"Preference({self.user_id}): 志愿不能为空")


@dataclass
class AssignmentRecord(_Base):
    """分配 + 完成标记 —— 落 data/assignments.json（§6.4）。"""

    task_id: str
    assignee: str
    source: str
    completed_at: Optional[str] = None      # None = 未完成（D-31）

    def validate(self) -> None:
        if self.source not in ASSIGNMENT_SOURCE:
            raise SchemaError(
                f"AssignmentRecord({self.task_id}): source 必须是 {ASSIGNMENT_SOURCE}，"
                f"得到 {self.source!r}"
            )


@dataclass
class Member(_Base):
    open_id: str
    name: str


@dataclass
class Roster(_Base):
    """花名册 —— M0「登记」产出，落 data/members.json（§6.6）。

    硬规则：组长有且只有一个，组员至少 2 人。
    open_id 从 @ 结构里取，所以不需要"读群成员名单"权限（D-34）。
    """

    leader: str
    members: list[Member]
    registered_at: str
    confirmed_by: str

    @classmethod
    def from_dict(cls, data: dict) -> "Roster":
        raw = _build(cls, {**data, "members": data.get("members", [])})
        raw.members = [
            m if isinstance(m, Member) else Member.from_dict(m) for m in raw.members
        ]
        return raw

    def validate(self) -> None:
        if not self.leader:
            raise SchemaError("Roster：组长有且只有一个")
        if len(self.members) < 2:
            raise SchemaError(f"Roster：组员至少 2 人，得到 {len(self.members)}")
        ids = [m.open_id for m in self.members]
        if len(ids) != len(set(ids)):
            raise SchemaError("Roster：members 里有重复的 open_id")
        if self.leader not in ids:
            raise SchemaError("Roster：leader 必须出现在 members 里")


# ---------------------------------------------------------------------------
# 以下两处 requirements.md 没有给字段级定义。按 §8 的规矩"没写到的地方禁止臆想"，
# 这里刻意不落类型，等你们拍板后再补：
#
#   data/proposals.json —— §6.5 只说了"必须留痕真实身份 user_id + 内容原样转达"
#   data/state.json     —— ARCHITECTURE §4 只说了含"当前阶段、待确认项、投票候选与票数"
#
# 下面是我建议的形态（**是建议，不是我已定的**）：
#   Proposal: proposal_id, user_id, content, created_at
#   State:    awaiting(None|vote|preference|register), vote{candidates, votes}, updated_at
# ---------------------------------------------------------------------------
