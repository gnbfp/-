"""M2 方向投票窗口单测（方案 §4 组 2~6，D-35 / D-36）。

全程离线：不碰飞书、不碰网络、不碰 LLM（投票全是纯规则）。
"""

from datetime import datetime, timedelta

from src.gateway import replies, vote
from src.gateway.events import Inbound, Outcome
from src.gateway.router import route
from src.models import Member, Roster, TaskCard

OPEN = datetime(2026, 9, 14, 20, 0, 0)
GROUP = "c_group"
LEADER = "ou_zhang"

CANDIDATES = [
    {"id": 1, "title": "做一个校园二手交易平台", "note": "对上 R2", "rubric_refs": ["R2"]},
    {"id": 2, "title": "做一个课程知识点问答机器人", "note": "对上 R3", "rubric_refs": ["R3"]},
    {"id": 3, "title": "做一个实验数据可视化看板", "note": "对上 R4", "rubric_refs": ["R4"]},
]


def _roster():
    """三人花名册 ⇒ D-36 门槛 = ceil(3 / 2) = 2 人。"""
    return Roster(
        leader=LEADER,
        members=[
            Member(open_id=open_id, name=name)
            for open_id, name in (
                (LEADER, "张三"),
                ("ou_li", "李四"),
                ("ou_wang", "王五"),
            )
        ],
        registered_at="2026-09-13T09:00:00",
        confirmed_by=LEADER,
    )


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


def _inbound(text="", **over):
    data = dict(
        chat_id=GROUP,
        chat_type="group",
        message_type="text",
        text=text,
        sender_type="user",
        sender_open_id="ou_li",
        message_id="m1",
    )
    data.update(over)
    return Inbound(**data)


def _private(text, sender="ou_li"):
    return _inbound(text, chat_type="p2p", chat_id=sender, sender_open_id=sender)


def _state(opened_at=OPEN, votes=None, awaiting="vote", group=GROUP):
    return {
        "awaiting": awaiting,
        "vote": {
            "chat_id": group,
            "opened_at": opened_at.isoformat(timespec="seconds"),
            "opened_by": LEADER,
            "candidates": [dict(item) for item in CANDIDATES],
            "votes": dict(votes or {}),
        },
        "group_chat_id": group,
    }


def _frozen(votes=None):
    """超时之后被**冻住**的窗口：``awaiting`` 还是 vote、候选与票数都还在。"""
    state = _state(opened_at=OPEN - timedelta(minutes=11), votes=votes)
    state["vote"]["closed"] = True
    return state


def _texts(outcome):
    return [r.text for r in outcome.replies]


# ---------- 开窗口与前置（§2.2）----------


def test_group_direction_acks_and_starts_the_pipeline():
    outcome = route(_inbound("方向"), {}, _roster(), has_rubric=True, now=OPEN)
    assert _texts(outcome) == [replies.VOTE_GENERATING]
    assert outcome.pipeline == "direction"      # 唯一的重活：生成候选
    assert outcome.state is None                # 窗口由后台生成成功后落


def test_direction_without_rubric_does_not_start_the_pipeline():
    outcome = route(_inbound("方向"), {}, _roster(), has_rubric=False, now=OPEN)
    assert _texts(outcome) == [replies.NEEDS_RUBRIC]
    assert outcome.pipeline == ""


def test_direction_without_roster_does_not_start_the_pipeline():
    outcome = route(_inbound("方向"), {}, None, has_rubric=True, now=OPEN)
    assert _texts(outcome) == [replies.VOTE_NEED_ROSTER]
    assert outcome.pipeline == ""


def test_private_direction_points_to_the_group_without_burning_tokens():
    outcome = route(_private("方向"), {}, _roster(), has_rubric=True, now=OPEN)
    assert _texts(outcome) == [replies.VOTE_NEED_GROUP]
    assert outcome.pipeline == ""


def test_running_window_is_not_regenerated_and_keeps_its_votes():
    state = _state(votes={"ou_li": 1})
    outcome = route(
        _inbound("方向"), state, _roster(), has_rubric=True, now=OPEN + timedelta(minutes=1)
    )
    assert _texts(outcome) == [replies.VOTE_IN_PROGRESS.format(minutes=9)]
    assert outcome.state is None                # 不重开窗、不清票
    assert outcome.pipeline == ""


