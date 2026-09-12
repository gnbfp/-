"""M0 路由 —— 纯函数。**铁律：本文件不许 import lark_oapi**（方案 §1）。

依据：`requirements.md` §7.1（D-33「先剥 @段 → 再看状态 → 最后认前缀」+ 7 条前缀）、
D-42（文字与附件必然是两条消息）、M0 网关方案 §4 / §5 / §6 / §7。

进出都是纯数据（``Inbound`` / ``dict`` / ``Outcome``）：不联网、不发消息、不读文件，
所以这一整套规则可以在没有飞书、没有网络的情况下全量单测。

两条文档没写死、按方案 §4 的工程默认（可推翻）落地：
  * 「我想提议」全角 ``：`` 与半角 ``:`` 都认（中文输入法容易出半角）；
  * 「完成 T3」用 ``^完成\\s*[Tt]\\d+``，容忍空格与大小写。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Sequence

from src.gateway import register, replies
from src.gateway.events import Inbound, Mention, Outcome, reply

__all__ = [
    "PROPOSAL_PREFIXES",
    "COMPLETE_PATTERN",
    "strip_mentions",
    "route",
    "remember_file",
    "pipeline_kind",
]

# 7 条前缀里两条带变体：提议的冒号全半角、完成的 Tn 容忍空格与大小写。
PROPOSAL_PREFIXES = ("我想提议：", "我想提议:")
COMPLETE_PATTERN = re.compile(r"^完成\s*[Tt]\d+")

_MENTION_PLACEHOLDER = re.compile(r"@_user_\d+")
_RESOURCE_TYPES = ("file", "image")


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
    now: datetime | None = None,
) -> Outcome:
    """一条消息 → 一个 Outcome。顺序严格按 D-33：剥 @段 → 状态 → 前缀 → 兜底。

    ``roster`` 预留给 M2（投票分母）/ M5（组长身份）；M0 的登记不依赖它。
    ``has_rubric`` 由 app 层从 ``data/rubric.json`` 读出来传进来 —— router 自己不读文件，
    但仍然能对「拆解」给出正确回复（没有评分点 vs 重跑）。
    """
    # 0. 机器人自己的消息：权限里开了 include_bot，这条不丢就会自己回自己
    if inbound.sender_type == "app":
        return Outcome()

    # 1. 文件 / 图片：只缓存，不干活（D-42：文字和附件必然是两条消息）
    if inbound.message_type in _RESOURCE_TYPES:
        return remember_file(inbound, state, now)
    if inbound.message_type != "text":
        return Outcome()

    text = strip_mentions(inbound.text, inbound.mentions).strip()
    if not text:
        return Outcome()                       # 纯 @ 段 / 空文本：静默，不刷屏

    # 2. 状态优先：裸数字/表单怎么解释，全看 state.json 的 awaiting
    awaiting = (state or {}).get("awaiting")
    if awaiting == "register":
        return register.register_step(text, inbound, state, now)
    if awaiting == "vote":
        return Outcome(replies=(reply(inbound, replies.PLACEHOLDER_VOTE),))
    if awaiting == "preference":
        return Outcome(replies=(reply(inbound, replies.PLACEHOLDER_PREFERENCE),))

    # 3. 前缀精确匹配（7 条，无重叠）
    if text.startswith("作业书"):
        return _assignment(inbound, state)
    if text.startswith("拆解"):
        return Outcome(
            replies=(reply(inbound, replies.DECOMPOSING if has_rubric else replies.NEEDS_RUBRIC),)
        )
    if text.startswith("方向"):
        return Outcome(replies=(reply(inbound, replies.PLACEHOLDER_DIRECTION),))
    if text.startswith("你想做哪一块"):
        return Outcome(replies=(reply(inbound, replies.PLACEHOLDER_PREFERENCE),))
    if any(text.startswith(prefix) for prefix in PROPOSAL_PREFIXES):
        return Outcome(replies=(reply(inbound, replies.PLACEHOLDER_PROPOSAL),))
    if COMPLETE_PATTERN.match(text):
        return Outcome(replies=(reply(inbound, replies.PLACEHOLDER_COMPLETE),))
    if text.startswith("登记"):
        return register.register_begin(inbound, state, now)

    # 4. 兜底：指令列表（T01）
    return Outcome(replies=(reply(inbound, replies.COMMAND_LIST_TEXT),))


def remember_file(inbound: Inbound, state: dict, now: datetime | None = None) -> Outcome:
    """把 file_key 存进 ``state.pending_file``，等文字消息来配对（D-42）。

    只缓存、不下载：下载是 I/O，归 app 层；router 连"要不要下载"都不决定。
    """
    name = inbound.file_name or "（未命名文件）"
    pending = {
        "file_key": inbound.file_key,
        "file_name": inbound.file_name,
        "chat_id": inbound.chat_id,
        "message_id": inbound.message_id,
        "received_at": (now or datetime.now()).isoformat(timespec="seconds"),
    }
    return Outcome(
        replies=(reply(inbound, replies.FILE_RECEIVED.format(name=name)),),
        state={**(state or {}), "pending_file": pending},
    )


def pipeline_kind(inbound: Inbound, state: dict, has_rubric: bool = False) -> str:
    """这条消息要不要起后台重活？返回 ``"assignment"`` / ``"decompose"`` / ``""``。

    对应方案 §6 的 ``needs_m1_m3()``；把「拆解」那条重跑也并进来，app 层只判一次。
    """
    if inbound.sender_type == "app" or inbound.message_type != "text":
        return ""
    text = strip_mentions(inbound.text, inbound.mentions).strip()
    if text.startswith("作业书"):
        return "assignment" if _pending_file(state) else ""
    if text.startswith("拆解"):
        return "decompose" if has_rubric else ""
    return ""


def _assignment(inbound: Inbound, state: dict) -> Outcome:
    if not _pending_file(state):
        return Outcome(replies=(reply(inbound, replies.FILE_MISSING),))
    return Outcome(replies=(reply(inbound, replies.PARSING),))


def _pending_file(state: dict) -> dict:
    return ((state or {}).get("pending_file") or {}) if isinstance(state, dict) else {}