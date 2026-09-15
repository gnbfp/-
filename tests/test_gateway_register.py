"""「登记」状态机单测（§7.7 / D-34，方案 §8）。"""

from datetime import datetime, timedelta

from src.gateway import replies
from src.gateway.events import Inbound, Mention, Outcome
from src.gateway.register import (
    REGISTER_TTL,
    is_cancel,
    register_begin,
    register_cancel,
    register_step,
)
from src.models import Member, Roster

NOW = datetime(2026, 9, 12, 13, 30, 0)
FORM = "登记\n组长：@_user_1\n组员：@_user_2 @_user_3"


def _inbound(text="", mentions=(), sender_open_id="ou_initiator"):
    return Inbound(
        chat_id="c1",
        chat_type="group",
        message_type="text",
        text=text,
        mentions=tuple(mentions),
        sender_type="user",
        sender_open_id=sender_open_id,
        message_id="m1",
    )


def _mentions():
    return (
        Mention(key="@_user_1", open_id="ou_zhang", name="张三"),
        Mention(key="@_user_2", open_id="ou_li", name="李四"),
        Mention(key="@_user_3", open_id="ou_wang", name="王五"),
    )


def test_begin_returns_blank_form_and_waits_for_collect():
    outcome = register_begin(_inbound("登记"), {}, NOW)
    assert replies.REGISTER_FORM in outcome.replies[0].text
    assert outcome.state["awaiting"] == "register"
    assert outcome.state["register"]["stage"] == "collect"


def test_begin_starts_the_collect_ttl_and_remembers_the_initiator():
    """必修 1：collect 也要有 TTL —— 否则不填表就永久锁群。"""
    block = register_begin(_inbound("登记"), {}, NOW).state["register"]
    assert block["expires_at"] == (NOW + REGISTER_TTL).isoformat(timespec="seconds")
    assert block["initiator_open_id"] == "ou_initiator"


def test_register_in_private_chat_asks_for_the_group():
    """私聊发「登记」只回一句"去群里发"，**不开窗口**（D-69 附带口径）。

    登记靠 @ 人拿 open_id，私聊里 @ 不了人 ⇒ 开出来的窗口只会白占 `awaiting`
    5 分钟（期间群消息全走登记状态机），用户填了也没用。
    """
    private = Inbound(
        chat_id="ou_initiator",
        chat_type="p2p",
        message_type="text",
        text="登记",
        sender_type="user",
        sender_open_id="ou_initiator",
        message_id="m2",
    )
    outcome = register_begin(private, {}, NOW)
    assert [r.text for r in outcome.replies] == [replies.REGISTER_NEED_GROUP]
    assert outcome.state is None                   # 一个字都不许落盘


def test_collect_after_expiry_cancels():
    state = register_begin(_inbound("登记"), {}, NOW).state
    later = NOW + REGISTER_TTL + timedelta(seconds=1)
    outcome = register_step(FORM, _inbound(FORM, _mentions()), state, later)
    assert outcome.replies[0].text == replies.REGISTER_EXPIRED
    assert outcome.state["awaiting"] is None


def test_collect_ok_moves_to_confirm():
    state = register_begin(_inbound("登记"), {}, NOW).state
    outcome = register_step(FORM, _inbound(FORM, _mentions()), state, NOW)
    block = outcome.state["register"]
    assert block["stage"] == "confirm"
    assert block["leader"] == {"open_id": "ou_zhang", "name": "张三"}
    assert [m["open_id"] for m in block["members"]] == ["ou_li", "ou_wang"]
    assert "张三" in outcome.replies[0].text
    assert "共 3 人" in outcome.replies[0].text


def test_collect_without_any_mention_explains_at_syntax():
    state = register_begin(_inbound("登记"), {}, NOW).state
    outcome = register_step("登记\n组长：张三\n组员：李四 王五", _inbound(), state, NOW)
    assert outcome.replies[0].text == replies.REGISTER_FORM_BAD
    assert outcome.state is None


def test_collect_needs_exactly_one_leader():
    state = register_begin(_inbound("登记"), {}, NOW).state
    text = "组长：@_user_1 @_user_4\n组员：@_user_2 @_user_3"
    mentions = (*_mentions(), Mention(key="@_user_4", open_id="ou_zhao", name="赵六"))
    outcome = register_step(text, _inbound(text, mentions), state, NOW)
    assert outcome.replies[0].text == replies.REGISTER_NEED_LEADER


def test_collect_needs_at_least_two_members():
    state = register_begin(_inbound("登记"), {}, NOW).state
    text = "组长：@_user_1\n组员：@_user_2"
    outcome = register_step(text, _inbound(text, _mentions()), state, NOW)
    assert outcome.replies[0].text == replies.REGISTER_NEED_MEMBERS


def test_confirm_agree_produces_roster_payload():
    state = register_begin(_inbound("登记"), {}, NOW).state
    state = register_step(FORM, _inbound(FORM, _mentions()), state, NOW).state
    outcome = register_step("同意", _inbound("同意"), state, NOW)

    roster = Roster.from_dict(outcome.save_roster)
    roster.validate()                                  # 组长必须在 members 里
    assert roster.leader == "ou_zhang"
    assert [m.open_id for m in roster.members] == ["ou_zhang", "ou_li", "ou_wang"]
    assert roster.confirmed_by == "ou_initiator"
    assert outcome.state["awaiting"] is None
    assert outcome.state["register"] is None


