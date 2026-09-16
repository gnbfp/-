"""F1 加固的回归（2026-09-16）：@ 门不能被"表单形状"绕过 + M1/M3 的身份闸。

两条都是**实测复现过**的越权，各配一条回归；另外守住"正常路径没被堵死"和
"有意留下的口径"（群里 + 还没登记 → 放行），免得以后被当成 bug 改掉。

不碰飞书、不碰网络、不碰 `data/`。
"""

from datetime import datetime

from src.gateway import replies
from src.gateway.events import Inbound
from src.gateway.router import route
from src.models import Member, Roster

BOT = "ou_bot_self"
BOT_NAME = "喵喵喵"
GROUP = "oc_group"
NOW = datetime(2026, 9, 16, 11, 0)


def _roster():
    return Roster(
        leader="ou_leader",
        members=[
            Member(open_id="ou_leader", name="组长"),
            Member(open_id="ou_a", name="组员甲"),
            Member(open_id="ou_b", name="组员乙"),
        ],
        registered_at="2026-09-16T09:00:00",
        confirmed_by="ou_leader",
    )


def _inbound(text="", *, chat_type="group", chat_id="c1", sender="ou_leader",
             mentions=(), message_id="m1", **over):
    return Inbound(
        chat_id=chat_id,
        chat_type=chat_type,
        message_type="text",
        text=text,
        mentions=tuple(mentions),
        sender_type="user",
        sender_open_id=sender,
        message_id=message_id,
        **over,
    )


def _mention(open_id="ou_bot_self", name="喵喵喵", key="@_user_1"):
    from src.gateway.events import Mention

    return Mention(key=key, open_id=open_id, name=name)


def _member_mentions():
    """「组员：@甲 @乙」那一行里的两个 @（**都不是机器人**）。"""
    from src.gateway.events import Mention

    return (
        Mention(key="@_user_2", open_id="ou_a", name="组员甲"),
        Mention(key="@_user_3", open_id="ou_b", name="组员乙"),
    )


def _route(inbound, state=None, roster=None, **kw):
    kw.setdefault("bot_open_id", BOT)
    kw.setdefault("bot_name", BOT_NAME)
    kw.setdefault("has_rubric", True)
    kw.setdefault("now", NOW)
    return route(inbound, state if state is not None else {}, roster, **kw)


def _texts(outcome):
    return [r.text for r in outcome.replies]


# ---------- F1①：@ 门不能被"表单形状"绕过 ----------


def test_form_shaped_chatter_does_not_start_m3_without_a_bot_mention():
    """实测过的绕过：`拆解\\n组员：@甲 @乙 你们看看` 没 @ 机器人，以前会起 M3 流水线。"""
    outcome = _route(
        _inbound("拆解\n组员：@_user_2 @_user_3 你们看看这个", mentions=_member_mentions()),
        {},
        _roster(),
    )
    assert outcome.replies == ()          # 静默丢弃
    assert outcome.pipeline == ""          # 不起重活


def test_form_shaped_chatter_does_not_start_m2_without_a_bot_mention():
    """同一条绕过换「方向」（M2）：以前会起 direction 流水线 + 开投票窗口。"""
    outcome = _route(
        _inbound("方向 我觉得可以这样\n组员：@_user_2 你看呢", mentions=_member_mentions()),
        {},
        _roster(),
    )
    assert outcome.replies == ()
    assert outcome.pipeline == ""


def test_plain_chatter_without_a_bot_mention_is_still_dropped():
    """对照组：不带「组员：」行的普通聊天本来就被挡住（这条一直是好的）。"""
    outcome = _route(_inbound("拆解"), {}, _roster())
    assert outcome.replies == ()
    assert outcome.pipeline == ""


