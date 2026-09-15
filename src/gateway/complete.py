"""M6 完成标记（私聊「完成 T3」）—— 纯函数、零 I/O、零 LLM（B2 / S10–S11）。

依据：`requirements.md` §3(S11) / §4(M6 = P1) / §6.4（`AssignmentRecord.completed_at`）/
§7.1（第 6 条「完成 T3」）、D-22 / D-31，M6+M7 方案 §2.1。

三条纪律：
  * **只认私聊** —— 群里发只回一句"去私聊"：群里标完成，没人知道标的是谁的卡；
  * **不给假确认** —— 不是你的卡 / 没有这张卡 → 说清楚，并**列出他自己领到的卡**；
  * **幂等** —— 已经标过就回原时间戳、**不覆盖**第一次的时间：那是真实工作量的证据。

落盘走 ``Outcome.save_complete``（纯数据），真正的 ``mutate_many()`` 在 app 层 ——
与 ``save_roster`` / ``save_preference`` 同一个形状，本模块自己不做 I/O。
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from src.gateway import replies
from src.gateway.events import Inbound, Outcome, reply
from src.models import AssignmentRecord, TaskCard

__all__ = ["accept", "list_mine"]


def list_mine(
    inbound: Inbound,
    assignments: Sequence[AssignmentRecord] = (),
    cards: Sequence[TaskCard] = (),
) -> Outcome:
    """无参「完成」→ **列出他自己名下的卡 + 该怎么标**（D-70）。

    为什么要有这条：菜单项只能发一句固定文本，而任务卡是**动态**的（换个作业书就是全新编号）
    —— 每张卡配一个菜单项既放不下、又要每次回后台改菜单 + 发版。所以菜单只留一格，
    具体的编号由机器人当场报出来。**不引入新状态机**：只回一句提示，用户照抄即可。
    """
    if inbound.chat_type != "p2p":
        return Outcome(replies=(reply(inbound, replies.COMPLETE_NEED_DM),))

    mine = [r for r in (assignments or ()) if r.assignee == inbound.sender_open_id]
    if not mine:
        return Outcome(replies=(reply(inbound, replies.COMPLETE_NEED_ASSIGNMENTS),))

    done = [r.task_id for r in mine if r.completed_at]
    pending = [r.task_id for r in mine if not r.completed_at]
    first = (pending or [r.task_id for r in mine])[0]
    return Outcome(
        replies=(
            reply(
                inbound,
                replies.COMPLETE_HOWTO.format(
                    tasks=_render_tasks(mine, cards),
                    first=first,
                    done=("已完成：" + "、".join(done) + "。" if done else ""),
                ),
            ),
        )
    )


def accept(
    index: str,
    inbound: Inbound,
    assignments: Sequence[AssignmentRecord] = (),
    cards: Sequence[TaskCard] = (),
    now: datetime | None = None,
) -> Outcome:
    """``index`` 是编号数字 —— router 已经从「完成 T3」里捕获出来（判定只有一处）。"""
    if inbound.chat_type != "p2p":
        # 只认私聊：群里标完成，既不知道替谁标、也让"谁做完了什么"没法认（§2.1）
        return Outcome(replies=(reply(inbound, replies.COMPLETE_NEED_DM),))

    records = list(assignments or ())
    if not records:
        return Outcome(replies=(reply(inbound, replies.COMPLETE_NEED_ASSIGNMENTS),))

    task_id = f"T{index}"
    mine = _render_mine(
        [r for r in records if r.assignee == inbound.sender_open_id], cards
    )
    target = next((r for r in records if r.task_id == task_id), None)
    if target is None:
        return Outcome(
            replies=(reply(inbound, replies.COMPLETE_UNKNOWN.format(index=index, mine=mine)),)
        )
    if target.assignee != inbound.sender_open_id:
        # 不给假确认：别人的卡不能标（同 §6.3 的口径）
        return Outcome(
            replies=(reply(inbound, replies.COMPLETE_NOT_YOURS.format(index=index, mine=mine)),)
        )
    if target.completed_at:
        # 幂等：保留第一次的时间戳，别覆盖 —— 那是真实工作量的证据
        return Outcome(
            replies=(
                reply(
                    inbound,
                    replies.COMPLETE_ALREADY.format(task_id=task_id, at=target.completed_at),
                ),
            )
        )
    return Outcome(
        replies=(
            reply(
                inbound,
                replies.COMPLETE_OK.format(task_id=task_id, module=_module_name(task_id, cards)),
            ),
        ),
        save_complete={
            "task_id": task_id,
            "completed_at": (now or datetime.now()).isoformat(timespec="seconds"),
        },
    )


def _module_name(task_id: str, cards: Sequence[TaskCard]) -> str:
    for card in cards or ():
        if card.task_id == task_id:
            return card.module_name or task_id
    return task_id


def _render_tasks(mine: Sequence[AssignmentRecord], cards: Sequence[TaskCard]) -> str:
    """``T3（模块）、T5（模块）`` —— 只给卡本身，不带前后缀（D-70 的列表要复用）。

    已完成的卡在后面加一个勾，和 M7 的核对清单同一套记号。
    """
    return "、".join(
        f"{r.task_id}（{_module_name(r.task_id, cards)}）{'✓' if r.completed_at else ''}"
        for r in mine
    )


def _render_mine(mine: Sequence[AssignmentRecord], cards: Sequence[TaskCard]) -> str:
    """``你手上的是：…`` —— 说"不是你的"时必须给出他真正能标的那些卡。"""
    if not mine:
        return replies.COMPLETE_MINE_NONE
    return replies.COMPLETE_MINE.format(tasks=_render_tasks(mine, cards))

