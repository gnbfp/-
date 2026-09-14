"""M4 志愿窗口状态机单测（D-52~D-54，方案 §6）。

全程离线：不碰飞书、不碰网络、不碰 LLM（B2：M4 零智能）。
"""

from datetime import datetime, timedelta

from src.gateway import replies
from src.gateway.events import Inbound, Outcome
from src.gateway.router import route
from src.models import Member, Preference, Roster, TaskCard

OPEN = datetime(2026, 9, 13, 9, 0, 0)
GROUP = "c_group"


def _inbound(text="", **over):
    data = dict(
        chat_id=GROUP,
        chat_type="group",
        message_type="text",
        text=text,
        sender_type="user",
        sender_open_id="ou_zhang",
        message_id="m1",
    )
    data.update(over)
    return Inbound(**data)


def _private(text, sender="ou_li"):
    return _inbound(text, chat_type="p2p", chat_id=sender, sender_open_id=sender)


def _cards():
    return [
        TaskCard(
            task_id=f"T{index}",
            module_name=f"模块{index}",
            rubric_refs=["R1"],
            effort_hours=1.0,
            deliverable="交付物",
            acceptance="验收标准",
        )
        for index in (1, 2, 3)
    ]


def _roster():
    return Roster(
        leader="ou_zhang",
        members=[
            Member(open_id=open_id, name=name)
            for open_id, name in (("ou_zhang", "张三"), ("ou_li", "李四"), ("ou_wang", "王五"))
        ],
        registered_at="2026-09-13T09:00:00",
        confirmed_by="ou_zhang",
    )


def _state(opened_at=OPEN, group=GROUP, awaiting="preference"):
    return {
        "awaiting": awaiting,
        "preference": {
            "opened_at": opened_at.isoformat(timespec="seconds"),
            "opened_by": "ou_zhang",
            "chat_id": group,
        },
        "group_chat_id": group,
    }


def _texts(outcome):
    return [r.text for r in outcome.replies]


# ---------- 开窗口（§2.1）----------


def test_group_command_opens_the_window_and_dms_everyone():
    outcome = route(_inbound("你想做哪一块"), {}, _roster(), cards=_cards(), now=OPEN)
    assert outcome.state["awaiting"] == "preference"
    assert outcome.state["preference"]["opened_at"] == OPEN.isoformat(timespec="seconds")
    assert outcome.pipeline == ""                    # M4 零重活

    group, *dms = outcome.replies
    assert (group.chat_id, group.receive_id_type) == (GROUP, "chat_id")
    assert "1. T1 模块1（1h）" in group.text
    assert [dm.chat_id for dm in dms] == ["ou_zhang", "ou_li", "ou_wang"]
    assert all(dm.receive_id_type == "open_id" for dm in dms)
    assert all(dm.text == group.text for dm in dms)


def test_p2p_command_only_returns_his_own_list_and_does_not_touch_the_window():
    outcome = route(
        _private("你想做哪一块"), {}, _roster(), cards=_cards(), now=OPEN
    )
    assert outcome.state is None
    assert len(outcome.replies) == 1
    assert outcome.replies[0].chat_id == "ou_li"
    assert outcome.pipeline == ""


def test_command_points_to_the_missing_prerequisite():
    assert _texts(route(_inbound("你想做哪一块"), {}, None, now=OPEN)) == [
        replies.PREFERENCE_NEED_CARDS
    ]
    assert _texts(route(_inbound("你想做哪一块"), {}, None, cards=_cards(), now=OPEN)) == [
        replies.PREFERENCE_NEED_ROSTER
    ]


# ---------- 收志愿（§2.2 / D-54）----------


def test_p2p_digits_become_a_preference():
    outcome = route(
        _private("2 1"), _state(), _roster(), cards=_cards(), preferences=[], now=OPEN
    )
    assert outcome.save_preference == {
        "user_id": "ou_li",
        "ranked_task_ids": ["T2", "T1"],
        "submitted_at": OPEN.isoformat(timespec="seconds"),
    }
    assert outcome.save_assignments == ()
    assert _texts(outcome) == [replies.PREFERENCE_SAVED.format(tasks="T2 → T1")]


def test_group_digits_are_not_preferences():
    outcome = route(
        _inbound("3"), _state(), _roster(), cards=_cards(), preferences=[], now=OPEN
    )
    assert outcome.save_preference is None
    assert _texts(outcome) == [replies.COMMAND_LIST_TEXT]      # 兜底文案，不是志愿提示


def test_open_window_does_not_eat_private_commands():
    """窗口长达 5 小时：私聊里的其它指令必须照常能用。"""
    common = dict(roster=_roster(), cards=_cards(), preferences=[], now=OPEN)
    assert _texts(route(_private("完成 T3"), _state(), **common)) == [
        replies.PLACEHOLDER_COMPLETE
    ]
    assert _texts(route(_private("作业书"), _state(), **common)) == [replies.FILE_MISSING]
    assert _texts(route(_private("我想提议：加个图表"), _state(), **common)) == [
        "有组员提议：加个图表",
        replies.PROPOSAL_ACK,
    ]
    assert _texts(route(_private("你好"), _state(), **common)) == [replies.COMMAND_LIST_TEXT]


