"""JSON 落盘层 —— 全项目唯一有副作用的地方。

依据：requirements.md §1(B4) / §6、docs/ARCHITECTURE.md §4、D-30
（唯一落盘位置 = 仓库根 data/）。

两条工程约定，是线程模型的直接后果：
  1. 写盘一律**原子写**（临时文件 + os.replace），绝不留下半截 JSON。
  2. 读-改-写一律走 `mutate()`，由一把**进程级** RLock 串行化。
     锁必须是进程级的、不能每个实例一把 —— 否则 M0 回调线程、worker 线程、
     M6 定时线程各自 new 一个 store，就等于根本没上锁。

`state.json` / `proposals.json` 只提供裸读写入口：它们的字段 requirements.md
没有定义，按 §8 的规矩不臆想，等拍板后再补类型（见 src/models.py 末尾）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from src.models import (
    AssignmentMeta,
    AssignmentRecord,
    Preference,
    Roster,
    RubricPoint,
    TaskCard,
)

__all__ = [
    "JsonStore",
    "ASSIGNMENT",
    "RUBRIC",
    "CARDS",
    "PREFERENCES",
    "ASSIGNMENTS",
    "PROPOSALS",
    "DIRECTION",
    "MEMBERS",
    "STATE",
    "SEEN",
    "UPLOADS",
    "REMINDERS",
    "REPORT",
    "GANTT",
]

# 文件名对照 docs/ARCHITECTURE.md §4
ASSIGNMENT = "assignment.json"
RUBRIC = "rubric.json"
CARDS = "cards.json"
PREFERENCES = "preferences.json"
ASSIGNMENTS = "assignments.json"
PROPOSALS = "proposals.json"
# M2 方向落定结果（§2.6）。字段级定义 requirements.md 没有，所以走裸 JSON，同 proposals.json。
DIRECTION = "direction.json"
MEMBERS = "members.json"
STATE = "state.json"
SEEN = "seen.json"        # P0-A 事件去重（最近 200 条 message_id）
# M6 催办去重（§2.2 / D-66）：同 (task_id, tier) 只发一次。字段集见 D-66。
REMINDERS = "reminders.json"
# M7 执行报告的两份产物（§3.4）：文本存档 + 甘特图 PNG。都是**产物**、不是数据模型。
REPORT = "report.md"
GANTT = "gantt.png"
UPLOADS = "uploads"

# 进程级锁：单进程（B1）⇒ 全局唯一落盘 ⇒ 一把锁就够
_GLOBAL_LOCK = threading.RLock()


class JsonStore:
    """`data/` 的唯一入口。任何模块都不许绕过它直接开文件。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.uploads = self.root / UPLOADS
        self._lock = _GLOBAL_LOCK          # 刻意共享，见模块 docstring

    # ---------- 底层：裸读写 ----------

    def path(self, name: str) -> Path:
        return self.root / name

    def _read_unlocked(self, name: str, default: Any = None) -> Any:
        p = self.path(name)
        if not p.exists():
            return default
        text = p.read_text(encoding="utf-8")
        if not text.strip():
            return default
        # 坏 JSON 直接抛，不猜、不兜底 —— 与 LLM 输出"降级不猜"同一原则
        return json.loads(text)

    def _write_unlocked(self, name: str, payload: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.path(name)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, p)                 # 原子替换

    def read_raw(self, name: str, default: Any = None) -> Any:
        with self._lock:
            return self._read_unlocked(name, default)

    def write_raw(self, name: str, payload: Any) -> None:
        with self._lock:
            self._write_unlocked(name, payload)

    def mutate_raw(self, name: str, fn: Callable[[Any], Any], default: Any = None) -> Any:
        """裸 JSON 的原子读-改-写。用于 state.json / proposals.json（类型未定）。

        `fn(payload)` 返回新 payload；抛异常则**不写盘**。
        """
        with self._lock:
            payload = self._read_unlocked(name, default)
            new_payload = fn(payload)
            self._write_unlocked(name, new_payload)
            return new_payload

    def mutate_many(self, name: str, model: type, fn: Callable[[list], list]) -> list:
        """**类型化列表**的原子读-改-写，落盘前逐个 validate。

        这是 M4 收志愿、M6 写 completed_at 要用的原语：同一份文件被两个线程
        同时改时，必须走这里，否则就是丢更新。
        """
        with self._lock:
            raw = self._read_unlocked(name, []) or []
            items = [model.from_dict(item) for item in raw]
            new_items = fn(items)
            for item in new_items:
                item.validate()
            self._write_unlocked(name, [item.to_dict() for item in new_items])
            return new_items

    def ensure_dirs(self) -> None:
        """建好 data/ 与 data/uploads/。进程启动时调一次。"""
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.uploads.mkdir(parents=True, exist_ok=True)

    # ---------- 泛型：单对象 / 列表 ----------

    def _load_one(self, name: str, model: type) -> Any:
        raw = self.read_raw(name)
        return None if raw is None else model.from_dict(raw)

    def _save_one(self, name: str, obj: Any) -> None:
        obj.validate()
        self.write_raw(name, obj.to_dict())

    def _load_many(self, name: str, model: type) -> list:
        raw = self.read_raw(name, []) or []
        return [model.from_dict(item) for item in raw]

    def _save_many(self, name: str, items: list) -> None:
        for item in items:
            item.validate()
        self.write_raw(name, [item.to_dict() for item in items])

    # ---------- 作业元信息（M1 写，M6 / M7 读）----------

    def load_assignment(self) -> AssignmentMeta | None:
        return self._load_one(ASSIGNMENT, AssignmentMeta)

    def save_assignment(self, meta: AssignmentMeta) -> None:
        self._save_one(ASSIGNMENT, meta)

    # ---------- 评分点（M1 写，M3 / M7 / M8 读）----------

    def load_rubric(self) -> list[RubricPoint]:
        return self._load_many(RUBRIC, RubricPoint)

    def save_rubric(self, points: list[RubricPoint]) -> None:
        self._save_many(RUBRIC, points)

    # ---------- 任务卡（M3 写，M4 / M7 / M8 读）----------

    def load_cards(self) -> list[TaskCard]:
        return self._load_many(CARDS, TaskCard)

    def save_cards(self, cards: list[TaskCard]) -> None:
        self._save_many(CARDS, cards)

    # ---------- 志愿（M4 写、M4 读）----------

    def load_preferences(self) -> list[Preference]:
        return self._load_many(PREFERENCES, Preference)

    def save_preferences(self, preferences: list[Preference]) -> None:
        self._save_many(PREFERENCES, preferences)

    # ---------- 分配 + 完成标记（M4 写分配 / M6 写完成，M6 / M7 读）----------

    def load_assignments(self) -> list[AssignmentRecord]:
        return self._load_many(ASSIGNMENTS, AssignmentRecord)

    def save_assignments(self, assignments: list[AssignmentRecord]) -> None:
        self._save_many(ASSIGNMENTS, assignments)

    # ---------- 花名册（M0 登记写，M2 / M4 / M5 / M6 / M7 读）----------

    def load_members(self) -> Roster | None:
        return self._load_one(MEMBERS, Roster)

    def save_members(self, roster: Roster) -> None:
        self._save_one(MEMBERS, roster)

    # ---------- 提议 / 会话状态：类型未定，只给裸读写 ----------
    # requirements.md §6.5 与 ARCHITECTURE §4 都没有字段级定义，
    # 按 §8"没写到的地方禁止臆想"暂时按裸 JSON 处理。

    def load_proposals(self) -> list[dict]:
        return self.read_raw(PROPOSALS, []) or []

    def save_proposals(self, proposals: list[dict]) -> None:
        self.write_raw(PROPOSALS, proposals)

    # ---------- 方向落定（M2 写，M7 展示用；整份覆盖）----------

    def load_direction(self) -> dict:
        return self.read_raw(DIRECTION, {}) or {}

    def save_direction(self, payload: dict) -> None:
        self.write_raw(DIRECTION, payload)

    def load_state(self) -> dict:
        return self.read_raw(STATE, {}) or {}

    def save_state(self, state: dict) -> None:
        self.write_raw(STATE, state)
