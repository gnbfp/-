"""M7 简版核对清单渲染的单测（§7.2 展示口径与判定口径一致）。"""

from src.intelligence.decompose import DecomposeResult
from src.models import (
    AssignmentMeta,
    AssignmentRecord,
    Member,
    Roster,
    RubricPoint,
    TaskCard,
)
from src.report.checklist import render_checklist

META = AssignmentMeta(
    course="编译原理",
    title="C 语言课程设计",
    submission="源码 + 报告",
    deadline="2026-09-19T23:59",
    source_file="作业书.txt",
)


def _card(task_id, refs, hours=4.0):
    return TaskCard(
        task_id=task_id,
        module_name="实现词法分析器",
        rubric_refs=list(refs),
        effort_hours=hours,
        deliverable="一个源文件",
        acceptance="从 R1 原文改写",
    )


def _points():
    return [
        RubricPoint(id="R1", quote="实现词法分析器", observable="可运行", status="normal", weight=40),
        RubricPoint(id="R2", quote="撰写实验报告", observable="有报告", status="normal", weight=30),
        RubricPoint(id="R3", quote="内容充实", observable="不可核对", status="ambiguous", weight=30),
    ]


def test_checklist_marks_covered_and_missing_points():
    points = _points()
    cards = [_card("T1", ["R1"])]
    result = DecomposeResult(cards=tuple(cards), failures=("未覆盖的评分点：R2",), generations=2)
    text = render_checklist(META, points, cards, result)
    assert "[x] R1（40）→ T1" in text
    assert "[ ] R2（30）未覆盖" in text
    assert "[?] R3（30）需组长确认" in text
    assert "覆盖率：1/2 = 50%" in text
    assert "生成 2 轮" in text
    assert "自检未达标" in text
    assert "《C 语言课程设计》" in text


def test_checklist_reports_pass_when_self_check_clean():
    points = _points()
    cards = [_card("T1", ["R1"]), _card("T2", ["R2"])]
    result = DecomposeResult(cards=tuple(cards), failures=(), generations=1)
    text = render_checklist(META, points, cards, result)
    assert "覆盖率：2/2 = 100%" in text
    assert "自检通过" in text
    assert "自检未达标" not in text


def test_checklist_all_ambiguous_prints_no_percentage():
    points = [
        RubricPoint(id="R1", quote="内容充实", observable="不可核对", status="ambiguous", weight=100)
    ]
    result = DecomposeResult(
        cards=(), failures=("没有任何可拆评分点（全部为 ambiguous）→ 拒拆",), generations=0
    )
    text = render_checklist(META, points, [], result)
    assert "覆盖率：无可拆点 → 拒拆（不是 100%）" in text
    # 固定文案里本来就有"不是 100%"；除此之外不许再格式化出任何百分比
    assert text.count("%") == 1
    assert "0/0" not in text


def test_checklist_shows_ambiguous_point_referenced_by_card():
    points = _points()
    cards = [_card("T1", ["R1"]), _card("T2", ["R2"]), _card("T5", ["R3"])]
    result = DecomposeResult(cards=tuple(cards), failures=(), generations=1)
    text = render_checklist(META, points, cards, result)
    assert "[?] R3（30）需组长确认（模糊要求；已被 T5 引用）" in text
    assert "覆盖率：2/2 = 100%" in text          # 模糊点不进分子分母
    assert "任务卡 3 张" in text                  # 它的卡照常计入总数与均衡


def test_checklist_shows_unlabeled_when_deadline_empty():
    # D-49：空 deadline 要显式显示成「未标注」，不能是个看不出来的空字符串
    meta = AssignmentMeta(
        course="编译原理",
        title="课程设计",
        submission="源码 + 报告",
        deadline="",
        source_file="作业书.txt",
    )
    result = DecomposeResult(cards=(), failures=(), generations=0)
    text = render_checklist(meta, _points(), [], result)
    assert "截止：未标注" in text


def _roster():
    return Roster(
        leader="ou_a",
        members=[Member(open_id="ou_a", name="张三"), Member(open_id="ou_b", name="李四")],
        registered_at="2026-09-14T09:00:00",
        confirmed_by="ou_a",
    )


def test_report_columns_are_owner_and_completion_not_coverage():
    """M7：执行阶段的「负责人 + 完成」是**另一回事**，不能和覆盖的 [x]/[ ] 混。"""
    points = _points()
    cards = [_card("T1", ["R1"]), _card("T2", ["R2"])]
    result = DecomposeResult(cards=tuple(cards), failures=(), generations=0)
    assignments = [
        AssignmentRecord("T1", "ou_a", "volunteer_1", "2026-09-14T09:00:00"),
        AssignmentRecord("T2", "ou_b", "auto"),
    ]
    text = render_checklist(
        META, points, cards, result, assignments=assignments, roster=_roster()
    )
    assert "[x] R1（40）→ T1 ｜负责人：张三 ｜完成 1/1" in text
    assert "[x] R2（30）→ T2 ｜负责人：李四 ｜完成 0/1" in text
    assert "[?] R3（30）需组长确认（模糊要求，未进循环分母） ｜负责人：— ｜完成 —" in text
    assert "覆盖率：2/2 = 100%" in text          # 执行列不污染覆盖率口径
    assert "生成" not in text                    # 报告是快照，没有"拆了几轮"


def test_without_assignments_the_old_output_is_unchanged():
    points = _points()
    cards = [_card("T1", ["R1"])]
    result = DecomposeResult(cards=tuple(cards), failures=(), generations=2)
    text = render_checklist(META, points, cards, result)
    assert "负责人" not in text and "完成 " not in text