def test_open_window_posts_candidates_to_the_group_as_advisory():
    outcome = vote.open_window(_inbound("方向"), {"group_chat_id": GROUP}, CANDIDATES, now=OPEN)
    assert outcome.state["awaiting"] == "vote"
    assert outcome.state["vote"]["opened_at"] == OPEN.isoformat(timespec="seconds")
    assert outcome.state["vote"]["votes"] == {}
    assert outcome.state["vote"]["chat_id"] == GROUP
    group_reply = outcome.replies[0]
    assert (group_reply.chat_id, group_reply.receive_id_type) == (GROUP, "chat_id")
    assert "仅供参考，由全组拍板" in group_reply.text
    assert "1. 做一个校园二手交易平台" in group_reply.text
    assert "回复数字投票" in group_reply.text


# ---------- 收票（§2.3）----------


def test_member_digit_in_the_window_is_recorded_with_an_ack():
    outcome = route(_inbound("2"), _state(), _roster(), now=OPEN)
    assert outcome.state["vote"]["votes"] == {"ou_li": 2}
    assert _texts(outcome) == [
        replies.VOTE_ACK.format(id=2, title=CANDIDATES[1]["title"])
    ]
    assert outcome.save_direction is None       # 一个人不算过半


def test_resubmission_overwrites_the_earlier_vote():
    outcome = route(_inbound("2"), _state(votes={"ou_li": 1}), _roster(), now=OPEN)
    assert outcome.state["vote"]["votes"] == {"ou_li": 2}


def test_stranger_digit_is_ignored_silently():
    outcome = route(
        _inbound("2", sender_open_id="ou_stranger"), _state(), _roster(), now=OPEN
    )
    assert outcome == Outcome()                 # 不记、不回话、不报错


def test_private_digit_is_not_a_vote_and_falls_through_to_the_prefixes():
    outcome = route(_private("2"), _state(), _roster(), now=OPEN)
    assert outcome.state is None
    assert _texts(outcome) == [replies.COMMAND_LIST_TEXT]


def test_out_of_range_digit_is_rejected_without_writing():
    outcome = route(_inbound("9"), _state(), _roster(), now=OPEN)
    assert _texts(outcome) == [replies.VOTE_BAD.format(ids="1、2、3")]
    assert outcome.state is None                # 不落盘


def test_non_numeric_junk_is_rejected_without_writing():
    outcome = route(_inbound("abc"), _state(), _roster(), now=OPEN)
    assert _texts(outcome) == [replies.VOTE_BAD.format(ids="1、2、3")]
    assert outcome.state is None


def test_other_group_digit_is_ignored_silently():
    outcome = route(_inbound("2", chat_id="c_other"), _state(), _roster(), now=OPEN)
    assert outcome == Outcome()


def test_vote_window_does_not_eat_commands():
    outcome = route(_inbound("拆解"), _state(), _roster(), has_rubric=True, now=OPEN)
    assert _texts(outcome) == [replies.DECOMPOSING]
    assert outcome.pipeline == "decompose"
    assert outcome.state is None                # 窗口不动


# ---------- 落定：过半 + 门槛（§2.4 条 1 / D-35 / D-36）----------


def test_one_vote_is_not_enough_even_though_it_is_over_half():
    """1/1 数字上"过半"，但没过 D-36 门槛（2 人），一律不落定。"""
    outcome = route(_inbound("2"), _state(), _roster(), now=OPEN)
    assert outcome.save_direction is None
    assert _texts(outcome) == [
        replies.VOTE_ACK.format(id=2, title=CANDIDATES[1]["title"])
    ]


def test_two_voters_on_the_same_direction_settle_it():
    state = _state(votes={"ou_li": 2})
    outcome = route(_inbound("2", sender_open_id="ou_wang"), state, _roster(), now=OPEN)

    assert outcome.state["awaiting"] is None
    assert outcome.state["vote"] is None
    assert len(outcome.replies) == 2            # 先回执，再落定
    settled = outcome.replies[-1]
    assert settled.chat_id == GROUP
    assert "方向已定" in settled.text
    assert CANDIDATES[1]["title"] in settled.text
    assert "2/2" in settled.text

    payload = outcome.save_direction
    assert payload["decided_by"] == "vote"
    assert payload["reason"] == "过半落定"
    assert payload["winner"]["id"] == 2
    assert payload["votes"] == {"ou_li": 2, "ou_wang": 2}
    assert payload["tally"] == {"1": 0, "2": 2, "3": 0}


