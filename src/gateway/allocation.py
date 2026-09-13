"""M4 分配算法 + 总表渲染 —— **纯函数、零 I/O、零 LLM**（B2 / S6–S8）。

依据：`requirements.md` §6.4 / §7.1、D-14 / D-20、M4+M5 方案 §2.4 / §2.5。

两条规则（用户 2026-09-13 认可，见 D-53）：
  1. **先到先得**：按 ``submitted_at`` 升序逐个人处理，取他志愿里第一个还没被占的卡
     —— 命中第 1 个记 ``volunteer_1``、第 2 个记 ``volunteer_2``；
  2. **兜底**：没人认领的卡按 ``effort_hours`` 升序（并列按 ``task_id``）排，
     依次分给"当前手上卡数最少的人"（并列按花名册顺序）—— 没填志愿的人因此拿到 ``auto``。

``source`` 的合法取值只有 ``volunteer_1`` / ``volunteer_2`` / ``auto`` / ``leader``（§6.4），
所以志愿里第 3 个及以后的命中一律记 ``auto`` —— 卡仍是他自己选的，只是标签退化成兜底。
``depends_on`` **不参与分配**（只影响甘特图，§2.4 ③）；一张卡只能有一个 ``assignee``。
"""

from __future__ import annotations

from typing import Sequence

from src.gateway import replies
from src.models import AssignmentRecord, Preference, Roster, TaskCard

__all__ = ["allocate", "render_task_list", "render_board"]

# source -> 总表上的说法（§2.5）。leader 是 P1 的改派，现在不会被产出，但别 KeyError。
_SOURCE_LABEL = {
    "volunteer_1": "第一志愿",
    "volunteer_2": "第二志愿",
    "auto": "兜底",
    "leader": "组长指定",
}
# 一个人可能既中了志愿、又拿了兜底卡：统计时按**他最好的一档**算，人头才不重复计。
_SOURCE_ORDER = {"leader": 0, "volunteer_1": 1, "volunteer_2": 2, "auto": 3}


def allocate(
    cards: Sequence[TaskCard],
    roster: Roster | None,
    preferences: Sequence[Preference],
) -> list[AssignmentRecord]:
    """``任务卡 × 花名册 × 志愿`` → 分配结果（按任务卡顺序返回）。**纯函数。**"""
    cards = list(cards or ())
    members = list(getattr(roster, "members", None) or ())
    member_ids = [m.open_id for m in members]
    known = set(member_ids)
    by_id = {card.task_id: card for card in cards}

    # 花名册外的人不进分配：旧花名册的残留志愿、陌生人私聊都会在这里被滤掉（D-53）
    ranked = [p for p in (preferences or ()) if p.user_id in known]
    ranked.sort(key=lambda p: p.submitted_at or "")      # 先到先得（同刻按文件顺序，稳定排序）

    chosen: dict[str, AssignmentRecord] = {}
    for preference in ranked:
        picked = _first_free(preference, chosen, by_id)
        if picked is None:
            continue                                    # 志愿全被占：挂起，等兜底
        task_id, rank = picked
        chosen[task_id] = AssignmentRecord(
            task_id=task_id,
            assignee=preference.user_id,
            source="volunteer_1" if rank == 1 else ("volunteer_2" if rank == 2 else "auto"),
            completed_at=None,
        )

    load = {uid: 0 for uid in member_ids}
    for record in chosen.values():
        load[record.assignee] = load.get(record.assignee, 0) + 1

    # 兜底：剩下的卡按工时升序，补给手上最少的人。卡数 < 人数时后面的人自然拿到「无任务」，
    # 不硬塞（§2.4 ⑤）。
    for task in sorted(
        (card for card in cards if card.task_id not in chosen),
        key=lambda card: (card.effort_hours, card.task_id),
    ):
        assignee = _least_loaded(member_ids, load)
        if assignee is None:
            break                                        # 花名册是空的：没人可分
        chosen[task.task_id] = AssignmentRecord(
            task_id=task.task_id, assignee=assignee, source="auto", completed_at=None
        )
        load[assignee] += 1

    return [chosen[card.task_id] for card in cards if card.task_id in chosen]


def render_task_list(cards: Sequence[TaskCard]) -> str:
    """发群 / 发私聊的**任务卡清单**：序号就是组员要回复的数字（§2.1 / §2.2）。"""
    items = "\n".join(
        f"{index}. {card.task_id} {card.module_name}（{card.effort_hours:g}h）"
        for index, card in enumerate(cards, start=1)
    )
    return replies.PREFERENCE_LIST.format(items=items)


def render_board(
    assignments: Sequence[AssignmentRecord],
    cards: Sequence[TaskCard],
    roster: Roster | None,
) -> str:
    """**分配总表**（§2.5）：按人分组 + 末行统计。末尾那行是"志愿制的机制证明"。"""
    members = list(getattr(roster, "members", None) or ())
    by_person: dict[str, list[AssignmentRecord]] = {m.open_id: [] for m in members}
    for record in assignments or ():
        by_person.setdefault(record.assignee, []).append(record)

    lines = ["分配总表"]
    best: list[str] = []
    for member in members:
        mine = by_person.get(member.open_id) or []
        label = member.name or member.open_id[:8] or member.open_id
        if not mine:
            lines.append(f"{label} → 无任务")
            continue
        lines.append(
            f"{label} → " + "/ ".join(f"{r.task_id}（{_SOURCE_LABEL[r.source]}）" for r in mine)
        )
        best.append(min(mine, key=lambda r: _SOURCE_ORDER.get(r.source, 9)).source)

    counts = {source: best.count(source) for source in ("volunteer_1", "volunteer_2", "auto")}
    lines.append(
        f"第一志愿 {counts['volunteer_1']} 人 / 第二志愿 {counts['volunteer_2']} 人 / "
        f"兜底 {counts['auto']} 人"
    )
    return "\n".join(lines)


# ---------- 小工具 ----------


def _first_free(
    preference: Preference, taken: dict, by_id: dict[str, TaskCard]
) -> tuple[str, int] | None:
    """他志愿里第一个"还没被占、且确实是张卡"的任务；返回 (task_id, 它在志愿里的位次)。"""
    for index, task_id in enumerate(preference.ranked_task_ids or (), start=1):
        if task_id in taken or task_id not in by_id:
            continue
        return task_id, index
    return None


def _least_loaded(member_ids: Sequence[str], load: dict[str, int]) -> str | None:
    """手上卡数最少的人；并列取花名册里靠前的那个。"""
    if not member_ids:
        return None
    return min(member_ids, key=lambda uid: (load.get(uid, 0), member_ids.index(uid)))
