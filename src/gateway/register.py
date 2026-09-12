"""「登记」两步确认状态机（§7.7）—— 纯函数，进出都是 dataclass / dict。

依据：`requirements.md` §6.6 / §7.7、D-34、M0 网关方案 §5 / §8。

流程：
  群里发「登记」→ 回空白表单（awaiting=register, stage=collect）
  → 发起人照表单 @ 人填回 → 解析组长/组员，校验 → 回显（stage=confirm, 5 分钟有效）
  → 「同意」→ 交 ``Outcome.save_roster`` 给 app 层落盘；回别的 / 超时 → 作废，不动原名单。

两个刻意的边界：
  * **open_id 只从 @ 结构里取**（D-34）：不需要"读取群成员名单"权限，也不认手打的名字。
  * **组长要进 members**：``models.Roster`` 硬性要求 ``leader ∈ members``，而表单里组长是
    单列一项、通常不在「组员」那一行 —— 落盘前把组长并进去（组长排第一）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Sequence

from src.gateway import replies
from src.gateway.events import Inbound, Mention, Outcome, reply

__all__ = ["REGISTER_TTL", "register_begin", "register_step"]

REGISTER_TTL = timedelta(minutes=5)
_AGREE = "同意"


def register_begin(inbound: Inbound, state: dict, now: datetime | None = None) -> Outcome:
    """收到「登记」：回空白表单，进入 collect 阶段。"""
    new_state = {
        **(state or {}),
        "awaiting": "register",
        "register": {"stage": "collect", "leader": None, "members": [], "expires_at": None},
    }
    return Outcome(replies=(reply(inbound, replies.REGISTER_FORM),), state=new_state)


def register_step(
    text: str, inbound: Inbound, state: dict, now: datetime | None = None
) -> Outcome:
    """awaiting=register 时的分流：collect（填表）/ confirm（确认）。"""
    block = dict((state or {}).get("register") or {})
    stage = block.get("stage")
    if stage == "collect":
        return _collect(text, inbound, state, block, now)
    if stage == "confirm":
        return _confirm(text, inbound, state, block, now)
    # 状态缺胳膊少腿：作废，别把用户卡在 waiting 里
    return Outcome(replies=(reply(inbound, replies.REGISTER_CANCELLED),), state=_cleared(state))


# ---------- 第一步：收表 ----------


def _collect(
    text: str, inbound: Inbound, state: dict, block: dict, now: datetime | None
) -> Outcome:
    if not inbound.mentions:
        # 手打名字但一个 @ 都没有：D-34 只认 @ 结构里的 open_id，给最直白的提示
        return _stay(inbound, replies.REGISTER_FORM_BAD)

    leader = _distinct(_mentions_in(_section(text, r"组长\s*[:：](.*)"), inbound.mentions))
    members = _distinct(_mentions_in(_section(text, r"组员\s*[:：](.*)"), inbound.mentions))

    if any(not m.open_id for m in (*leader, *members)):
        return _stay(inbound, replies.REGISTER_FORM_BAD)
    if len(leader) != 1:
        return _stay(inbound, replies.REGISTER_NEED_LEADER)
    if len(members) < 2:
        return _stay(inbound, replies.REGISTER_NEED_MEMBERS)

    leader_mention = leader[0]
    others = [m for m in members if m.open_id != leader_mention.open_id]
    expires_at = _iso((now or datetime.now()) + REGISTER_TTL)
    new_block = {
        "stage": "confirm",
        "leader": {"open_id": leader_mention.open_id, "name": _display(leader_mention)},
        "members": [{"open_id": m.open_id, "name": _display(m)} for m in others],
        "expires_at": expires_at,
    }
    return Outcome(
        replies=(
            reply(
                inbound,
                replies.REGISTER_CONFIRM.format(
                    leader=_display(leader_mention),
                    members="、".join(_display(m) for m in others),
                    total=len(others) + 1,
                ),
            ),
        ),
        state={**(state or {}), "register": new_block},
    )


# ---------- 第二步：确认 ----------


def _confirm(
    text: str, inbound: Inbound, state: dict, block: dict, now: datetime | None
) -> Outcome:
    if _expired(block, now):
        return Outcome(replies=(reply(inbound, replies.REGISTER_EXPIRED),), state=_cleared(state))

    if text.strip() != _AGREE:
        return Outcome(
            replies=(reply(inbound, replies.REGISTER_CANCELLED),), state=_cleared(state)
        )

    leader = dict(block.get("leader") or {})
    members = [dict(m) for m in (block.get("members") or [])]
    roster = {
        "leader": leader.get("open_id", ""),
        "members": [leader, *members],          # Roster 要求 leader ∈ members
        "registered_at": _iso(now or datetime.now()),
        "confirmed_by": inbound.sender_open_id,
    }
    return Outcome(
        replies=(
            reply(
                inbound,
                replies.REGISTER_SAVED.format(
                    leader=leader.get("name") or leader.get("open_id", ""),
                    total=len(roster["members"]),
                ),
            ),
        ),
        state=_cleared(state),
        save_roster=roster,
    )


# ---------- 小工具 ----------


def _section(text: str, pattern: str) -> str:
    """取「组长：…」/「组员：…」这一行冒号后面的内容。"""
    matcher = re.compile(pattern)
    for line in (text or "").splitlines():
        found = matcher.search(line)
        if found:
            return found.group(1)
    return ""


def _mentions_in(section: str, mentions: Sequence[Mention]) -> list[Mention]:
    """按 @ 占位符优先、退化为显示名匹配 —— 两者都只说明"这行 @ 了谁"。"""
    if not section:
        return []
    return [
        m
        for m in mentions
        if (m.key and m.key in section) or (m.name and m.name in section)
    ]


def _distinct(mentions: Sequence[Mention]) -> list[Mention]:
    seen: set[str] = set()
    result: list[Mention] = []
    for mention in mentions:
        if mention.open_id in seen:
            continue
        seen.add(mention.open_id)
        result.append(mention)
    return result


def _display(mention: Mention) -> str:
    return mention.name or mention.open_id[:8] or "（无名）"


def _expired(block: dict, now: datetime | None) -> bool:
    raw = block.get("expires_at")
    if not raw:
        return False
    return (now or datetime.now()) > datetime.fromisoformat(raw)


def _cleared(state: dict) -> dict:
    return {**(state or {}), "awaiting": None, "register": None}


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _stay(inbound: Inbound, text: str) -> Outcome:
    """表单不合格：不改 state，留在 collect 等重发。"""
    return Outcome(replies=(reply(inbound, text),))