def test_split_votes_do_not_settle():
    state = _state(votes={"ou_li": 1})
    outcome = route(_inbound("2", sender_open_id="ou_wang"), state, _roster(), now=OPEN)
    assert outcome.save_direction is None
    assert outcome.state["vote"]["votes"] == {"ou_li": 1, "ou_wang": 2}


def test_digits_after_settling_are_no_longer_votes():
    state = _state(votes={"ou_li": 2})
    settled = route(_inbound("2", sender_open_id="ou_wang"), state, _roster(), now=OPEN)
    assert settled.state["awaiting"] is None

    after = route(_inbound("2"), settled.state, _roster(), now=OPEN)
    assert after.state is None
    assert _texts(after) == [replies.COMMAND_LIST_TEXT]


# ---------- 超时 10 分钟（§2.4 条 2）----------


def test_timeout_boundary_freezes_the_window():
    """恰好 10 分钟（``>=``）就到点；窗口是**冻住**，不是清空。"""
    state = _state(opened_at=OPEN - timedelta(minutes=10), votes={"ou_li": 1})
    outcome = route(_inbound("2"), state, _roster(), now=OPEN)
    assert outcome.state["awaiting"] == "vote"
    assert outcome.state["vote"]["closed"] is True
    assert "10 分钟到" in _texts(outcome)[0]


def test_timeout_without_a_majority_freezes_the_window_and_reports_the_tally():
    state = _state(opened_at=OPEN - timedelta(minutes=11), votes={"ou_li": 1})
    outcome = route(_inbound("2"), state, _roster(), now=OPEN)

    text = _texts(outcome)[0]
    assert "10 分钟到" in text
    assert "1 号 1 票" in text                  # 含票数明细
    assert "数字不再计票" in text
    assert outcome.state["awaiting"] == "vote"  # 窗口没关，只是冻住
    assert outcome.state["vote"]["closed"] is True
    assert outcome.state["vote"]["votes"] == {"ou_li": 1}     # 票数原样保留
    assert [c["id"] for c in outcome.state["vote"]["candidates"]] == [1, 2, 3]
    assert outcome.save_direction is None       # 没落定就不落盘


def test_timeout_with_a_majority_still_settles():
    state = _state(
        opened_at=OPEN - timedelta(minutes=11), votes={"ou_li": 2, "ou_wang": 2}
    )
    outcome = route(_inbound("2"), state, _roster(), now=OPEN)
    assert outcome.save_direction["reason"] == "过半落定"
    assert outcome.state["awaiting"] is None


# ---------- 冻住的窗口（M2 复核 P1）----------


def test_frozen_window_ignores_digits_without_a_reply():
    """冻住之后数字静默不计：票数不变、一个字都不回。"""
    outcome = route(
        _inbound("2", sender_open_id="ou_wang"), _frozen(votes={"ou_li": 1}), _roster(), now=OPEN
    )
    assert outcome == Outcome()


def test_frozen_window_still_lets_commands_through():
    outcome = route(
        _inbound("拆解"), _frozen(votes={"ou_li": 1}), _roster(), has_rubric=True, now=OPEN
    )
    assert _texts(outcome) == [replies.DECOMPOSING]
    assert outcome.pipeline == "decompose"


def test_leader_seals_the_frozen_window_on_the_plurality():
    """超时那条路的正解：组长在**同一批候选、同一张票数表**上拍板。"""
    outcome = route(
        _inbound("封盘", sender_open_id=LEADER),
        _frozen(votes={"ou_li": 2, "ou_wang": 2}),
        _roster(),
        now=OPEN,
    )
    assert outcome.save_direction["winner"]["id"] == 2
    assert outcome.save_direction["reason"] == "超时后组长指定"
    assert outcome.save_direction["decided_by"] == "leader"
    assert outcome.state["awaiting"] is None


def test_leader_names_a_number_on_the_frozen_window():
    outcome = route(
        _inbound("封盘 2", sender_open_id=LEADER),
        _frozen(votes={"ou_li": 1}),
        _roster(),
        now=OPEN,
    )
    assert outcome.save_direction["winner"]["id"] == 2
    assert outcome.save_direction["reason"] == "超时后组长指定"
    # 用的是**冻住的那批候选**，编号跟超时明细是同一张表
    assert [c["id"] for c in outcome.save_direction["candidates"]] == [1, 2, 3]


