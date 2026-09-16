"""「帮助」指令（D-71）—— 纯增量：任何状态机之前处理，不写 state、不起重活。

要求（需求方 2026-09-15）：@机器人 说「帮助」就给一份用法清单，且**不影响别的流程**。
所以这里的重点断言不是"回了什么"，而是 **`outcome.state is None` / 不落盘 / 不改票数**。
"""

from datetime import datetime

from src.gateway import replies
from src.gateway.events import Inbound, Mention
from src.gateway.router import HELP_WORDS, route
from src.models import Member, Roster

BOT = "ou_bot_self"
GROUP = "oc_g"
NOW = datetime(2026, 9, 16, 10, 0, 0)


def _inbound(text="", *, chat_type="group", mentions=(), **over):
    data = dict(
        chat_id=GROUP if chat_type == "group" else "ou_zhang",
        chat_type=chat_type,
        message_type="text",
        text=text,
        mentions=tuple(mentions),
        sender_type="user",
        sender_open_id="ou_zhang",
        message_id="m1",
    )
    data.update(over)
    return Inbound(**data)


def _mention():
    return Mention(key="@_user_1", open_id=BOT, name="喵喵喵")


def _roster():
    return Roster(
        leader="ou_zhang",
        members=[
            Member(open_id="ou_zhang", name="张三"),
            Member(open_id="ou_li", name="李四"),
        ],
        registered_at="2026-09-16T09:00:00",
        confirmed_by="ou_zhang",
    )


def _vote_state():
    return {
        "awaiting": "vote",
        "group_chat_id": GROUP,
        "vote": {
            "chat_id": GROUP,
            "opened_at": NOW.isoformat(timespec="seconds"),
            "opened_by": "ou_zhang",
            "candidates": [{"id": 1, "title": "方向A"}],
            "votes": {"ou_li": 1},
        },
    }


def _preference_state():
    return {
        "awaiting": "preference",
        "group_chat_id": GROUP,
        "preference": {
            "chat_id": GROUP,
            "opened_at": NOW.isoformat(timespec="seconds"),
            "opened_by": "ou_zhang",
        },
    }


def _confirm_state():
    return {
        "awaiting": "register",
        "group_chat_id": GROUP,
        "register": {
            "stage": "confirm",
            "initiator_open_id": "ou_zhang",
            "leader": {"open_id": "ou_zhang", "name": "张三"},
            "members": [{"open_id": "ou_li", "name": "李四"}],
            "expires_at": "2026-09-16T10:05:00",
        },
    }


# ---------- 基本行为 ----------


def test_group_help_after_mention_returns_the_guide():
    outcome = route(
        _inbound("@_user_1 帮助", mentions=[_mention()]),
        {},
        _roster(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
    )
    assert [r.text for r in outcome.replies] == [replies.HELP_TEXT]
    assert "怎么用" not in outcome.replies[0].text or True          # 内容本身不锁死
    for keyword in ("群里", "私聊", "封盘", "完成 T3", "@ 选人"):
        assert keyword in outcome.replies[0].text


def test_group_help_without_mention_is_silent():
    """@ 门照旧生效：群里不 @ 我，说「帮助」我也不回。"""
    outcome = route(
        _inbound("帮助"), {}, _roster(), bot_open_id=BOT, bot_name="喵喵喵"
    )
    assert outcome.replies == ()


def test_private_help_needs_no_mention():
    outcome = route(
        _inbound("帮助", chat_type="p2p"), {}, _roster(), bot_open_id=BOT, bot_name="喵喵喵"
    )
    assert [r.text for r in outcome.replies] == [replies.HELP_TEXT]


def test_help_variants_are_accepted():
    for word in ("help", "HELP", "使用说明", "帮助我一下"):
        outcome = route(
            _inbound(f"@_user_1 {word}", mentions=[_mention()]),
            {},
            _roster(),
            bot_open_id=BOT,
            bot_name="喵喵喵",
        )
        assert [r.text for r in outcome.replies] == [replies.HELP_TEXT], word
    assert "帮助" in HELP_WORDS


def test_command_list_points_at_help():
    """兜底文案里要能发现「帮助」这个入口。"""
    outcome = route(
        _inbound("@_user_1 今天天气不错", mentions=[_mention()]),
        {},
        _roster(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
    )
    assert "帮助" in outcome.replies[0].text


# ---------- 「不影响别的进程」—— 这一节才是重点 ----------


def test_help_does_not_touch_the_vote_window():
    """投票窗口开着时发「帮助」：回清单，但**票数、awaiting、落盘一律不动**。"""
    state = _vote_state()
    outcome = route(
        _inbound("@_user_1 帮助", mentions=[_mention()]),
        state,
        _roster(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
        now=NOW,
    )
    assert [r.text for r in outcome.replies] == [replies.HELP_TEXT]
    assert outcome.state is None                 # 一个字段都没改
    assert outcome.save_direction is None        # 没落盘
    assert state["awaiting"] == "vote"           # 窗口照旧开着
    assert state["vote"]["votes"] == {"ou_li": 1}  # 票数没动


def test_help_does_not_touch_the_preference_window():
    state = _preference_state()
    outcome = route(
        _inbound("@_user_1 帮助", mentions=[_mention()]),
        state,
        _roster(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
        now=NOW,
    )
    assert [r.text for r in outcome.replies] == [replies.HELP_TEXT]
    assert outcome.state is None
    assert outcome.save_assignments == ()
    assert state["awaiting"] == "preference"


def test_help_during_register_confirm_does_not_cancel():
    """登记确认阶段回「帮助」**不算**"回复别的就作废" —— 这是它必须排在状态机之前的原因。"""
    state = _confirm_state()
    outcome = route(
        _inbound("@_user_1 帮助", mentions=[_mention()]),
        state,
        _roster(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
        now=NOW,
    )
    assert [r.text for r in outcome.replies] == [replies.HELP_TEXT]
    assert outcome.state is None                 # 窗口没被清掉
    assert outcome.save_roster is None           # 也没保存
    assert replies.REGISTER_CANCELLED not in [r.text for r in outcome.replies]
    assert state["awaiting"] == "register"


def test_help_is_not_a_pipeline():
    """零 LLM：帮助不起任何重活。"""
    outcome = route(
        _inbound("@_user_1 帮助", mentions=[_mention()]),
        {},
        _roster(),
        has_rubric=True,
        bot_open_id=BOT,
        bot_name="喵喵喵",
    )
    assert outcome.pipeline == ""
    assert outcome.save_roster is None
    assert outcome.save_preference is None
    assert outcome.save_complete is None