def test_bad_numbers_are_rejected_without_writing_anything():
    for text in ("9", "0", "abc", "T2", "2 x"):
        outcome = route(
            _private(text), _state(), _roster(), cards=_cards(), preferences=[], now=OPEN
        )
        assert outcome.save_preference is None, text
        assert _texts(outcome) == [
            replies.PREFERENCE_BAD.format(tasks="T1、T2、T3")
        ], text


def test_empty_private_message_is_silent_and_writes_nothing():
    outcome = route(
        _private("   "), _state(), _roster(), cards=_cards(), preferences=[], now=OPEN
    )
    assert outcome == Outcome()


def test_digits_from_someone_outside_the_roster_get_no_fake_confirmation():
    outcome = route(
        _private("1", sender="ou_stranger"),
        _state(),
        _roster(),
        cards=_cards(),
        preferences=[],
        now=OPEN,
    )
    assert outcome.save_preference is None
    assert _texts(outcome) == [replies.PREFERENCE_NOT_MEMBER]


# ---------- 结算三条（§2.3 / D-52）----------


def test_settles_immediately_when_everyone_has_submitted():
    """第 1 条：全员填完 → 立刻封盘，不等满 5 小时。"""
    existing = [
        Preference("ou_zhang", ["T1"], "2026-09-13T09:01:00"),
        Preference("ou_li", ["T2"], "2026-09-13T09:02:00"),
    ]
    outcome = route(
        _private("3", sender="ou_wang"),
        _state(),
        _roster(),
        cards=_cards(),
        preferences=existing,
        now=OPEN,
    )
    assert outcome.save_preference["ranked_task_ids"] == ["T3"]
    assert outcome.state["awaiting"] is None
    assert [record["task_id"] for record in outcome.save_assignments] == ["T1", "T2", "T3"]
    assert _texts(outcome)[0] == replies.PREFERENCE_SAVED.format(tasks="T3")
    assert outcome.replies[-1].chat_id == GROUP                # 总表发群
    assert "分配总表" in outcome.replies[-1].text


def test_leader_resend_with_no_submissions_only_reshows_the_list():
    """P0-B ①：还没人交过志愿时，组长重发只是再发一遍清单，**不封盘**。"""
    outcome = route(
        _inbound("你想做哪一块"), _state(), _roster(), cards=_cards(), preferences=[], now=OPEN
    )
    assert outcome.state is None                               # 窗口不动
    assert outcome.save_assignments == ()
    assert "1. T1 模块1（1h）" in outcome.replies[0].text


def test_leader_resend_with_submissions_asks_for_confirmation():
    """P0-B ②：已有人交过 → 回确认语，窗口不动，等「封盘」。"""
    existing = [Preference("ou_li", ["T1"], "2026-09-13T09:01:00")]
    outcome = route(
        _inbound("你想做哪一块"),
        _state(),
        _roster(),
        cards=_cards(),
        preferences=existing,
        now=OPEN,
    )
    assert outcome.state is None                               # 窗口还在
    assert outcome.save_assignments == ()
    assert _texts(outcome) == [replies.PREFERENCE_CONFIRM_SEAL.format(done=1, missing=2)]


def test_leader_seal_word_settles_the_window():
    """P0-B ③：组长回「封盘」才结算 —— 总表发群、窗口清空。"""
    existing = [Preference("ou_li", ["T1"], "2026-09-13T09:01:00")]
    outcome = route(
        _inbound("封盘"), _state(), _roster(), cards=_cards(), preferences=existing, now=OPEN
    )
    assert outcome.state["awaiting"] is None
    assert [record["task_id"] for record in outcome.save_assignments] == ["T1", "T2", "T3"]
    assert outcome.replies[0].chat_id == GROUP
    assert "分配总表" in outcome.replies[0].text
    assert "未交志愿：张三、王五（他们的卡为兜底）" in outcome.replies[0].text


def test_non_leader_seal_word_is_not_consumed():
    """「封盘」只认组长：旁人发这句落到普通兜底（指令列表），不封盘。"""
    outcome = route(
        _inbound("封盘", sender_open_id="ou_li"),
        _state(),
        _roster(),
        cards=_cards(),
        preferences=[Preference("ou_li", ["T1"], "2026-09-13T09:01:00")],
        now=OPEN,
    )
    assert outcome.save_assignments == ()
    assert _texts(outcome) == [replies.COMMAND_LIST_TEXT]


def test_non_leader_resending_gets_the_list_again_instead_of_closing_the_window():
    outcome = route(
        _inbound("你想做哪一块", sender_open_id="ou_li"),
        _state(),
        _roster(),
        cards=_cards(),
        preferences=[],
        now=OPEN,
    )
    assert outcome.state is None                               # 窗口不动
    assert outcome.save_assignments == ()
    assert "1. T1 模块1（1h）" in outcome.replies[0].text


