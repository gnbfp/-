"""M7 甘特图的单测（§3.1 / D-65）：离线渲染，不碰飞书、零 token。"""

from datetime import datetime, timedelta

import matplotlib.pyplot as plt

from src.models import AssignmentMeta, AssignmentRecord, Member, Roster, TaskCard
from src.report.gantt import plan_bars, render_gantt

NOW = datetime(2026, 9, 14, 10, 0, 0)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _meta(hours):
    deadline = "" if hours is None else (NOW + timedelta(hours=hours)).isoformat(timespec="minutes")
    return AssignmentMeta(
        course="编译原理",
        title="课程设计",
        submission="源码 + 报告",
        deadline=deadline,
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
            depends_on=["T1"] if index == 2 else [],
        )
        for index in (1, 2, 3)
    ]


def _assignments():
    return [
        AssignmentRecord(
            task_id=f"T{index}",
            assignee="ou_a" if index < 3 else "ou_b",
            source="volunteer_1",
            completed_at="2026-09-14T09:00:00" if index == 1 else None,
        )
        for index in (1, 2, 3)
    ]


def _roster():
    return Roster(
        leader="ou_a",
        members=[Member(open_id="ou_a", name="张三"), Member(open_id="ou_b", name="李四")],
        registered_at="2026-09-14T09:00:00",
        confirmed_by="ou_a",
    )


def test_cjk_font_is_configured_or_everything_is_boxes():
    assert plt.rcParams["font.sans-serif"][:2] == ["Microsoft YaHei", "SimHei"]
    assert plt.rcParams["axes.unicode_minus"] is False


def test_plan_bars_is_sequential_and_marks_completion():
    bars, deadline = plan_bars(_cards(), _assignments(), _meta(30), now=NOW)
    assert deadline == NOW + timedelta(hours=30)
    assert [b.task_id for b in bars] == ["T1", "T2", "T3"]
    assert all(b.start < b.end for b in bars)
    assert bars[0].done is True and bars[1].done is False
    assert bars[1].depends_on == ("T1",)
    assert bars[-1].end <= deadline                       # 时间条不越过截止线


def test_render_gantt_writes_a_real_png(tmp_path):
    path = render_gantt(_cards(), _assignments(), _meta(30), tmp_path / "gantt.png", _roster())
    assert path.exists()
    assert path.stat().st_size > 0
    assert path.read_bytes()[:8] == PNG_MAGIC


def test_empty_deadline_draws_no_line_and_does_not_crash(tmp_path):
    bars, deadline = plan_bars(_cards(), _assignments(), _meta(None), now=NOW)
    assert deadline is None
    assert len(bars) == len(_cards())
    path = render_gantt(_cards(), _assignments(), _meta(None), tmp_path / "no-deadline.png")
    assert path.read_bytes()[:8] == PNG_MAGIC


def test_render_gantt_survives_missing_assignments_and_roster(tmp_path):
    path = render_gantt(_cards(), [], _meta(30), tmp_path / "empty.png")
    assert path.read_bytes()[:8] == PNG_MAGIC