def test_non_leader_seal_on_the_frozen_window_is_still_refused():
    outcome = route(
        _inbound("封盘", sender_open_id="ou_li"), _frozen(votes={"ou_li": 2}), _roster(), now=OPEN
    )
    assert _texts(outcome) == [replies.VOTE_NEED_LEADER]
    assert outcome.save_direction is None


def test_direction_after_freezing_opens_a_new_window():
    """冻住不算"还在投票"：再发「方向」应当开新窗口，而不是回"还剩 N 分钟"。"""
    outcome = route(
        _inbound("方向"), _frozen(votes={"ou_li": 1}), _roster(), has_rubric=True, now=OPEN
    )
    assert _texts(outcome) == [replies.VOTE_GENERATING]
    assert outcome.pipeline == "direction"


# ---------- 组长拍板（§2.5）----------


def test_leader_seal_takes_the_plurality():
    outcome = route(
        _inbound("封盘", sender_open_id=LEADER),
        _state(votes={"ou_li": 2, "ou_wang": 2}),
        _roster(),
        now=OPEN,
    )
    assert outcome.save_direction["decided_by"] == "leader"
    assert outcome.save_direction["reason"] == "组长拍板"
    assert outcome.save_direction["winner"]["id"] == 2
    assert outcome.state["awaiting"] is None


def test_leader_seal_breaks_a_tie_by_the_smallest_id():
    outcome = route(
        _inbound("封盘", sender_open_id=LEADER),
        _state(votes={"ou_li": 1, "ou_wang": 2}),
        _roster(),
        now=OPEN,
    )
    assert outcome.save_direction["winner"]["id"] == 1


def test_leader_can_name_a_candidate_without_any_vote():
    outcome = route(
        _inbound("封盘 3", sender_open_id=LEADER), _state(votes={}), _roster(), now=OPEN
    )
    assert outcome.save_direction["winner"]["id"] == 3
    assert outcome.save_direction["reason"] == "组长拍板"
    assert outcome.save_direction["tally"] == {"1": 0, "2": 0, "3": 0}


def test_leader_named_pick_after_the_timeout_is_recorded_as_such():
    outcome = route(
        _inbound("封盘 1", sender_open_id=LEADER),
        _state(opened_at=OPEN - timedelta(minutes=11), votes={"ou_li": 2}),
        _roster(),
        now=OPEN,
    )
    assert outcome.save_direction["winner"]["id"] == 1
    assert outcome.save_direction["reason"] == "超时后组长指定"


def test_leader_seal_without_any_vote_asks_for_a_pick():
    outcome = route(
        _inbound("封盘", sender_open_id=LEADER), _state(votes={}), _roster(), now=OPEN
    )
    assert _texts(outcome) == [replies.VOTE_SEAL_NEED_PICK]
    assert outcome.state is None
    assert outcome.save_direction is None


def test_non_leader_seal_is_refused():
    outcome = route(_inbound("封盘", sender_open_id="ou_li"), _state(), _roster(), now=OPEN)
    assert _texts(outcome) == [replies.VOTE_NEED_LEADER]
    assert outcome.state is None
    assert outcome.save_direction is None


def test_leader_seal_out_of_range_is_rejected():
    outcome = route(
        _inbound("封盘 9", sender_open_id=LEADER), _state(), _roster(), now=OPEN
    )
    assert _texts(outcome) == [replies.VOTE_BAD.format(ids="1、2、3")]
    assert outcome.save_direction is None


# ---------- 与 M4 / 登记互清（§2.7）----------


def test_opening_a_vote_window_clears_register_and_preference_residue():
    state = {
        "awaiting": "preference",
        "register": {"stage": "collect"},
        "preference": {"opened_at": OPEN.isoformat(timespec="seconds"), "chat_id": GROUP},
        "group_chat_id": GROUP,
    }
    outcome = vote.open_window(_inbound("方向"), state, CANDIDATES, now=OPEN)
    assert outcome.state["register"] is None
    assert outcome.state["preference"] is None
    assert outcome.state["awaiting"] == "vote"


def test_preference_window_clears_vote_residue():
    state = _state()
    outcome = route(
        _inbound("你想做哪一块"), state, _roster(), cards=_cards(), now=OPEN
    )
    assert outcome.state["awaiting"] == "preference"
    assert outcome.state["vote"] is None
