"""M6 临期催办扫描 —— 纯函数、零 I/O、零 LLM（B2 / S11）。

依据：`requirements.md` §3(S11) / §4(M6 = P1) / §6.0（作业级 `deadline`）、
§8 待定义-35（48h / 24h 两档）、D-23 / D-49 / D-57 / D-66，M6+M7 方案 §2.2。

锚点只有一根：`data/assignment.json` 的**作业级** `deadline`（D-23：任务卡没有 `due_date`）。
`deadline` 为空 / 脏 → **整轮跳过**（D-49 允许空）：宁可漏催，不可乱催。

三档（前两档 = 待定义-35 已定；逾期档 = D-66 的工程默认，可推翻）：
  > 48h 不催 / ≤ 48h 且 > 24h → 「48 小时」/ ≤ 24h → 「24 小时」/ 已过期 → 「已逾期」。
逾期**也要催**：逾期不是"还没到点"，是"更该催"。

**同 `(task_id, tier)` 只催一次**（查 `data/reminders.json`），换档才再催一次 ——
没有这条，每小时扫一次就会每小时 @ 一次，直接刷屏。

@人用飞书文本的 ``<at user_id="ou_xxx"></at>`` 语法：写成纯文本 ``@某人`` 不会真 @。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from src.gateway import replies
from src.models import AssignmentMeta, AssignmentRecord, TaskCard, parse_deadline

__all__ = [
    "Reminder",
    "TIER_T1",
    "TIER_T2",
    "TIER_OVERDUE",
    "TIER1_HOURS",
    "TIER2_HOURS",
    "INTERVAL_SECONDS",
    "scan",
    "settings",
]

# 两档阈值 = 待定义-35 已定（48h / 24h）；扫描间隔与逾期档 = D-66 的工程默认，可推翻。
TIER1_HOURS = 48
TIER2_HOURS = 24
INTERVAL_SECONDS = 3600

TIER_T1 = "48 小时"
TIER_T2 = "24 小时"
TIER_OVERDUE = "已逾期"

ENV_INTERVAL = "REMIND_INTERVAL_SECONDS"
ENV_TIER1 = "REMIND_TIER1_HOURS"
ENV_TIER2 = "REMIND_TIER2_HOURS"


@dataclass(frozen=True)
class Reminder:
    """一条要发出去的催办 —— 纯数据。``text`` 里已经带好飞书 @ 语法。"""

    task_id: str
    tier: str
    open_id: str
    text: str

    def to_record(self, chat_id: str, sent_at: str, ok: bool) -> dict:
        """落 ``data/reminders.json`` 的那条记录（字段集见 D-66）。失败也记（``ok: false``）。"""
        return {
            "task_id": self.task_id,
            "tier": self.tier,
            "chat_id": chat_id,
            "sent_at": sent_at,
            "ok": bool(ok),
        }


def scan(
    cards: Sequence[TaskCard],
    assignments: Sequence[AssignmentRecord],
    meta: AssignmentMeta | None,
    sent: Sequence[dict] = (),
    now: datetime | None = None,
    *,
    tier1_hours: int = TIER1_HOURS,
    tier2_hours: int = TIER2_HOURS,
) -> list[Reminder]:
    """这一轮该催哪些卡（已按 ``sent`` 去过重）。截止时间认不出来 → 空列表。"""
    deadline = parse_deadline(meta)
    if deadline is None:
        return []                       # 截止未标注 / 脏 → 整轮跳过（D-49）
    moment = now or datetime.now()
    records = {r.task_id: r for r in (assignments or ())}
    already = {
        (item.get("task_id"), item.get("tier"))
        for item in (sent or ())
        if isinstance(item, dict)
    }
    due: list[Reminder] = []
    for card in cards or ():
        record = records.get(card.task_id)
        if record is None or not record.assignee:
            continue                    # 没分配 → 没有可 @ 的人
        if record.completed_at:
            continue                    # 已完成 → 别再催（S11）
        tier, hours = _tier(deadline - moment, tier1_hours, tier2_hours)
        if tier is None:
            continue                    # 还早
        if (card.task_id, tier) in already:
            continue                    # 同 (task_id, tier) 只催一次（D-66）
        due.append(
            Reminder(
                task_id=card.task_id,
                tier=tier,
                open_id=record.assignee,
                text=_render(card, tier, hours, deadline, record.assignee),
            )
        )
    return due


def settings() -> tuple[int, int, int]:
    """``(扫描间隔秒, 48h 档, 24h 档)`` —— 三个都能用环境变量覆盖（D-66）。

    演示 / 验收要能控制时间：默认 1 小时扫一次、48h / 24h 两档。
    """
    return (
        _env_int(ENV_INTERVAL, INTERVAL_SECONDS),
        _env_int(ENV_TIER1, TIER1_HOURS),
        _env_int(ENV_TIER2, TIER2_HOURS),
    )


def _tier(remaining, tier1_hours: int, tier2_hours: int) -> tuple[str | None, int]:
    """``(档位, 小时数)``；还早则 ``(None, 0)``。逾期也催（D-66）。"""
    hours = remaining.total_seconds() / 3600
    if hours <= 0:
        return TIER_OVERDUE, max(1, math.ceil(-hours))
    if hours <= tier2_hours:
        return TIER_T2, max(1, math.ceil(hours))
    if hours <= tier1_hours:
        return TIER_T1, max(1, math.ceil(hours))
    return None, 0


def _render(card: TaskCard, tier: str, hours: int, deadline: datetime, open_id: str) -> str:
    template = replies.REMIND_OVERDUE if tier == TIER_OVERDUE else replies.REMIND_DUE
    return template.format(
        at=f'<at user_id="{open_id}"></at>',
        module=card.module_name or card.task_id,
        hours=hours,
        deadline=deadline.strftime("%Y-%m-%d %H:%M"),
        task_id=card.task_id,
    )


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default
