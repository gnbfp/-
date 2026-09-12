"""「登记」状态机单测（§7.7 / D-34，方案 §8）。"""

from datetime import datetime, timedelta

from src.gateway import replies
from src.gateway.events import Inbound, Mention
from src.gateway.register import REGISTER_TTL, register_begin, register_step
from src.models import Roster

NOW = datetime(2026, 9, 12, 13, 30, 0)
FORM = "登记\n组长：@_user_1\n组员：@_user_2 @_user_3"


def _inbound(text="", mentions=()):
    return Inbound(
        chat_id="c1",
        chat_type="group",
        message_type="text",
        text=text,
        mentions=tuple(mentions),
        sender_type="user",
        sender_open_id="ou_initiator",
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
