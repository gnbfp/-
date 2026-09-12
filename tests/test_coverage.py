"""M3 循环内判定函数的单测（requirements §7.2 / §7.3 / §7.4）。

只测本项目的两个循环口径；M8 的评测口径（人工分母 / 归一化极差）不在这里。
"""

import pytest

from src.intelligence.coverage import balance_loop, coverage_loop
from src.models import RubricPoint, TaskCard


def _point(pid, status="normal", weight=None):
    return RubricPoint(id=pid, quote=f"{pid} 原文", observable="可核对", status=status, weight=weight)


def _card(task_id="T1", refs=("R1",), hours=4.0):
    return TaskCard(
        task_id=task_id,
        module_name="实现登录模块",
        rubric_refs=list(refs),
        effort_hours=hours,
        deliverable="一个源文件",
        acceptance="从 R1 原文改写",
    )


# ---------- coverage_loop ----------


def test_full_coverage_passes():
    result = coverage_loop([_card(refs=("R1", "R2"))], [_point("R1"), _point("R2")])
    assert result.ok
    assert result.ratio == 1.0
    assert result.missing == ()


def test_missing_point_fails_and_is_named():
    result = coverage_loop([_card(refs=("R1",))], [_point("R1"), _point("R2")])
    assert not result.ok
    assert result.missing == ("R2",)
    assert result.ratio == pytest.approx(0.5)


def test_ambiguous_points_are_not_in_denominator():
    rubric = [_point("R1"), _point("R2", status="ambiguous")]
    result = coverage_loop([_card(refs=("R1",))], rubric)
    assert result.ok
    assert result.eligible == ("R1",)
    assert result.missing == ()


def test_extra_refs_to_ambiguous_or_unknown_ids_are_not_errors():
    rubric = [_point("R1"), _point("R2", status="ambiguous")]
    result = coverage_loop([_card(refs=("R1", "R2", "R99"))], rubric)
    assert result.ok
    assert result.covered == ("R1",)


def test_coverage_dedupes_refs_across_cards():
    cards = [_card("T1", refs=("R1",)), _card("T2", refs=("R1",))]
    result = coverage_loop(cards, [_point("R1")])
    assert result.covered == ("R1",)
    assert result.ratio == 1.0


def test_weight_missing_does_not_affect_coverage():
    result = coverage_loop([_card(refs=("R1",))], [_point("R1", weight=None)])
    assert result.ok


def test_no_eligible_points_is_not_a_pass():
    result = coverage_loop([_card()], [_point("R1", status="ambiguous")])
    assert result.eligible == ()
    assert result.ratio == 0.0            # 空分母不是达标
    assert result.ok is False


def test_no_eligible_points_and_no_cards_is_not_a_pass():
    result = coverage_loop([], [_point("R1", status="ambiguous")])
    assert result.ratio == 0.0
    assert result.ok is False


def test_no_cards_against_eligible_points_is_zero():
    result = coverage_loop([], [_point("R1")])
    assert result.ratio == 0.0
    assert result.missing == ("R1",)


# ---------- balance_loop ----------


def test_equal_hours_are_balanced():
    result = balance_loop([_card("T1", hours=4.0), _card("T2", hours=4.0)])
    assert result.ok
    assert result.ratio == 1.0


def test_ratio_at_limit_still_passes():
    result = balance_loop([_card("T1", hours=6.0), _card("T2", hours=2.0)])
    assert result.ratio == 3.0
    assert result.ok


def test_ratio_over_limit_fails():
    result = balance_loop([_card("T1", hours=7.0), _card("T2", hours=2.0)])
    assert result.ratio > 3.0
    assert not result.ok


def test_min_hours_floor_prevents_division_by_zero():
    result = balance_loop([_card("T1", hours=2.0), _card("T2", hours=0.0)])
    assert result.min_hours == 0.5
    assert result.ratio == 4.0
    assert not result.ok


def test_hours_below_floor_are_lifted():
    result = balance_loop([_card("T1", hours=0.1)])
    assert result.hours == (0.5,)
    assert result.ratio == 1.0
    assert result.ok


def test_empty_cards_are_vacuously_balanced():
    result = balance_loop([])
    assert result.ratio == 1.0
    assert result.ok