def test_window_expiry_boundary():
    """第 2 条：4 小时 59 分不算到期；**正好 5 小时**就封盘，再过 1 分当然也是。"""

    def _send(delta):
        return route(
            _private("3"),
            _state(),
            _roster(),
            cards=_cards(),
            preferences=[],
            now=OPEN + delta,
        )

    just_before = _send(timedelta(hours=4, minutes=59))
    assert just_before.save_preference is not None              # 窗口还开着 → 照收数字
    assert just_before.save_assignments == ()

    for delta in (timedelta(hours=5), timedelta(hours=5, minutes=1)):
        outcome = _send(delta)
        assert outcome.save_preference is None, delta           # 到点就不再收数字
        assert outcome.state["awaiting"] is None, delta         # 窗口关掉
        assert "分配总表" in outcome.replies[0].text, delta      # 超时结算，不是静默丢弃


def test_yesterdays_window_is_closed_at_the_first_read():
    """跨天残留：读到就清掉，绝不"吃掉今天的数字"。"""
    outcome = route(
        _private("3"),
        _state(opened_at=OPEN - timedelta(days=1)),
        _roster(),
        cards=_cards(),
        preferences=[],
        now=OPEN,
    )
    assert outcome.save_preference is None
    assert outcome.state["awaiting"] is None


def test_stale_window_is_still_cleared_when_it_cannot_be_settled():
    """结不了（还没认下群）也得把 awaiting 清掉 —— 别让残留窗口一直挂着。"""
    outcome = route(
        _inbound("3"),
        _state(group=""),
        _roster(),
        cards=_cards(),
        preferences=[],
        now=OPEN + timedelta(hours=6),
    )
    assert outcome.state["awaiting"] is None
    assert outcome.save_assignments == ()


def test_digits_are_not_preferences_after_the_window_closed():
    """防"窗口关了还计票"（§7.1 第 2 步）。"""
    settled = route(
        _inbound("你想做哪一块"), _state(), _roster(), cards=_cards(), preferences=[], now=OPEN
    )
    outcome = route(
        _private("3"),
        settled.state,
        _roster(),
        cards=_cards(),
        preferences=[],
        now=OPEN,
    )
    assert outcome.save_preference is None
    assert _texts(outcome) == [replies.COMMAND_LIST_TEXT]


# ---------- M5 匿名代言（§6.5 / D-55）----------


def test_proposal_is_relayed_verbatim_and_leaves_a_trace():
    outcome = route(_private("我想提议：前端用 React"), _state(), _roster(), now=OPEN)
    posted, ack = outcome.replies
    assert posted.chat_id == GROUP
    assert posted.text == "有组员提议：前端用 React"
    assert "ou_li" not in posted.text and "李四" not in posted.text   # 文案里不能有人
    assert ack.chat_id == "ou_li"
    assert ack.text == replies.PROPOSAL_ACK
    assert outcome.save_proposal == {
        "user_id": "ou_li",
        "text": "前端用 React",
        "created_at": OPEN.isoformat(timespec="seconds"),
    }


def test_proposal_content_is_not_trimmed_in_the_middle():
    outcome = route(_private("我想提议：  前端  用 React  ，图表照旧 "), _state(), _roster())
    assert outcome.save_proposal["text"] == "前端  用 React  ，图表照旧"


def test_empty_proposal_is_not_relayed_and_writes_nothing():
    outcome = route(_private("我想提议："), _state(), _roster(), now=OPEN)
    assert outcome.save_proposal is None
    assert _texts(outcome) == [replies.PROPOSAL_EMPTY]


def test_proposal_without_a_known_group_asks_to_prime_it_first():
    outcome = route(_private("我想提议：加图表"), {}, None)
    assert outcome.save_proposal is None
    assert _texts(outcome) == [replies.NEED_GROUP]


def test_m4_and_m5_paths_never_carry_a_pipeline():
    """B2 铁律：M4/M5 全程零 LLM、零后台重活。"""
    cases = [
        route(_inbound("你想做哪一块"), {}, _roster(), cards=_cards(), now=OPEN),
        route(_inbound("你想做哪一块"), _state(), _roster(), cards=_cards(), now=OPEN),
        route(_private("3"), _state(), _roster(), cards=_cards(), now=OPEN),
        route(_private("我想提议：x"), _state(), _roster(), now=OPEN),
    ]
    assert all(outcome.pipeline == "" for outcome in cases)


def test_settles_into_the_group_that_opened_the_window():
    """P1-H：总表发回**开窗的那个群**，不受"最后见到的群消息"影响。"""
    state = _state()
    state["group_chat_id"] = "c_other"           # 窗口开着时另一个群来了消息
    outcome = route(
        _inbound("封盘"),
        state,
        _roster(),
        cards=_cards(),
        preferences=[Preference("ou_li", ["T1"], "2026-09-13T09:01:00")],
        now=OPEN,
    )
    assert outcome.replies[0].chat_id == GROUP   # 不是 c_other
