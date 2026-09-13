"""M4 志愿分配状态机（§7.1 / D-52~D-54）—— 纯函数，形状照 ``register.py``。

流程（§2.1 ~ §2.3）：

  群里发「你想做哪一块」→ 清单发群 + 给花名册每人私聊一份（``awaiting=preference``）
  → 组员**私聊**回裸数字 → 落 ``data/preferences.json``（后投覆盖先投）
  → 结算（**三条任一即结算，只结算一次**）→ 分配 → 总表发群。

三条口径（用户 2026-09-13 拍板 / 认可）：

  * **5 小时是上限，不是必须等满**：花名册全员都交了就立刻封盘（三人 5 分钟填完
    还得干等 5 小时，演示和实用都没法接受）；
  * **过期即失效**：读到 ``opened_at`` 距今 > 5 小时的窗口就不再收志愿 —— 不清掉的话
    昨天的窗口会吃掉今天的数字。**读到的过期窗口当场结算**（就是"三选一"里的第 2 条），
    所以"失效"与"封盘"是同一个动作，不是静默丢弃；
  * **只认私聊**：窗口长达 5 小时，而 ``awaiting`` 是全局的 —— 群里谁打一个裸数字
    都会被记成志愿序号。所以只有 ``chat_type == "p2p"`` 的裸数字算志愿，
    群里一切消息照走 7 条前缀。

``M2 的投票窗口仍是 10 分钟``，不要跟着改成 5 小时（用户只让大家动 M4 这一处）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Sequence

from src.gateway import allocation, replies
from src.gateway.events import Inbound, Outcome, Reply, reply
from src.models import Preference, Roster, TaskCard

__all__ = [
    "PREFERENCE_TTL",
    "read_window",
    "command",
    "open_window",
    "accept",
    "settle",
    "clear",
]

PREFERENCE_TTL = timedelta(hours=5)

# 含中文的消息一定是"某条指令"（7 条前缀里没有一条是纯 ASCII），不可能是志愿序号；
# 不含中文又不是纯数字的（"abc" / "T2" / "3abc"）才是"序号没看懂"，要回提示。
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_SEPARATORS = re.compile(r"[\s,，、]+")


def read_window(state: dict, now: datetime | None = None) -> tuple[dict, bool]:
    """``(窗口块, 是否已过期)``。没有窗口 → ``({}, False)``。

    过期时**照样把块返回**：调用方要用它来结算，而不是装作无事发生（D-52）。
    """
    block = dict((state or {}).get("preference") or {})
    if not block:
        return {}, False
    return block, _expired(block, now)


def clear(state: dict) -> dict:
    """关掉窗口（``awaiting`` 与 ``preference`` 一起清）—— 裸数字立刻不再被当志愿序号。"""
    return {**(state or {}), "awaiting": None, "preference": None}


def command(
    inbound: Inbound,
    state: dict,
    cards: Sequence[TaskCard],
    roster: Roster | None,
    preferences: Sequence[Preference],
    now: datetime | None = None,
) -> Outcome:
    """「你想做哪一块」：群里=开窗口（组长重发=封盘），私聊=只回他自己那份清单（§2.1）。"""
    cards = list(cards or ())
    if not cards:
        return Outcome(replies=(reply(inbound, replies.PREFERENCE_NEED_CARDS),))
    members = list(getattr(roster, "members", None) or ())
    if not members:
        return Outcome(replies=(reply(inbound, replies.PREFERENCE_NEED_ROSTER),))

    if inbound.chat_type != "group":
        # §7.1 的口径：志愿填报走私聊。私聊发这句只回他自己的清单，**不动窗口** ——
        # 否则随便一个组员私聊发一句就能把窗口关掉。
        return Outcome(replies=(reply(inbound, allocation.render_task_list(cards)),))

    block, expired = read_window(state, now)
    if block and (expired or inbound.sender_open_id == roster.leader):
        # 第 3 条（组长在群里重发）与第 2 条（超时）都落在这里。
        settled = settle(state, cards, roster, preferences, now)
        if settled is not None:
            return settled
        state = clear(state)                     # 结不了（还没认下群）：清掉窗口，重新开
    elif block:
        # 旁人重发：窗口照旧开着，把清单再发一遍（幂等），不提前封盘、也不刷私聊
        return Outcome(replies=(reply(inbound, allocation.render_task_list(cards)),))

    return open_window(inbound, state, cards, members, now)


def open_window(
    inbound: Inbound,
    state: dict,
    cards: Sequence[TaskCard],
    members: Sequence,
    now: datetime | None = None,
) -> Outcome:
    """开一个志愿窗口：清单发群 + **花名册里每个人**私聊一份（§2.1）。"""
    group = (state or {}).get("group_chat_id") or inbound.chat_id
    block = {
        "opened_at": _iso(now or datetime.now()),
        "opened_by": inbound.sender_open_id,
    }
    text = allocation.render_task_list(cards)
    out = [Reply(chat_id=group, text=text)]
    for member in members:
        if member.open_id:
            out.append(Reply(chat_id=member.open_id, text=text, receive_id_type="open_id"))
    return Outcome(
        replies=tuple(out),
        state={**(state or {}), "awaiting": "preference", "preference": block},
    )


def accept(
    text: str,
    inbound: Inbound,
    state: dict,
    cards: Sequence[TaskCard],
    roster: Roster | None,
    preferences: Sequence[Preference],
    now: datetime | None = None,
) -> Outcome | None:
    """窗口开着时的一条消息：**是志愿就处理，不是志愿返回 ``None``**（交回 7 条前缀）。

    返回 ``None`` 是刻意的：窗口有 5 小时，"完成 T3"「我想提议：…」「作业书」都得照常能用。
    """
    if inbound.chat_type != "p2p" or not inbound.sender_open_id:
        return None                              # 群里的裸数字一律不算（D-54）
    known = {member.open_id for member in (getattr(roster, "members", None) or ())}
    if known and inbound.sender_open_id not in known:
        # 花名册外的人**不给假确认**：他的志愿不进分配（D-53），所以直说没他这个人，
        # 而不是回一句"记下了"然后把他忽略掉。
        return Outcome(replies=(reply(inbound, replies.PREFERENCE_NOT_MEMBER),))
    numbers = _parse_numbers(text)
    if numbers is None:
        return None                              # 含中文 ⇒ 是某条指令，不归 M4 管
    task_ids = [card.task_id for card in cards or ()]
    if not task_ids:
        return None                              # 没清单就没法解释序号：交回前缀
    if not numbers or any(n < 1 or n > len(task_ids) for n in numbers):
        return Outcome(
            replies=(reply(inbound, replies.PREFERENCE_BAD.format(tasks="、".join(task_ids))),)
        )

    ranked = _dedup([task_ids[n - 1] for n in numbers])
    preference = Preference(
        user_id=inbound.sender_open_id,
        ranked_task_ids=ranked,
        submitted_at=_iso(now or datetime.now()),
    )
    preference.validate()

    out = [reply(inbound, replies.PREFERENCE_SAVED.format(tasks=" → ".join(ranked)))]
    result = Outcome(replies=tuple(out), save_preference=preference.to_dict())
    if _all_submitted(preferences, inbound.sender_open_id, roster):
        # 第 1 条结算触发：花名册全员都交过 → 立刻封盘，不等满 5 小时
        settled = settle(state, cards, roster, [*(preferences or ()), preference], now)
        if settled is not None:
            return Outcome(
                replies=(*out, *settled.replies),
                state=settled.state,
                save_preference=preference.to_dict(),
                save_assignments=settled.save_assignments,
            )
    return result


def settle(
    state: dict,
    cards: Sequence[TaskCard],
    roster: Roster | None,
    preferences: Sequence[Preference],
    now: datetime | None = None,
) -> Outcome | None:
    """结算：分配 + 总表发群 + 关窗口。**结不了就返回 ``None``**，绝不让总表无声消失。"""
    cards = list(cards or ())
    members = list(getattr(roster, "members", None) or ())
    group = (state or {}).get("group_chat_id") or ""
    if not cards or not members or not group:
        return None
    assignments = allocation.allocate(cards, roster, preferences)
    return Outcome(
        replies=(Reply(chat_id=group, text=allocation.render_board(assignments, cards, roster)),),
        state=clear(state),
        save_assignments=tuple(record.to_dict() for record in assignments),
    )


# ---------- 小工具 ----------


def _parse_numbers(text: str) -> list[int] | None:
    """``[12, 5]`` = 志愿序号；``None`` = 不是志愿（交回前缀）；``[]`` = 序号没看懂（回提示）。"""
    raw = (text or "").strip()
    if not raw or _CJK.search(raw):
        return None
    tokens = [token for token in _SEPARATORS.split(raw) if token]
    if not tokens or any(not token.isdigit() for token in tokens):
        return []
    return [int(token) for token in tokens]


def _dedup(values: Sequence[str]) -> list[str]:
    """同一个序号写两遍（"2 2"）只算一次，顺序不变。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _all_submitted(
    preferences: Sequence[Preference], sender_id: str, roster: Roster | None
) -> bool:
    """花名册里每个人都交过了吗（花名册外的人不算数 —— 旧名单残留、陌生人不作数）。"""
    members = {member.open_id for member in (getattr(roster, "members", None) or ())}
    if not members:
        return False
    submitted = {p.user_id for p in (preferences or ())} | {sender_id}
    return members <= submitted


def _expired(block: dict, now: datetime | None = None) -> bool:
    """超时判据：``>= PREFERENCE_TTL`` —— 窗口"开放 5 小时"，**到点即到期**。

    （``PENDING_FILE_TTL`` 用的是"超过 30 分钟"因为那是个缓存；这里是个窗口，
    正好 5 小时就该封盘，所以取闭区间。M4 方案 §6 的算例也是这么写的。）
    """
    raw = block.get("opened_at")
    if not raw:
        return False                             # 老 state 没有时间戳：不因此失效
    try:
        opened = datetime.fromisoformat(str(raw))
    except ValueError:
        return False                             # 时间戳脏了就当没有 TTL（同 _stale_pending）
    return (now or datetime.now()) - opened >= PREFERENCE_TTL


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
