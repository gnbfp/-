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

__all__ = [
    "REGISTER_TTL",
    "REGISTER_CANCEL_WORDS",
    "is_cancel",
    "classify",
    "register_begin",
    "register_step",
    "register_cancel",
]

REGISTER_TTL = timedelta(minutes=5)
_AGREE = "同意"
# 逃生词：登记窗口的出口。没有它，一次误触「登记」就把整个群的指令都吃掉（必修 1）。
REGISTER_CANCEL_WORDS = ("取消登记", "取消")

# 表单行的形状：「组长：…」「组员：…」各自**独占一行**。
# 必须与 _section() 的解析口径一致，否则会出现「接管了却解析不出来」。
_FORM_LINE = re.compile(r"^[ \t]*(组长|组员)[ \t]*[:：]", re.M)


def register_begin(
    inbound: Inbound, state: dict, now: datetime | None = None, roster=None
) -> Outcome:
    """收到「登记」：回空白表单，进入 collect 阶段（只有发起人能把它填完）。

    **重登记限组长**（D-34 补充口径）：已有花名册时只有现任组长能重开登记 ——
    否则任何人群里发一次「登记」、把自己填成组长，一句「同意」就把 leader 换掉、
    之后还能用组长口令（F1 复现）。花名册为空（还没登记过）时任何人可登记。
    """
    if getattr(roster, "leader", "") and roster.leader != inbound.sender_open_id:
        return Outcome(replies=(reply(inbound, replies.REGISTER_LEADER_ONLY),))
    new_state = {
        **(state or {}),
        "awaiting": "register",
        "preference": None,          # 切状态机要清掉旧志愿窗口（P0-D）
        "vote": None,                # 也清掉投票窗口（外审必修 B；反向见 preference.open_window）
        "register": {
            "stage": "collect",
            "initiator_open_id": inbound.sender_open_id,
            "leader": None,
            "members": [],
            # collect 也要有 TTL：原先设 None ⇒ 发一次「登记」不填表就永久锁群（必修 1）
            "expires_at": _iso((now or datetime.now()) + REGISTER_TTL),
        },
    }
    return Outcome(replies=(reply(inbound, replies.REGISTER_FORM),), state=new_state)


def is_cancel(text: str) -> bool:
    """这句话是不是登记窗口的逃生词。"""
    return (text or "").strip() in REGISTER_CANCEL_WORDS


def classify(block: dict, inbound: Inbound, text: str, now: datetime | None = None) -> str:
    """这条消息归登记状态机管吗？（必修 6）

    三态：
      * ``"step"``   —— 交给 ``register_step()``；
      * ``"silent"`` —— 归状态机但不回话（§7.7「旁人发言静默忽略」）；
      * ``"pass"``   —— 不归状态机，照走 7 条前缀。

    **collect 阶段必须"带 @ 且 长得像表单"两项同时成立**，缺一不可：
    飞书用户的习惯就是发指令前先 @ 机器人，只按「带 @」接管会把指令当表单吃掉
    （真机复现：发起人发「@机器人 方向」被回「组长要正好 1 个人」，
    旁人发同一句则一个字都不回）。**带 @ != 表单消息。**

    ``now`` 只为一件事存在：**过期窗口一律走 ``"step"``**，好让 ``register_step()``
    的过期分支把它清掉（上一轮复核定下的「超时窗口谁说话都能清掉」，见 ``register_step``）。
    """
    block = block or {}
    if _expired(block, now):
        return "step"
    stage = block.get("stage")
    initiator = block.get("initiator_open_id")
    is_initiator = not initiator or initiator == inbound.sender_open_id

    if stage == "confirm":
        # 机器人已明说「回复别的就作废」⇒ 发起人的任何话都算数
        return "step" if is_initiator else "silent"

    if not inbound.mentions or not _FORM_LINE.search(text or ""):
        return "pass"
    return "step" if is_initiator else "silent"


def register_cancel(inbound: Inbound, state: dict, now: datetime | None = None) -> Outcome:
    """逃生词：主动退出登记窗口（必修 1）。只认发起人，旁人说了不算。

    过期窗口是例外：它已经作废，逃生词谁先说都只是把残留状态清干净 —— 与
    ``register_step`` 的「过期判在发起人之前」同一条理，免得卡住的 ``awaiting`` 没人能清。
    """
    block = dict((state or {}).get("register") or {})
    if _expired(block, now):
        return Outcome(replies=(reply(inbound, replies.REGISTER_EXPIRED),), state=_cleared(state))
    initiator = block.get("initiator_open_id")
    if initiator and initiator != inbound.sender_open_id:
        return Outcome()
    return Outcome(replies=(reply(inbound, replies.REGISTER_CANCELLED),), state=_cleared(state))


def register_step(
    text: str, inbound: Inbound, state: dict, now: datetime | None = None
) -> Outcome:
    """awaiting=register 时的分流：collect（填表）/ confirm（确认）。

    三条纪律（外审必修 1、2）：
      * **过期判在发起人之前**（顺序有讲究）：超时的窗口已经作废，谁说话都该把它清掉。
        反过来的话，confirm 过期后若发起人不再开口，旁人的「作业书」「拆解」会被静默
        吞掉、`awaiting` 永远卡在 ``register``。collect 与 confirm 一视同仁；
      * **非发起人静默忽略**：不推进、不作废、不回话。旁人说一句「好」不该把登记
        作废，也不该收到一长串表单刷屏（§7.7 的「组长回「同意」」）；
      * 状态缺胳膊少腿 → 作废，别把人卡在 waiting 里。
    """
    block = dict((state or {}).get("register") or {})
    if _expired(block, now):
        return Outcome(replies=(reply(inbound, replies.REGISTER_EXPIRED),), state=_cleared(state))
    initiator = block.get("initiator_open_id")
    if initiator and initiator != inbound.sender_open_id:
        return Outcome()
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
    # 从 route() 已不可达（classify() 进 "step" 的前提已包含 inbound.mentions）；
    # 保留作 register_step() 这个公开入口的防御 —— 直接调用者仍会拿到最直白的提示。
    # 有测试守着：tests/test_gateway_register.py::test_collect_without_any_mention_explains_at_syntax
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
        # 必须把发起人带过去：confirm 阶段全靠它挡住"旁人一句「同意」就落盘"（必修 2）
        "initiator_open_id": block.get("initiator_open_id") or inbound.sender_open_id,
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
    # 登记窗口退出 / 作废时，一并清掉可能残留的志愿 / 投票窗口（P0-D / 外审必修 B）：
    # 三个状态机都吃裸数字，谁残留都会把别人的数字吃掉
    return {**(state or {}), "awaiting": None, "register": None, "preference": None, "vote": None}


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _stay(inbound: Inbound, text: str) -> Outcome:
    """表单不合格：不改 state，留在 collect 等重发。"""
    return Outcome(replies=(reply(inbound, text),))
