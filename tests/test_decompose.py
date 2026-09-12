"""M3 自检判定 ``check()`` 的单测（requirements §7.3，D-16 / D-25）。"""

from src.intelligence.decompose import check
from src.models import RubricPoint, TaskCard


def _point(pid, status="normal"):
    return RubricPoint(id=pid, quote=f"{pid} 原文", observable="可核对", status=status)


def _card(task_id="T1", refs=("R1",), hours=4.0):
    return TaskCard(
        task_id=task_id,
        module_name="实现登录模块",
        rubric_refs=list(refs),
        effort_hours=hours,
        deliverable="一个源文件",
        acceptance="从 R1 原文改写",
    )


def test_empty_cards_short_circuits_as_failure():
    failures = check([], [_point("R1")])
    assert failures
    assert "为空" in failures[0]


def test_empty_cards_does_not_raise_on_max():
    # 短路的意义：没有卡也不能碰 max()，更不能抛异常
    assert check([], [_point("R1"), _point("R2")]) != []


def test_pass_returns_empty_list():
    assert check([_card(refs=("R1", "R2"))], [_point("R1"), _point("R2")]) == []


def test_missing_point_is_reported_by_id():
    failures = check([_card(refs=("R1",))], [_point("R1"), _point("R2")])
    assert len(failures) == 1
    assert "R2" in failures[0]


def test_imbalance_is_reported():
    cards = [_card("T1", hours=7.0), _card("T2", hours=2.0)]
    failures = check(cards, [_point("R1")])
    assert len(failures) == 1
    assert "不均衡" in failures[0]


def test_floor_prevents_zero_division_in_check():
    cards = [_card("T1", hours=2.0), _card("T2", hours=0.0)]
    failures = check(cards, [_point("R1")])
    assert "不均衡" in failures[0]


def test_two_failures_are_both_reported_for_feedback():
    cards = [_card("T1", refs=("R1",), hours=8.0), _card("T2", refs=("R1",), hours=2.0)]
    failures = check(cards, [_point("R1"), _point("R2")])
    assert len(failures) == 2
    assert any("R2" in f for f in failures)
    assert any("不均衡" in f for f in failures)


def test_all_ambiguous_rubric_is_refused():
    failures = check([_card()], [_point("R1", status="ambiguous")])
    assert len(failures) == 1
    assert "拒拆" in failures[0]


def test_all_ambiguous_reason_wins_over_empty_cards():
    # 短路顺序：先看有没有可拆点，再看有没有卡（全模糊时"拒拆"更准确）
    failures = check([], [_point("R1", status="ambiguous")])
    assert len(failures) == 1
    assert "拒拆" in failures[0]


def test_ambiguous_points_may_be_referenced_without_error():
    rubric = [_point("R1"), _point("R2", status="ambiguous")]
    assert check([_card(refs=("R1", "R2"))], rubric) == []
