"""M6「完成 T3」标记的单测（§2.1 / §7.1 第 6 条、D-22 / D-31）。纯函数、零 I/O、零 LLM。"""

from datetime import datetime

from src.gateway import replies
from src.gateway.complete import accept
from src.gateway.events import Inbound
from src.models import AssignmentRecord, TaskCard

NOW = datetime(2026, 9, 14, 10, 0, 0)


def _inbound(chat_type="p2p", sender="ou_a"):
    return Inbound(
        chat_id="p1" if chat_type == "p2p" else "c1",
        chat_type=chat_type,
        message_type="text",
        text="完成 T1",
        sender_type="user",
        sender_open_id=sender,
        message_id="m1",
    )


def _cards():
    return [
        TaskCard(
            task_id=f"T{index}",
            module_name=f"模块{index}",
            rubric_refs=["R1"],
            effort_hours=2.0,
            deliverable="交付物",
            acceptance="验收标准",
        )
        for index in (1, 2, 3)
    ]


def _assignments():
    return [
        AssignmentRecord(task_id="T1", assignee="ou_a", source="volunteer_1"),
        AssignmentRecord(task_id="T2", assignee="ou_b", source="volunteer_1"),
    ]


def _texts(outcome):
    return [r.text for r in outcome.replies]


def test_private_marking_stamps_completed_at():
    outcome = accept("1", _inbound(), _assignments(), _cards(), NOW)
    assert outcome.save_complete == {
        "task_id": "T1",
        "completed_at": "2026-09-14T10:00:00",
    }
    assert _texts(outcome) == [replies.COMPLETE_OK.format(task_id="T1", module="模块1")]


def test_marking_twice_is_idempotent_and_keeps_the_first_stamp():
    """幂等：第二次回原时间戳，**不覆盖** —— 那是真实工作量的证据（§2.1）。"""
    marked = [AssignmentRecord("T1", "ou_a", "volunteer_1", "2026-09-13T08:00:00")]
    outcome = accept("1", _inbound(), marked, _cards(), NOW)
    assert outcome.save_complete is None                  # 不写盘
    assert _texts(outcome) == [
        replies.COMPLETE_ALREADY.format(task_id="T1", at="2026-09-13T08:00:00")
    ]


def test_someone_elses_card_gets_no_fake_confirmation():
    outcome = accept("2", _inbound(sender="ou_a"), _assignments(), _cards(), NOW)
    assert outcome.save_complete is None
    assert _texts(outcome) == [
        replies.COMPLETE_NOT_YOURS.format(index="2", mine=replies.COMPLETE_MINE.format(tasks="T1（模块1）"))
    ]


def test_unknown_task_id_lists_what_you_actually_hold():
    outcome = accept("7", _inbound(), _assignments(), _cards(), NOW)
    assert outcome.save_complete is None
    assert _texts(outcome) == [
        replies.COMPLETE_UNKNOWN.format(index="7", mine=replies.COMPLETE_MINE.format(tasks="T1（模块1）"))
    ]


def test_group_message_is_told_to_go_private():
    """群里标完成没人知道标的是谁的卡 —— 只回一句"去私聊"（§2.1）。"""
    outcome = accept("1", _inbound(chat_type="group"), _assignments(), _cards(), NOW)
    assert outcome.save_complete is None
    assert _texts(outcome) == [replies.COMPLETE_NEED_DM]


def test_without_assignments_it_says_so():
    outcome = accept("1", _inbound(), (), _cards(), NOW)
    assert outcome.save_complete is None
    assert _texts(outcome) == [replies.COMPLETE_NEED_ASSIGNMENTS]


def test_no_cards_still_renders_mine_without_module_names():
    """没有任务卡（旧数据 / 命令行路径）也不许 KeyError：退化成 task_id 本身。"""
    outcome = accept("1", _inbound(), _assignments(), (), NOW)
    assert _texts(outcome) == [replies.COMPLETE_OK.format(task_id="T1", module="T1")]