"""M8 复算脚本的单测（口径见 `eval/report.py` 顶部 docstring）。

**离线**：不调 LLM、不需要真作业书、不碰 `data/`。

本文件还守着 D-19：`eval/` 必须自带一份覆盖率实现，
**禁止 import `src/intelligence/coverage.py`** —— 复用同一份实现等于自己改自己的卷子。
"""

import json
from pathlib import Path

import pytest

from eval.report import BaselineError, compute, load_baseline, load_runs, render

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
BASELINE_DIR = EVAL_DIR / "baseline"


def _baseline(points, doc_id="t", title="测试作业书"):
    return {
        "doc_id": doc_id,
        "title": title,
        "source_file": "x.pdf",
        "scoring_section": "评分标准",
        "annotator": "测试",
        "annotated_at": "2026-09-13",
        "points": points,
    }


def _point(order, weight=None, decomposable=True, label="标签"):
    return {"order": order, "weight": weight, "decomposable": decomposable, "label": label}


def _rubric(*specs):
    """specs = (id, status, weight) 三元组。"""
    return [
        {
            "id": pid,
            "quote": f"{pid} 原文",
            "observable": "可核对",
            "status": status,
            "weight": weight,
        }
        for pid, status, weight in specs
    ]


def _card(task_id="T1", refs=("R1",), hours=4.0):
    return {
        "task_id": task_id,
        "module_name": "模块",
        "rubric_refs": list(refs),
        "effort_hours": hours,
        "deliverable": "交付物",
        "acceptance": "验收",
    }


# ---------- 1. 分母用人工集合，不采纳 agent 的 status ----------


def test_denominator_uses_human_set_not_agent_status():
    """最核心一条：agent 判 ambiguous、但卡片引用了它 —— 仍计入覆盖（D-51 ①）。"""
    baseline = _baseline([_point(1, 30), _point(2, 70)])
    rubric = _rubric(("R1", "ambiguous", 30), ("R2", "normal", 70))
    result = compute(baseline, rubric, [_card(refs=("R1", "R2"))])
    assert result.baseline_decomposable == 2
    assert result.covered == 2
    assert result.ratio == pytest.approx(1.0)
    assert result.missing == ()
    assert any("口径差异" in note for note in result.notes)


# ---------- 2. 漏了可拆点 ----------


def test_missed_point_lowers_ratio_and_is_listed():
    baseline = _baseline([_point(1), _point(2), _point(3)])
    rubric = _rubric(("R1", "normal", None), ("R2", "normal", None), ("R3", "normal", None))
    result = compute(baseline, rubric, [_card(refs=("R1",))])
    assert result.covered == 1
    assert result.ratio == pytest.approx(1 / 3)
    assert result.missing == (2, 3)
    assert "R2、R3" in render([result], "2026-09-13 00:00")


# ---------- 3. 条数不一致 ----------


def test_count_mismatch_warns_and_blocks_pass():
    baseline = _baseline([_point(1, 100, True)] + [_point(i, None, False) for i in (2, 3, 4, 5)])
    rubric = _rubric(*[(f"R{i}", "normal", None) for i in (1, 2, 3, 4)])
    result = compute(baseline, rubric, [_card(refs=("R1",))])
    text = render([result], "2026-09-13 00:00")
    assert result.ratio == pytest.approx(1.0)          # 覆盖率本身满
    assert "⚠️ 条数不一致（人工 5 / agent 4）" in text
    assert not result.passing                          # 但 ⚠️ 一票否决


# ---------- 4. 分值不一致 ----------


def test_weight_mismatch_warns():
    baseline = _baseline([_point(1, 30), _point(2, 70), _point(3, 10)])
    rubric = _rubric(("R1", "normal", 30), ("R2", "normal", 20), ("R3", "normal", 10))
    result = compute(baseline, rubric, [_card(refs=("R1", "R2", "R3"))])
    text = render([result], "2026-09-13 00:00")
    assert "⚠️ 分值不一致（第 2 条：人工 70 / agent 20）" in text
    assert not result.passing


# ---------- 5. 均衡度公式 ----------