def test_confirm_anything_else_cancels_without_saving():
    state = register_begin(_inbound("登记"), {}, NOW).state
    state = register_step(FORM, _inbound(FORM, _mentions()), state, NOW).state
    outcome = register_step("不同意", _inbound("不同意"), state, NOW)
    assert outcome.save_roster is None
    assert outcome.replies[0].text == replies.REGISTER_CANCELLED
    assert outcome.state["awaiting"] is None


def test_confirm_after_expiry_cancels():
    state = register_begin(_inbound("登记"), {}, NOW).state
    state = register_step(FORM, _inbound(FORM, _mentions()), state, NOW).state
    later = NOW + REGISTER_TTL + timedelta(seconds=1)
    outcome = register_step("同意", _inbound("同意"), state, later)
    assert outcome.save_roster is None
    assert outcome.replies[0].text == replies.REGISTER_EXPIRED
    assert outcome.state["awaiting"] is None


def test_confirm_just_before_expiry_still_saves():
    state = register_begin(_inbound("登记"), {}, NOW).state
    state = register_step(FORM, _inbound(FORM, _mentions()), state, NOW).state
    nearly = NOW + REGISTER_TTL - timedelta(seconds=1)
    assert register_step("同意", _inbound("同意"), state, nearly).save_roster is not None


# ---------- 只有发起人能推进（必修 2）----------


def test_stranger_cannot_advance_the_form():
    """旁人在 collect 阶段说话 → 静默，不推进、也不回一长串表单刷屏。"""
    state = register_begin(_inbound("登记"), {}, NOW).state
    form = _inbound(FORM, _mentions(), sender_open_id="ou_stranger")
    assert register_step(FORM, form, state, NOW) == Outcome()


def test_stranger_cannot_cancel_the_pending_registration():
    """confirm 阶段旁人说一句「好」不该把登记作废。"""
    state = register_begin(_inbound("登记"), {}, NOW).state
    state = register_step(FORM, _inbound(FORM, _mentions()), state, NOW).state
    stranger = _inbound("好", sender_open_id="ou_stranger")
    assert register_step("好", stranger, state, NOW) == Outcome()


def test_expired_confirm_is_cleared_by_anyone_who_speaks():
    """过期判在发起人之前：confirm 过期后谁说话都该把它清掉。

    反过来的话，发起人不再开口时旁人的消息会被静默吞掉、awaiting 永远卡在 register。
    """
    state = register_begin(_inbound("登记"), {}, NOW).state
    state = register_step(FORM, _inbound(FORM, _mentions()), state, NOW).state
    later = NOW + REGISTER_TTL + timedelta(seconds=1)
    stranger = _inbound("作业书", sender_open_id="ou_stranger")

    outcome = register_step("作业书", stranger, state, later)

    assert outcome.replies[0].text == replies.REGISTER_EXPIRED
    assert outcome.state["awaiting"] is None
    assert outcome.state["register"] is None


def test_stranger_cannot_confirm_the_roster():
    """文档 §7.7 写的是「组长回「同意」」：旁人同意也不落盘。"""
    state = register_begin(_inbound("登记"), {}, NOW).state
    state = register_step(FORM, _inbound(FORM, _mentions()), state, NOW).state
    stranger = _inbound("同意", sender_open_id="ou_stranger")
    assert register_step("同意", stranger, state, NOW).save_roster is None
    assert register_step("同意", stranger, state, NOW) == Outcome()


# ---------- 逃生词（必修 1）----------


def test_cancel_words_are_recognised():
    assert is_cancel("取消登记")
    assert is_cancel(" 取消 ")
    assert not is_cancel("取消一下")
    assert not is_cancel("")


def test_initiator_can_cancel_the_window():
    state = register_begin(_inbound("登记"), {}, NOW).state
    outcome = register_cancel(_inbound("取消登记"), state, NOW)
    assert outcome.replies[0].text == replies.REGISTER_CANCELLED
    assert outcome.state["awaiting"] is None


def test_stranger_cannot_cancel_the_window():
    state = register_begin(_inbound("登记"), {}, NOW).state
    stranger = _inbound("取消登记", sender_open_id="ou_stranger")
    assert register_cancel(stranger, state, NOW) == Outcome()


def test_escape_word_also_clears_an_expired_window():
    """过期窗口里逃生词谁先说都只是清残留状态（与 register_step 同一条理）。"""
    state = register_begin(_inbound("登记"), {}, NOW).state
    later = NOW + REGISTER_TTL + timedelta(seconds=1)
    stranger = _inbound("取消登记", sender_open_id="ou_stranger")

    outcome = register_cancel(stranger, state, later)

    assert outcome.replies[0].text == replies.REGISTER_EXPIRED
    assert outcome.state["awaiting"] is None

def _led_roster(leader):
    return Roster(
        leader=leader,
        members=[Member(open_id=leader, name="组长"), Member(open_id="ou_other", name="组员")],
        registered_at="2026-09-12T09:00:00",
        confirmed_by=leader,
    )


def test_rebegin_by_non_leader_is_rejected():
    """F1：非组长重登记不能夺权 —— 不回表单、也不改状态。"""
    outcome = register_begin(_inbound("登记"), {}, NOW, _led_roster("ou_zhang"))
    assert outcome.replies[0].text == replies.REGISTER_LEADER_ONLY
    assert outcome.state is None


def test_rebegin_by_leader_still_opens_the_form():
    """防回归：组长重登记照常（唯一的例外）。"""
    outcome = register_begin(_inbound("登记"), {}, NOW, _led_roster("ou_initiator"))
    assert outcome.state["awaiting"] == "register"
    assert outcome.state["register"]["stage"] == "collect"
