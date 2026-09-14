"""M2 方向候选投票窗口（§7.1 / §7.6 / D-33 / D-35 / D-36）—— 纯函数，形状照 ``preference.py``。

流程（方案 §2.2 ~ §2.6）：

  群里发「方向」→ 后台生成 2–3 个候选 → 候选发群 + 开投票窗口（``awaiting=vote``）
  → 组员在**开窗那个群**回裸数字 → 一人一票、后投覆盖
  → 关闭（**三条任一，只关一次**）：某方向"已投票者过半"且过门槛（D-35 / D-36）/
    超时 10 分钟（``VOTE_TTL``，到期后**任何**到达的消息都会触发收口）/
    组长「封盘」拍板
  → 落 ``data/direction.json`` + 群里报「方向已定」。

三条口径：

  * **只认开窗那个群 + 花名册成员**，两个条件同时成立才算票：私聊数字照走 7 条前缀；
    别的群 / 非花名册成员静默不计（§5 推荐 3）；越界数字回一句 ``VOTE_BAD``、不落盘。
  * **门槛按 D-36**：分母是"投过票的人"，且已投票人数 ≥ ``ceil(花名册人数 / 2)``
    —— 防"第 1 票就宣布过半"。
  * **组长拍板复用 ``封盘``**（与 M4 同一个词，不新增第 8 条前缀）：``封盘`` 取票最多
    （并列取编号小），``封盘 N`` 直接指定；都只在投票状态机内消费、不进 7 条前缀。

``M4 的志愿窗口是 5 小时``（``PREFERENCE_TTL``），M2 的投票窗口是 10 分钟（``VOTE_TTL``）
—— 两个窗口不要混。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Sequence

from src.gateway import replies
from src.gateway.events import Inbound, Outcome, Reply, reply
from src.gateway.preference import SEAL_WORD

__all__ = [
    "VOTE_TTL",
    "read_window",
    "command",
    "open_window",
    "accept",
    "should_close",
    "close_expired",
    "settle",
    "clear",
]

VOTE_TTL = timedelta(minutes=10)

# 含中文的消息一定是"某条指令"（7 条前缀里没有一条是纯 ASCII，且组长拍板的「封盘」
# 也是中文），不可能是候选编号；不含中文又不是纯数字的（"abc" / "3abc"）才是"没看懂"。
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_SEPARATORS = re.compile(r"[\s,，、]+")

REASON_MAJORITY = "过半落定"
REASON_LEADER = "组长拍板"
REASON_LEADER_AFTER_TIMEOUT = "超时后组长指定"


def read_window(state: dict, now: datetime | None = None) -> tuple[dict, bool]:
    """``(窗口块, 是否已过期)``。没有窗口 → ``({}, False)``。

    过期时**照样把块返回**：调用方要用它来报明细 / 结算，而不是装作无事发生（D-35）。
    """
    block = dict((state or {}).get("vote") or {})
    if not block:
        return {}, False
    return block, _expired(block, now)


def clear(state: dict) -> dict:
    """关掉投票窗口（``awaiting`` 与 ``vote`` 一起清）—— 裸数字立刻不再被当票。"""
    return {**(state or {}), "awaiting": None, "vote": None}


def command(
    inbound: Inbound,
    state: dict,
    roster,
    *,
    has_rubric: bool = False,
    now: datetime | None = None,
) -> Outcome:
    """「方向」：群里 = 起后台生成 + 开窗；私聊 = 指出"去群里发"（§2.2）。

    前置缺哪句就回哪句，且**都不起 pipeline**（同必修 4 的口径：回话与起不起重活同源）。
    """
    if not has_rubric:
        # D-48 口径：没有评分点就不生成，不烧 token
        return Outcome(replies=(reply(inbound, replies.NEEDS_RUBRIC),))
    members = list(getattr(roster, "members", None) or ())
    if not members:
        return Outcome(replies=(reply(inbound, replies.VOTE_NEED_ROSTER),))
    if inbound.chat_type != "group":
        # 投票是群里的动作（T05 原文"回复数字投票"）。私聊发这句只指出正确去处。
        return Outcome(replies=(reply(inbound, replies.VOTE_NEED_GROUP),))

    block, expired = read_window(state, now)
    if block and not expired and not block.get("closed"):
        # 窗口还在（且没被冻住）：不重新生成、不重置票（防有人顺手把票清空）
        return Outcome(
            replies=(
                reply(inbound, replies.VOTE_IN_PROGRESS.format(minutes=_remaining(block, now))),
            )
        )
    # 没窗口 / 已过期 / 已冻住 → 重新生成候选、开新窗口（§2.2）
    return Outcome(replies=(reply(inbound, replies.VOTE_GENERATING),), pipeline="direction")


def open_window(
    inbound: Inbound,
    state: dict,
    candidates: Sequence[dict],
    now: datetime | None = None,
) -> Outcome:
    """落窗口块 + 候选发群（由 app 层在 LLM 生成成功之后调用，§2.6）。

    ``candidates`` 是**纯数据**（``Direction.to_dict()``），本模块不认识智能层的类型。
    """
    group = (state or {}).get("group_chat_id") or inbound.chat_id
    block = {
        "chat_id": group,
        "opened_at": _iso(now or datetime.now()),
        "opened_by": inbound.sender_open_id,
        "candidates": [dict(item) for item in candidates],
        "votes": {},
    }
    return Outcome(
        replies=(Reply(chat_id=group, text=_render_candidates(candidates)),),
        # 切状态机要清旧窗口（P0-D / §2.7）：``awaiting`` 是全局单值，register / preference
        # 的残留会跟投票互相吃掉对方的裸数字。
        state={
            **(state or {}),
            "awaiting": "vote",
            "vote": block,
            "register": None,
            "preference": None,
        },
    )


def accept(
    text: str,
    inbound: Inbound,
    state: dict,
    roster,
    now: datetime | None = None,
) -> Outcome | None:
    """窗口里的一条消息：**是投票 / 封盘就消费，否则返回 ``None``**，交回 7 条前缀。

    返回 ``None`` 是刻意的：窗口开着时「拆解」「作业书」这类指令必须照常走前缀，
    否则就是一个吃掉指令的死锁窗口（必修 1 的同款病）。返回 ``Outcome()``（空）才是
    "静默不计"：消息被窗口消费掉、但一个字都不回。

    会话口径（§2.3）：投票只在**开窗那个群**里算数。私聊的裸数字交回前缀（照走兜底文案）；
    别的群的裸数字静默不计（§5 推荐 3）；指令（含中文）一律放行。

    窗口被**冻住**（``vote.closed``，超时后）时：数字静默不计，**只有组长还能 ``封盘`` 拍板** ——
    冻住就是为了让他在同一批候选、同一张票数表上落定，而不是被迫重开一轮。

    超时收口本身在 ``close_expired()``：路由层**先收口、再照原路走一遍**，所以「拆解」
    这种消息也能触发收口、**且照常执行**（外审必修 A；只把 ``if expired`` 提到前面会吃掉指令）。
    """
    block, expired = read_window(state, now)
    if not block:
        return None
    stripped = (text or "").strip()
    group = block.get("chat_id") or (state or {}).get("group_chat_id") or ""
    in_window = bool(group) and inbound.chat_type == "group" and inbound.chat_id == group
    seal = _is_seal(stripped)
    numbers = _parse_numbers(stripped)

    if not in_window:
        if seal or numbers is None:
            return None                          # 指令 → 交回前缀
        return None if inbound.chat_type != "group" else Outcome()

    if block.get("closed"):
        # 已冻住的窗口（超时后，§7.1 / M2 复核 P1）：数字**静默不计**，但组长仍可在
        # **同一批候选**上拍板 —— 冻住的目的就是别让他从头再生成一轮、编号对不上票数表。
        if seal:
            return _seal(stripped, inbound, state, block, roster, now, expired=True)
        if numbers is None:
            return None                          # 指令照常放行（「方向」会开新窗口）
        return Outcome()                         # 数字：不计票、不刷屏

    if seal:
        return _seal(stripped, inbound, state, block, roster, now, expired=expired)
    if numbers is None:
        return None                              # 含中文（指令）→ 交回前缀；「方向」会重开窗
    if expired:
        # 超时（D-35）：有人过半就落定，没人过半就报明细 + **冻住窗口**（§2.4 条 2 / §7.1）
        return _timeout(inbound, state, block, roster, now)

    members = list(getattr(roster, "members", None) or ())
    known = {member.open_id for member in members}
    if known and inbound.sender_open_id not in known:
        return Outcome()                         # 非花名册成员：静默不计（§5 推荐 3）

    candidates = _candidates(block)
    if not numbers or any(number < 1 or number > len(candidates) for number in numbers):
        return Outcome(
            replies=(reply(inbound, replies.VOTE_BAD.format(ids=_render_ids(candidates))),)
        )
    # 一人一票：一条消息里写多个数字时只认**第一个**（"1 2" 这种多半是手滑）
    return _cast(state, block, inbound, numbers[0], candidates, roster, now)


def should_close(text: str) -> bool:
    """这条消息要不要**顺手把到期的窗口收口**（外审必修 A）。

    口径：**不是数字、也不是「封盘」** → 要收口。数字走 ``accept()`` 里既有的
    ``_timeout()``（有人过半就落定、没人过半就冻住）、「封盘」走 ``_seal(expired=True)``；
    剩下的（指令、闲聊）以前会被静默放过 —— 盘上仍是 ``awaiting="vote"``、``closed``
    不写、那句票数明细也不发，"10 分钟自动报明细"实际退化成"得等下一条数字"。

    只判"这条像不像数字 / 封盘"，**不判窗口在不在、过期没** —— 那是 ``close_expired()``
    的事。这样切开，调用方才能"先收口、再照常处理这条消息本身"。
    """
    return _parse_numbers(text) is None and not _is_seal((text or "").strip())


def close_expired(
    inbound: Inbound, state: dict, roster, now: datetime | None = None
) -> Outcome | None:
    """超时到点就收口：报票数明细 + 冻结窗口（有人过半则直接落定）。

    返回 ``None`` = 不用收口（没窗口 / 还没到点 / 已经冻住过）。**幂等**：收口过的窗口
    ``closed = true``，再调也只返回 ``None``，不会重复发明细。

    ⚠️ 调用方拿到收口结果后**仍要继续处理这条消息本身**：超时后第一条到达的消息很可能
    就是「拆解」，"收口"与"不许吞掉指令"两条必须同时成立（外审必修 A）。
    """
    block, expired = read_window(state, now)
    if not block or not expired or block.get("closed"):
        return None
    return _timeout(inbound, state, block, roster, now)


def settle(
    state: dict, roster, now: datetime | None = None
) -> Outcome | None:
    """过半（且过门槛）→ 落定；结不了返回 ``None``，绝不无声吞掉结果。"""
    block = dict((state or {}).get("vote") or {})
    candidates = _candidates(block)
    if not candidates:
        return None
    winner = _majority_winner(dict(block.get("votes") or {}), candidates, roster)
    if winner is None:
        return None
    return _settled(state, block, winner, reason=REASON_MAJORITY, decided_by="vote", now=now)


# ---------- 关闭的三条路 ----------


def _cast(
    state: dict,
    block: dict,
    inbound: Inbound,
    pick: int,
    candidates: list,
    roster,
    now: datetime | None,
) -> Outcome:
    """记一票 + 回执；若这一票正好凑成"过半且过门槛"，顺带落定。"""
    votes = {str(key): _as_int(value) for key, value in (block.get("votes") or {}).items()}
    votes[inbound.sender_open_id] = int(pick)
    block = {**block, "votes": votes}
    state = {**(state or {}), "vote": block}
    title = next(
        (str(item.get("title") or "") for item in candidates if _as_int(item.get("id")) == pick),
        "",
    )
    ack = Outcome(
        replies=(reply(inbound, replies.VOTE_ACK.format(id=pick, title=title)),),
        state=state,
    )
    settled = settle(state, roster, now)
    if settled is None:
        return ack
    return Outcome(
        replies=(*ack.replies, *settled.replies),
        state=settled.state,
        save_direction=settled.save_direction,
    )


def _timeout(
    inbound: Inbound, state: dict, block: dict, roster, now: datetime | None
) -> Outcome:
    """超时到点：有人过半就落定；没人过半就报明细 + **冻住窗口**（不落盘）。

    冻住 = ``vote.closed = true``（``awaiting`` 保持 ``"vote"``、候选与票数原样保留）：
    数字不再计票，但组长还能在这**同一批候选**上 ``封盘`` / ``封盘 N`` 拍板（§7.1 的
    "超时交组长拍板"）。清空窗口（``clear()``）只留给落定那条路。
    """
    candidates = _candidates(block)
    votes = dict(block.get("votes") or {})
    winner = _majority_winner(votes, candidates, roster)
    if winner is not None:
        return _settled(state, block, winner, reason=REASON_MAJORITY, decided_by="vote", now=now)
    group = block.get("chat_id") or (state or {}).get("group_chat_id") or inbound.chat_id
    return Outcome(
        replies=(
            Reply(
                chat_id=group,
                text=replies.VOTE_TIMEOUT.format(tally=_render_tally(votes, candidates)),
            ),
        ),
        state={
            **(state or {}),
            "awaiting": "vote",
            "vote": {**block, "closed": True},
        },
    )


def _seal(
    text: str,
    inbound: Inbound,
    state: dict,
    block: dict,
    roster,
    now: datetime | None,
    *,
    expired: bool,
) -> Outcome:
    """组长拍板：``封盘`` 取票最多（并列取编号小），``封盘 N`` 直接指定（没票也能用）。"""
    if inbound.sender_open_id != getattr(roster, "leader", None):
        return Outcome(replies=(reply(inbound, replies.VOTE_NEED_LEADER),))

    candidates = _candidates(block)
    ids = [_as_int(item.get("id")) for item in candidates]
    raw = text[len(SEAL_WORD):].strip()
    if not raw:
        tally = _tally(dict(block.get("votes") or {}), candidates)
        top = max(tally.values()) if tally else 0
        if top <= 0:
            # 一票都没有 → 没有"票最多"可取，让组长点名（§2.5）
            return Outcome(replies=(reply(inbound, replies.VOTE_SEAL_NEED_PICK),))
        pick = min(cid for cid, count in tally.items() if count == top)
    elif not raw.isdigit() or int(raw) not in ids:
        return Outcome(
            replies=(reply(inbound, replies.VOTE_BAD.format(ids=_render_ids(candidates))),)
        )
    else:
        pick = int(raw)

    reason = REASON_LEADER_AFTER_TIMEOUT if expired else REASON_LEADER
    return _settled(state, block, pick, reason=reason, decided_by="leader", now=now)


def _settled(
    state: dict,
    block: dict,
    winner_id: int,
    *,
    reason: str,
    decided_by: str,
    now: datetime | None,
) -> Outcome:
    """落 ``data/direction.json``（整份覆盖、裸 JSON）+ 群里报「方向已定」+ 关窗（§2.6）。"""
    candidates = _candidates(block)
    votes = dict(block.get("votes") or {})
    group = block.get("chat_id") or (state or {}).get("group_chat_id") or ""
    winner = next(item for item in candidates if _as_int(item.get("id")) == winner_id)
    tally = _tally(votes, candidates)
    voters = len({oid for oid, pick in votes.items() if _as_int(pick) in tally})
    detail = f"过半：{tally[winner_id]}/{voters} 票" if reason == REASON_MAJORITY else reason
    payload = {
        "decided_at": _iso(now or datetime.now()),
        "decided_by": decided_by,
        "winner": {
            "id": winner_id,
            "title": str(winner.get("title") or ""),
            "note": str(winner.get("note") or ""),
        },
        "candidates": candidates,
        "votes": votes,
        "tally": {str(cid): count for cid, count in tally.items()},
        "reason": reason,
    }
    return Outcome(
        replies=(
            Reply(
                chat_id=group,
                text=replies.VOTE_SETTLED.format(
                    id=winner_id,
                    title=str(winner.get("title") or ""),
                    detail=detail,
                ),
            ),
        ),
        state=clear(state),
        save_direction=payload,
    )


# ---------- 判定小工具 ----------


def _majority_winner(votes: dict, candidates: list, roster) -> int | None:
    """"已投票者过半 + 过门槛"的赢家；不成立返回 ``None``（D-35 / D-36）。"""
    tally = _tally(votes, candidates)
    if not tally:
        return None
    voters = len({oid for oid, pick in votes.items() if _as_int(pick) in tally})
    if voters < _threshold(roster):
        return None                              # 没过门槛：绝不宣布过半（D-36）
    top = max(tally.values())
    if top <= 0 or top * 2 <= voters:
        return None                              # 必须**严格**过半
    return min(cid for cid, count in tally.items() if count == top)   # 并列取编号小


def _threshold(roster) -> int:
    """D-36：门槛 = 花名册人数的过半，向上取整（3 人 → 2 人）。"""
    members = list(getattr(roster, "members", None) or ())
    return max(1, math.ceil(len(members) / 2)) if members else 1


def _tally(votes: dict, candidates: Sequence[dict]) -> dict[int, int]:
    """``{候选编号: 票数}``，候选编号全在、初始为 0（越界票不算）。"""
    tally = {_as_int(item.get("id")): 0 for item in candidates}
    for pick in votes.values():
        number = _as_int(pick)
        if number in tally:
            tally[number] += 1
    return tally


def _parse_numbers(text: str) -> list[int] | None:
    """``[2]`` = 投了 2 号；``None`` = 不是投票（交回前缀）；``[]`` = 没看懂（回提示）。"""
    raw = (text or "").strip()
    if not raw or _CJK.search(raw):
        return None
    tokens = [token for token in _SEPARATORS.split(raw) if token]
    if not tokens or any(not token.isdigit() for token in tokens):
        return []
    return [int(token) for token in tokens]


def _is_seal(text: str) -> bool:
    return bool(text) and text.startswith(SEAL_WORD)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _candidates(block: dict) -> list[dict]:
    return [dict(item) for item in (block.get("candidates") or ()) if isinstance(item, dict)]


def _render_ids(candidates: Sequence[dict]) -> str:
    ids = [str(_as_int(item.get("id"))) for item in candidates]
    return "、".join(ids) if ids else "（无）"


def _render_tally(votes: dict, candidates: Sequence[dict]) -> str:
    tally = _tally(votes, candidates)
    if not any(tally.values()):
        return "还没有人投票"
    return " / ".join(f"{cid} 号 {count} 票" for cid, count in tally.items())


def _render_candidates(candidates: Sequence[dict]) -> str:
    items: list[str] = []
    for item in candidates:
        line = f"{_as_int(item.get('id'))}. {str(item.get('title') or '').strip()}"
        note = str(item.get("note") or "").strip()
        if note:
            line += f"\n   {note}"
        items.append(line)
    return replies.VOTE_CANDIDATES.format(items="\n".join(items))


def _expired(block: dict, now: datetime | None = None) -> bool:
    """超时判据：``>= VOTE_TTL`` —— 窗口"开放 10 分钟"，**到点即到期**（§7.1 明文）。"""
    return _elapsed(block, now) >= VOTE_TTL


def _elapsed(block: dict, now: datetime | None = None) -> timedelta:
    raw = block.get("opened_at")
    if not raw:
        return timedelta(0)                      # 老 state 没有时间戳：不因此失效
    try:
        opened = datetime.fromisoformat(str(raw))
    except ValueError:
        return timedelta(0)                      # 时间戳脏了就当没有 TTL
    return (now or datetime.now()) - opened


def _remaining(block: dict, now: datetime | None = None) -> int:
    remaining = VOTE_TTL - _elapsed(block, now)
    return max(0, math.ceil(remaining.total_seconds() / 60))


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