def test_balance_is_normalized_range():
    """§4.3 的真实基准：[6,9,6,6,3,3] → 1 − 6/33 = 0.818。"""
    baseline = _baseline([_point(1, 100)])
    rubric = _rubric(("R1", "normal", 100))
    cards = [_card(task_id=f"T{i}", hours=hours) for i, hours in enumerate([6, 9, 6, 6, 3, 3], 1)]
    result = compute(baseline, rubric, cards)
    assert result.balance == pytest.approx(0.818, abs=1e-3)


def test_balance_counts_ambiguous_cards_too():
    """模糊点建的卡照常计入均衡（D-40）—— 不筛 rubric_refs。"""
    baseline = _baseline([_point(1, 100)])
    rubric = _rubric(("R1", "normal", 100))
    cards = [_card(refs=("R1",), hours=2.0), _card(task_id="T2", refs=(), hours=8.0)]
    result = compute(baseline, rubric, cards)
    assert result.balance == pytest.approx(1 - 6 / 10)


# ---------- 6. 没有卡 ----------


def test_no_cards_gives_na_balance_and_zero_ratio():
    baseline = _baseline([_point(1), _point(2)])
    result = compute(baseline, _rubric(("R1", "normal", None), ("R2", "normal", None)), [])
    assert result.balance is None
    assert result.ratio == 0.0
    assert "N/A" in render([result], "2026-09-13 00:00")


def test_load_runs_missing_files_are_empty():
    """拒拆时主链路不落盘（D-49 ④）—— 缺文件当空，别炸。"""
    assert load_runs(EVAL_DIR / "runs" / "__不存在的doc__") == ([], [])


# ---------- 7. D-19 守卫 ----------


def test_no_import_of_loop_coverage_implementation():
    """`eval/` 不得复用 src/intelligence/coverage.py（D-19 / D-51）。"""
    offenders = []
    for path in sorted(EVAL_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "from src.intelligence.coverage import" in text or "import src.intelligence.coverage" in text:
            offenders.append(path.relative_to(EVAL_DIR).as_posix())
    assert offenders == []


# ---------- 8. 基线缺字段 ----------


def test_baseline_missing_point_field_names_file_and_field(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text(
        json.dumps(
            {
                "doc_id": "x",
                "title": "t",
                "source_file": "f.pdf",
                "points": [{"order": 1, "weight": 10, "label": "少了 decomposable"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(BaselineError) as exc:
        load_baseline(path)
    message = str(exc.value)
    assert "broken.json" in message
    assert "decomposable" in message


def test_baseline_missing_doc_field_names_field(tmp_path):
    path = tmp_path / "nodoc.json"
    path.write_text(
        json.dumps(
            {"doc_id": "x", "title": "t", "source_file": "f.pdf"}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    with pytest.raises(BaselineError) as exc:
        load_baseline(path)
    assert "points" in str(exc.value)


# ---------- 9. 入库的 5 份基线必须都能读 ----------


def test_shipped_baselines_all_load():
    ids = sorted(path.stem for path in BASELINE_DIR.glob("*.json"))
    assert ids == ["bim", "db", "marketing", "river", "sw"]
    for doc_id in ids:
        payload = load_baseline(BASELINE_DIR / f"{doc_id}.json")
        assert payload["doc_id"] == doc_id
        assert payload["points"]



# ---------- 10. 门③ 不能用子集声称（F6）----------


def _passing_result(doc_id="d0"):
    baseline = _baseline([_point(1, 100)], doc_id=doc_id)
    rubric = _rubric(("R1", "normal", 100))
    return compute(baseline, rubric, [_card(refs=("R1",))])


def test_single_doc_render_does_not_claim_the_gate():
    """F6：样本不足（D5 要 5 份）时只报子集成绩，不打门③结论。"""
    text = render([_passing_result()], "2026-09-13 00:00")
    assert "1/1 通过" in text
    assert "门③ ✅" not in text
    assert "样本不足" in text


def test_five_passing_docs_claim_the_gate():
    """五份齐且全过 → 才能打门③ ✅（回归）。"""
    results = [_passing_result(f"d{i}") for i in range(5)]
    text = render(results, "2026-09-13 00:00")
    assert "5/5 通过" in text
    assert "D5 门③ ✅" in text
