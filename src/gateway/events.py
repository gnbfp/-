"""M0 的数据结构 —— 纯 dataclass，零依赖。

依据：M0 网关方案 §2 / §3 / §5、`requirements.md` §7.1（D-33）/ §7.7、D-42。

铁律：本文件**不 import lark_oapi，也不 import 任何项目内模块**。
连接层负责把飞书事件翻译成 ``Inbound``（``to_inbound()``），路由层只吃 ``Inbound``、
只吐 ``Outcome`` —— 这样 §7.1 的整套路由规则可以在没有飞书、没有网络的情况下全量单测。

``to_inbound()`` 放在这里而不是 client.py，就是为了让它也能被单测：它只做
``getattr`` 取值，不碰 lark 的对象类型，用假事件对象即可覆盖（含 @ 段、file_key 提取）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = ["Mention", "Inbound", "Reply", "Outcome", "reply", "to_inbound"]


# 消息类型 -> 内容里的资源字段名。与 tools/probe_feishu.py 的实测口径一致。
_RESOURCE_FIELD = {
    "file": "file_key",
    "image": "image_key",
    "audio": "file_key",
    "media": "file_key",
    "video": "file_key",
    "sticker": "file_key",
}


@dataclass(frozen=True)
class Mention:
    """被 @ 到的人。``key`` 是文本里的占位符（如 ``@_user_1``），剥 @段靠它。"""

    key: str = ""
    open_id: str = ""
    name: str = ""


@dataclass(frozen=True)
class Inbound:
    """一条进来的消息。``text`` 是**原文**（含 @ 段），router 内部再剥。"""

    chat_id: str
    chat_type: str = ""            # "p2p" | "group"
    message_type: str = ""         # "text" | "file" | "image" | ...
    text: str = ""
    mentions: tuple[Mention, ...] = ()
    sender_open_id: str = ""
    sender_type: str = ""          # "user" | "app"
    message_id: str = ""
    file_key: str = ""
    file_name: str = ""


@dataclass(frozen=True)
class Reply:
    """一条要发出去的纯文本消息。

    ``receive_id_type``：M0 只回"来的那个会话"（``chat_id``）；M4/M5 要**主动发到群**
    （chat_id）或**私聊某个人**（open_id），所以这一条得能表达"发给谁"（D-54 / D-55）。
    """

    chat_id: str
    text: str
    receive_id_type: str = "chat_id"        # "chat_id" | "open_id"


@dataclass(frozen=True)
class Outcome:
    """路由结果 —— 全是数据，router 自己不做任何 I/O。

    ``state``：完整的新 state；``None`` = 不改。
    ``download_file_key``：方案 §3 的字段。当前实现用
      ``state.pending_file`` 传文件（方案 §7 的口径：先缓存、再配对），
      所以它暂时没人设；保留是为了不偏离 §3 的接口。
    ``save_roster``：非空 = 登记流程确认通过，app 层把它落 ``data/members.json``。
      方案 §3 的 Outcome 只有前三个字段，这里多一个的原因：名单内容是状态机在
      ``register.py`` 里解析出来的，让 app 层"再推一遍"等于把判定逻辑复制一份
      （违反"判定只有一处"）。多这一个纯数据字段，状态机仍然只有一个出口。
    ``pipeline``：非空 = app 层要起后台重活（``"assignment"`` / ``"decompose"``）。
      由 ``route()`` 一次算出，app 层只读不判 —— 否则「回什么话」与「起不起重活」
      会各判一遍，给出互相矛盾的结果（外审必修 4：状态窗口吃掉指令却照样烧 LLM）。

    下面三个是 M4 / M5 的落盘请求（都只是**数据**，写盘归 app 层），形状照 ``save_roster``：
      * ``save_preference``：一条志愿（按 ``user_id`` 覆盖写，M4 收志愿）；
      * ``save_assignments``：整份分配结果（M4 结算，一次性覆盖）；
      * ``save_proposal``：一条匿名提议（M5，追加写，含真实 ``user_id`` 留痕）。
    """

    replies: tuple[Reply, ...] = ()
    state: dict | None = None
    download_file_key: str = ""
    save_roster: dict | None = None
    pipeline: str = ""
    save_preference: dict | None = None
    save_assignments: tuple[dict, ...] = ()
    save_proposal: dict | None = None


def reply(inbound: Inbound, text: str) -> Reply:
    """按"回到哪条消息来的会话"造一条回复。"""
    return Reply(chat_id=inbound.chat_id, text=text)


def to_inbound(data) -> Inbound:
    """飞书事件对象 → ``Inbound``（只取字段，不依赖 lark 的类型）。"""
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)

    message_type = getattr(message, "message_type", "") or ""
    payload = _parse_content(getattr(message, "content", "") or "")

    file_key = ""
    field_name = _RESOURCE_FIELD.get(message_type)
    if field_name:
        file_key = str(payload.get(field_name) or "")

    return Inbound(
        chat_id=getattr(message, "chat_id", "") or "",
        chat_type=getattr(message, "chat_type", "") or "",
        message_type=message_type,
        text=str(payload.get("text") or ""),
        mentions=tuple(
            Mention(
                key=getattr(mention, "key", "") or "",
                open_id=getattr(getattr(mention, "id", None), "open_id", "") or "",
                name=getattr(mention, "name", "") or "",
            )
            for mention in (getattr(message, "mentions", None) or [])
        ),
        sender_open_id=getattr(getattr(sender, "sender_id", None), "open_id", "") or "",
        sender_type=getattr(sender, "sender_type", "") or "",
        message_id=getattr(message, "message_id", "") or "",
        file_key=file_key,
        file_name=str(payload.get("file_name") or ""),
    )


def _parse_content(raw) -> dict:
    try:
        parsed = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