def test_register_form_inside_the_register_window_still_passes():
    """不能把登记流程堵死：**窗口内**的表单仍然豁免 @ 门（这是原有的有意例外）。"""
    state = {
        "awaiting": "register",
        "register": {
            "stage": "collect",
            "initiator_open_id": "ou_leader",
            "leader": None,
            "members": [],
            "expires_at": "2026-09-16T11:05:00",
        },
    }
    outcome = _route(
        _inbound("登记\n组长：@_user_2\n组员：@_user_3", mentions=_member_mentions()), state, _roster()
    )
    assert outcome.replies or outcome.state       # 被放行：留下了痕迹，不是"静默丢弃"


# ---------- F1②：M1 / M3 的身份闸 ----------


def test_stranger_cannot_start_m1_from_a_private_chat():
    """实测过的越权：陌生人私聊「作业书」两步就起了 `pipeline="assignment"`（整份覆盖全组产物）。"""
    pending = {"file_key": "fk_1", "chat_id": "oc_dm_stranger", "message_id": "m0",
               "received_at": NOW.isoformat(timespec="seconds")}
    outcome = _route(
        _inbound("作业书", chat_type="p2p", chat_id="oc_dm_stranger", sender="ou_stranger"),
        {"pending_file": pending},
        _roster(),
    )
    assert _texts(outcome) == [replies.MAIN_CHAIN_NOT_MEMBER]
    assert outcome.pipeline == ""


def test_stranger_cannot_start_m3_by_mentioning_the_bot_in_the_group():
    """群里 @了机器人、但人不在花名册上 —— 「拆解」一样拒（实测这条以前是放行的）。"""
    outcome = _route(
        _inbound("@_user_1 拆解", sender="ou_stranger", mentions=(_mention(),)),
        {},
        _roster(),
    )
    assert _texts(outcome) == [replies.MAIN_CHAIN_NOT_MEMBER]
    assert outcome.pipeline == ""


def test_member_can_still_start_m1_from_a_private_chat():
    """不能把正常路径堵死：D-42 ④ 的演示方式就是各自私聊投递作业书。

    ``has_rubric=False`` = 盘上还没有产物（第一份作业书）⇒ 直接起主链路。
    盘上**已有**产物时走的是"先确认再换"（2026-09-16 口径），见 ``test_reset.py``。
    """
    pending = {"file_key": "fk_1", "chat_id": "oc_dm_a", "message_id": "m0",
               "received_at": NOW.isoformat(timespec="seconds")}
    outcome = _route(
        _inbound("作业书", chat_type="p2p", chat_id="oc_dm_a", sender="ou_a"),
        {"pending_file": pending},
        _roster(),
        has_rubric=False,
        cards=(),
    )
    assert _texts(outcome) == [replies.PARSING]
    assert outcome.pipeline == "assignment"


def test_member_can_still_start_m3_in_the_group():
    outcome = _route(
        _inbound("@_user_1 拆解", sender="ou_a", mentions=(_mention(),)), {}, _roster()
    )
    assert _texts(outcome) == [replies.DECOMPOSING]
    assert outcome.pipeline == "decompose"


def test_private_chat_without_a_roster_points_at_register():
    """私聊里没有"群"这层边界 ⇒ 没名册也拒，并指路「登记」。"""
    outcome = _route(
        _inbound("作业书", chat_type="p2p", chat_id="oc_dm_x", sender="ou_x"), {}, None
    )
    assert _texts(outcome) == [replies.MAIN_CHAIN_NEED_REGISTER]
    assert outcome.pipeline == ""


def test_group_without_a_roster_is_deliberately_allowed():
    """**有意的口径**（与 M5「roster 为空 = 谁都算数」同款）：还没登记时，群里放行。

    「登记」本身也是在群里做的 —— 群就是这时的身份边界。别把这条当 bug 改掉：
    堵死它就得给所有"在群里发作业书"的场景先补一次登记。
    """
    outcome = _route(_inbound("@_user_1 拆解", mentions=(_mention(),)), {}, None)
    assert _texts(outcome) == [replies.DECOMPOSING]
    assert outcome.pipeline == "decompose"
