"""M0 路由 + M4/M5 的指令入口 —— 纯函数。**铁律：本文件不许 import lark_oapi**（方案 §1）。

依据：`requirements.md` §7.1（D-33「先剥 @段 → 再看状态 → 最后认前缀」+ 7 条前缀）、
D-42（文字与附件必然是两条消息）、**M4 志愿分配 / M5 匿名代言**（D-52~D-55）、
M0 网关方案 §4 / §5 / §6 / §7。

进出都是纯数据（``Inbound`` / ``dict`` / ``Outcome``）：不联网、不发消息、不读文件，
所以这一整套规则可以在没有飞书、没有网络的情况下全量单测。

三条指令的状态机在各自的文件里，本文件只做"接管 or 放行"：
  * 「方向」→ ``vote.command()``（群里起 M2 生成候选并开投票窗口；组长「封盘」拍板）；
  * 「你想做哪一块」→ ``preference.command()``（群里开窗口 / 组长重发=封盘）；
  * 「我想提议：…」→ ``_proposal()``（匿名转达 + 留痕）。

两条文档没写死、按方案 §4 的工程默认（可推翻）落地：
  * 「我想提议」全角 ``：`` 与半角 ``:`` 都认（中文输入法容易出半角）；
  * 「完成 T3」用 ``^完成\\s*[Tt]\\d+``，容忍空格与大小写。
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Sequence

from src.gateway import preference, register, replies, vote
from src.gateway.events import Inbound, Mention, Outcome, Reply, reply

__all__ = [
    "PROPOSAL_PREFIXES",
    "COMPLETE_PATTERN",
    "PENDING_FILE_TTL",
    "strip_mentions",
    "route",
    "remember_file",
]

# 7 条前缀里两条带变体：提议的冒号全半角、完成的 Tn 容忍空格与大小写。
PROPOSAL_PREFIXES = ("我想提议：", "我想提议:")
COMPLETE_PATTERN = re.compile(r"^完成\s*[Tt]\d+")

_MENTION_PLACEHOLDER = re.compile(r"@_user_\d+")

# 缓存文件的有效期（D-46，工程默认，可推翻）。``pending_file`` 是**落盘**的、关掉重启仍在，
# 而唯一的清除时机是「作业书」跑完 —— 所以"发过文件、没接着说「作业书」"的残留会一直留着，
# 跨场次录制时会静默复用一份旧作业书。30 分钟对一场演示足够，只拦跨场次串味。
PENDING_FILE_TTL = timedelta(minutes=30)


def strip_mentions(text: str, mentions: Sequence[Mention] = ()) -> str:
    """剥掉 @段，只留正文。

    先按 mentions 里的占位符精确替换（``@_user_1``），再用正则兜底 —— 消息里的
    @ 段必须剥掉才能做前缀匹配，但 **mentions 本身不能丢**：「登记」靠它的 open_id（D-34）。
    """
    for mention in mentions:
        if mention.key:
            text = text.replace(mention.key, "")
    return _MENTION_PLACEHOLDER.sub("", text or "")


def route(
    inbound: Inbound,
    state: dict,
    roster=None,
    *,
    has_rubric: bool = False,
    cards: Sequence = (),
    preferences: Sequence = (),
    now: datetime | None = None,
    source_title: str = "",
) -> Outcome:
    """一条消息 → 一个 Outcome。顺序严格按 D-33：剥 @段 → 状态 → 前缀 → 兜底。

    ``roster`` / ``cards`` / ``preferences`` 由 app 层从 ``data/`` 读出来传进来 ——
    router 自己不读文件，但仍然能对 M4 给出正确回复：开窗口、收志愿、结算、发总表。
    传进的是**数据**不是**路径**，所以这一整套规则照样能离线全量单测。
    ``has_rubric`` 由 app 层从 ``data/rubric.json`` 读出来传进来 —— router 自己不读文件，
    但仍然能对「拆解」给出正确回复（没有评分点 vs 重跑）。

    **重活也在这里判**（``Outcome.pipeline``）：回什么话与起不起 M1/M3 必须同源，
    分两处判就会出现"回了「表单没看懂」却照样烧一次 LLM"（必修 4）。
    """
    original = state
    # 0. 机器人自己的消息：权限里开了 include_bot，这条不丢就会自己回自己
    if inbound.sender_type == "app":
        return Outcome()

    # 1. 文件：只缓存，不干活（D-42：文字和附件必然是两条消息）
    if inbound.message_type == "file":
        return remember_file(inbound, state, now)
    #    图片：读不了就直说（用户 2026-09-12 拍板，不再静默），但仍然**不入缓存** ——
    #    必修 3 的底线是"一张图不能把刚发来的作业书 PDF 挤掉"。其余类型保持静默。
    if inbound.message_type == "image":
        return Outcome(replies=(reply(inbound, replies.IMAGE_REJECTED),))
    if inbound.message_type != "text":
        return Outcome()

    text = strip_mentions(inbound.text, inbound.mentions).strip()
    if not text:
        return Outcome()                       # 纯 @ 段 / 空文本：静默，不刷屏

    # 2. 状态优先：裸数字/表单怎么解释，全看 state.json 的 awaiting
    awaiting = (state or {}).get("awaiting")
    if awaiting == "register":
        # 先给出口：没有逃生词，一次误触「登记」不填表就吃掉整个群的所有指令（必修 1）
        if register.is_cancel(text):
            return register.register_cancel(inbound, state, now)
        # 谁归状态机管由 register.classify() 一条规则说了算（必修 6）：
        # "step" 交给状态机、"silent" 归它但不回话、"pass" 继续往下走 7 条前缀 ——
        # 只按「带 @」接管的话，指令会被当成表单吃掉、或被静默吞掉（真机已复现）
        mode = register.classify((state or {}).get("register") or {}, inbound, inbound.text, now)
        if mode == "step":
            # 表单要吃**原文**：@ 占位符（@_user_1）是"这行 @ 了谁"的唯一线索，
            # 剥掉就再也对不上 open_id 了（D-34：id 只从 @ 结构里取）
            return register.register_step(inbound.text, inbound, state, now)
        if mode == "silent":
            return Outcome()
    if awaiting == "vote":
        # 方向投票窗口（M2，D-35 / D-36）：只认开窗那个群的花名册成员；
        # **不命中一律回退 7 条前缀** —— 窗口开着时「拆解」「作业书」必须照常干活，
        # 不然就是一个吃掉指令的死锁窗口（必修 1 的同款病）。
        hit = vote.accept(text, inbound, state, roster, now)
        if hit is not None:
            return hit
        return _merge(
            _by_prefix(
                inbound,
                state,
                roster,
                has_rubric=has_rubric,
                cards=cards,
                preferences=preferences,
                now=now,
                source_title=source_title,
            ),
            state,
            original,
        )
    if awaiting == "preference":
        # 志愿窗口（5 小时，D-52~D-54）：过期就当场结算，没过期就试收志愿；
        # 都不是（群里发数字 / 私聊发指令）→ 照走 7 条前缀。
        block, expired = preference.read_window(state, now)
        if block and expired:
            settled = preference.settle(state, cards, roster, preferences, now)
            if settled is not None:
                return settled
            state = preference.clear(state)        # 结不了（没认下群）：清残留，别卡住 awaiting
        elif block:
            hit = preference.accept(text, inbound, state, cards, roster, preferences, now)
            if hit is not None:
                return hit
        return _merge(
            _by_prefix(
                inbound,
                state,
                roster,
                has_rubric=has_rubric,
                cards=cards,
                preferences=preferences,
                now=now,
                source_title=source_title,
            ),
            state,
            original,
        )

    return _by_prefix(
        inbound,
        state,
        roster,
        has_rubric=has_rubric,
        cards=cards,
        preferences=preferences,
        now=now,
        source_title=source_title,
    )


def _by_prefix(
    inbound: Inbound,
    state: dict,
    roster=None,
    *,
    has_rubric: bool = False,
    cards: Sequence = (),
    preferences: Sequence = (),
    now: datetime | None = None,
    source_title: str = "",
) -> Outcome:
    """D-33 的第 3、4 步：7 条前缀精确匹配 → 都不中就是指令列表（T01）。"""
    text = strip_mentions(inbound.text, inbound.mentions).strip()

    if text.startswith("作业书"):
        return _assignment(inbound, state, now)
    if text.startswith("拆解"):
        return Outcome(
            replies=(reply(inbound, replies.DECOMPOSING if has_rubric else replies.NEEDS_RUBRIC),),
            pipeline="decompose" if has_rubric else "",
        )
    if text.startswith("方向"):
        # M2：群里 = 起后台生成候选 + 开投票窗口；私聊 = 指出"去群里发"（§2.2）。
        # 前置缺失（没评分点 / 没花名册）都在 vote.command() 里判，**都不起 pipeline**。
        return vote.command(inbound, state, roster, has_rubric=has_rubric, now=now)
    if text.startswith("你想做哪一块"):
        return preference.command(
            inbound, state, cards, roster, preferences, now, source_title=source_title
        )
    if any(text.startswith(prefix) for prefix in PROPOSAL_PREFIXES):
        return _proposal(text, inbound, state, now)
    if COMPLETE_PATTERN.match(text):
        return Outcome(replies=(reply(inbound, replies.PLACEHOLDER_COMPLETE),))
    if text.startswith("登记"):
        return register.register_begin(inbound, state, now)

    return Outcome(replies=(reply(inbound, replies.COMMAND_LIST_TEXT),))


def _merge(outcome: Outcome, state: dict, original: dict) -> Outcome:
    """前缀分支的结果里补上"状态被 M4 清理过"这件事。

    过期的志愿窗口清掉 ``awaiting`` 之后，这条消息本身可能是「拆解」这种不写状态的
    指令 —— 那个 Outcome 的 ``state`` 是 ``None``。不合并的话，清掉的 ``awaiting``
    就落不了盘，残留窗口会一直挂着（下次读还是过期、还是要再判一遍）。
    """
    if state is original or outcome.state is not None:
        return outcome
    return replace(outcome, state=state)


def _proposal(text: str, inbound: Inbound, state: dict, now: datetime | None = None) -> Outcome:
    """M5 匿名代言（§6.5 / D-55）：私聊提议 → 落盘留痕 → 群里匿名**原样**转达。

    正文只 strip 两端，不修改、不总结、不加工（B2 / §6.5）。原来带的 @ 段会被
    ``strip_mentions`` 一起剥掉 —— 与其它指令一致，也免得把 ``@_user_2`` 这种
    内部占位符泄露到群里。
    """
    content = ""
    for prefix in PROPOSAL_PREFIXES:
        if text.startswith(prefix):
            content = text[len(prefix):].strip()
            break
    if not content:
        return Outcome(replies=(reply(inbound, replies.PROPOSAL_EMPTY),))

    group = (state or {}).get("group_chat_id") or ""
    if not group:
        # 发不到群就别假装发了：不转达、也不落盘（留痕是给"已发布的内容"追责用的）
        return Outcome(replies=(reply(inbound, replies.NEED_GROUP),))

    return Outcome(
        replies=(
            Reply(chat_id=group, text=replies.PROPOSAL_POSTED.format(text=content)),
            reply(inbound, replies.PROPOSAL_ACK),
        ),
        save_proposal={
            "user_id": inbound.sender_open_id,
            "text": content,
            "created_at": (now or datetime.now()).isoformat(timespec="seconds"),
        },
    )


def remember_file(inbound: Inbound, state: dict, now: datetime | None = None) -> Outcome:
    """把 file_key 存进 ``state.pending_file``，等文字消息来配对（D-42）。

    只缓存、不下载：下载是 I/O，归 app 层；router 连"要不要下载"都不决定。
    """
    name = inbound.file_name or "（未命名文件）"
    pending = {
        "file_key": inbound.file_key,
        "file_name": inbound.file_name,
        "resource_type": "file",          # 当前只缓存文件；字段集见 D-45
        "chat_id": inbound.chat_id,
        "message_id": inbound.message_id,
        "received_at": (now or datetime.now()).isoformat(timespec="seconds"),
    }
    return Outcome(
        replies=(reply(inbound, replies.FILE_RECEIVED.format(name=name)),),
        state={**(state or {}), "pending_file": pending},
    )


def _assignment(inbound: Inbound, state: dict, now: datetime | None = None) -> Outcome:
    if not _pending_file(state, now, inbound=inbound):
        return Outcome(replies=(reply(inbound, replies.FILE_MISSING),))
    return Outcome(replies=(reply(inbound, replies.PARSING),), pipeline="assignment")


def _pending_file(
    state: dict, now: datetime | None = None, *, inbound: Inbound | None = None
) -> dict:
    """有没有**可用**的缓存文件 —— 唯一的入口，有效期（D-46）与会话（D-47）都在这里判。

    过期、或不是**这个会话**发的文件，都当没有：调用方自然回既有的 ``FILE_MISSING``，
    ``pipeline`` 也不会起。

    ``inbound`` 必须是 keyword-only：``now`` 是第 2 个位置参数，写成位置参数的话
    ``_pending_file(state, now, inbound)`` 会把 inbound 静默塞进 now —— 不报错，
    只是功能失效，极难查。
    """
    if not isinstance(state, dict):
        return {}
    pending = state.get("pending_file") or {}
    if not pending or _stale_pending(pending, now):
        return {}
    # 别的会话发的文件不算数：私聊里每人一个 chat_id，全局单份会被互相顶掉（D-42 ④
    # 定的演示方式就是各自私聊投递，所以这是必现路径，不是边角）。
    # 字段缺省则放行（兼容旧 state，与 _stale_pending 同款防御）。
    if inbound is not None and pending.get("chat_id") and pending["chat_id"] != inbound.chat_id:
        return {}
    return pending


def _stale_pending(pending: dict, now: datetime | None = None) -> bool:
    raw = pending.get("received_at")
    if not raw:
        return False                      # 老 state 没有这个字段：不因此失效
    try:
        received = datetime.fromisoformat(str(raw))
    except ValueError:
        return False                      # 时间戳脏了就当没有 TTL，别因脏数据把文件丢掉
    return (now or datetime.now()) - received > PENDING_FILE_TTL
