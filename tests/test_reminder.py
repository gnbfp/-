"""M6 临期催办扫描的单测（§2.2 / 待定义-35、D-66）。纯函数、零 I/O、零 LLM。"""

from datetime import datetime, timedelta

from src.gateway.reminder import (
    TIER_OVERDUE,
    TIER_T1,
    TIER_T2,
    scan,
    settings,
)
from src.models import AssignmentMeta, AssignmentRecord, TaskCard

NOW = datetime(2026, 9, 14, 10, 0, 0)


def _meta(hours, deadline=None):
    if deadline is not None:
        value = deadline
    elif hours is None:
        value = ""
    else:
        value = (NOW + timedelta(hours=hours)).isoformat(timespec="minutes")
    return AssignmentMeta(
        course="编译原理",
        title="课程设计",
        submission="源码",
        deadline=value,
        source_file="作业书.txt",
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
        for index in (1, 2)
    ]


def _assignments(**completed):
    records = []
    for index, assignee in ((1, "ou_a"), (2, "ou_b")):
        records.append(
            AssignmentRecord(
                task_id=f"T{index}",
                assignee=assignee,
                source="volunteer_1",
                completed_at=completed.get(f"T{index}"),
            )
        )
    return records


def test_within_48h_fires_the_first_tier():
    due = scan(_cards(), _assignments(), _meta(30), (), NOW)
    assert [(r.task_id, r.tier) for r in due] == [("T1", TIER_T1), ("T2", TIER_T1)]
    assert "还差 30 小时" in due[0].text
    assert '<at user_id="ou_a"></at>' in due[0].text      # 真 @ 语法，不是纯文本


def test_within_24h_fires_the_second_tier():
    due = scan(_cards(), _assignments(), _meta(10), (), NOW)
    assert {r.tier for r in due} == {TIER_T2}
    assert "还差 10 小时" in due[0].text


def test_overdue_is_still_reminded():
    """逾期不是"还没到点"，是"更该催"（D-66 第三档）。"""
    due = scan(_cards(), _assignments(), _meta(-5), (), NOW)
    assert {r.tier for r in due} == {TIER_OVERDUE}
    assert "已经逾期" in due[0].text


def test_completed_cards_are_never_reminded():
    due = scan(_cards(), _assignments(T1="2026-09-14T09:00:00"), _meta(10), (), NOW)
    assert [r.task_id for r in due] == ["T2"]


def test_empty_deadline_skips_the_whole_round():
    """D-49 允许 deadline 为空：宁可漏催、不可乱催 —— 整轮跳过。"""
    assert scan(_cards(), _assignments(), _meta(None), (), NOW) == []
    assert scan(_cards(), _assignments(), _meta(0, deadline="未标注"), (), NOW) == []
    assert scan(_cards(), _assignments(), _meta(0, deadline="下周交"), (), NOW) == []


def test_same_task_and_tier_is_only_sent_once():
    sent = [
        {"task_id": "T1", "tier": TIER_T1, "ok": True},
        {"task_id": "T2", "tier": TIER_T1, "ok": True},
    ]
    assert scan(_cards(), _assignments(), _meta(30), sent, NOW) == []


def test_a_failed_send_is_retried_next_round():
    """必修 A：``ok: false`` 的那次不算"已催"，下一轮要重试。"""
    failed = [{"task_id": "T1", "tier": TIER_T1, "ok": False}]
    due = scan(_cards(), _assignments(), _meta(30), failed, NOW)
    assert [r.task_id for r in due] == ["T1", "T2"]        # T1 补发，T2 照常


def test_a_legacy_record_without_ok_is_treated_as_not_sent():
    """旧记录没有 ok 字段 -> 视为未发成功 -> 补发一次（方向是对的）。"""
    legacy = [{"task_id": "T1", "tier": TIER_T1}]
    due = scan(_cards(), _assignments(), _meta(30), legacy, NOW)
    assert [r.task_id for r in due] == ["T1", "T2"]


def test_the_record_carries_the_assignee():
    """必修 E：reminders.json 要能看出催的是谁（事后审计）。"""
    due = scan(_cards(), _assignments(), _meta(30), (), NOW)
    record = due[0].to_record("c1", "2026-09-14T10:05:00", True)
    assert record["assignee"] == "ou_a"
    assert set(record) == {"task_id", "tier", "assignee", "chat_id", "sent_at", "ok"}


def test_changing_tier_reminds_again():
    """带着上一档的记录再来一轮：同一张卡换档要再催一次（48h → 24h）。"""
    sent = [{"task_id": "T1", "tier": TIER_T1, "ok": True}]
    due = scan(_cards(), _assignments(), _meta(10), sent, NOW)
    # 两张卡这时都进了 24h 档：T1 换档要再催一次，T2 之前没催过
    assert [(r.task_id, r.tier) for r in due] == [("T1", TIER_T2), ("T2", TIER_T2)]


def test_unassigned_cards_are_skipped():
    assignments = [r for r in _assignments() if r.task_id == "T1"]
    due = scan(_cards(), assignments, _meta(10), (), NOW)
    assert [r.task_id for r in due] == ["T1"]


def test_settings_fall_back_to_defaults_on_garbage(monkeypatch):
    monkeypatch.setenv("REMIND_INTERVAL_SECONDS", "abc")
    monkeypatch.setenv("REMIND_TIER1_HOURS", "0")
    monkeypatch.setenv("REMIND_TIER2_HOURS", "12")
    assert settings() == (3600, 48, 12)