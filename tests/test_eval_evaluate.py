"""M8 评测入口（``eval/evaluate.py``）的单测 —— 守的是"**别把门③ 的唯一证据搞坏**"。

在此之前这个入口**零测试覆盖**（`tests/` 里只有 `eval/report.py` 的用例），
而它恰好是全项目唯一会覆盖 ``eval/report.md``、还会删 ``data/`` 产物的地方。

**离线**：不调 LLM、不联网、不读真作业书；``main_chain`` 一律换成 stub，
``eval/`` 的路径常量全部指到 ``tmp_path``。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval import evaluate


# ---------- 夹具：把 eval/ 的路径常量整体搬到 tmp_path ----------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """``eval/evaluate.py`` 的路径常量与外部依赖全部替换成 tmp_path 版本。"""
    baseline_dir = tmp_path / "baseline"
    runs_dir = tmp_path / "runs"
    data_dir = tmp_path / "data"
    report = tmp_path / "report.md"
    bak = tmp_path / "report.md.bak"
    for path in (baseline_dir, runs_dir, data_dir):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(evaluate, "BASELINE_DIR", baseline_dir)
    monkeypatch.setattr(evaluate, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(evaluate, "REPORT_MD", report)
    monkeypatch.setattr(evaluate, "REPORT_BAK", bak)
    monkeypatch.setattr(evaluate, "load_config", lambda: SimpleNamespace(data_dir=data_dir))
    return SimpleNamespace(
        tmp=tmp_path,
        baseline_dir=baseline_dir,
        runs_dir=runs_dir,
        data_dir=data_dir,
        report=report,
        bak=bak,
    )


def _use_docs(monkeypatch, docs):
    monkeypatch.setattr(evaluate, "load_docs", lambda path=None: list(docs))


def _write_baseline(env, doc_id="d1", points=None):
    payload = {
        "doc_id": doc_id,
        "title": f"作业书 {doc_id}",
        "source_file": "x.pdf",
        "points": points
        if points is not None
        else [{"order": 1, "weight": 100, "decomposable": True, "label": "点1"}],
    }
    env.baseline_dir.mkdir(parents=True, exist_ok=True)
    (env.baseline_dir / f"{doc_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return payload


def _rubric():
    return [
        {
            "id": "R1",
            "quote": "原文",
            "observable": "可核对",
            "status": "normal",
            "weight": 100,
        }
    ]


def _cards():
    return [
        {
            "task_id": "T1",
            "module_name": "模块",
            "rubric_refs": ["R1"],
            "effort_hours": 4.0,
            "deliverable": "交付物",
            "acceptance": "验收",
        }
    ]


def _write_snapshot(env, doc_id="d1", *, rubric=True, cards=True, manifest=True):
    target = env.runs_dir / doc_id
    target.mkdir(parents=True, exist_ok=True)
    if rubric:
        (target / "rubric.json").write_text(
            json.dumps(_rubric(), ensure_ascii=False), encoding="utf-8"
        )
    if cards:
        (target / "cards.json").write_text(
            json.dumps(_cards(), ensure_ascii=False), encoding="utf-8"
        )
    if manifest:
        (target / evaluate.SNAPSHOT_MANIFEST).write_text(
            json.dumps({"doc_id": doc_id, "taken_at": "t", "files": ["rubric.json"]}),
            encoding="utf-8",
        )
    return target


def _write_products(data_dir, tag: str):
    """stub 主链路"跑出来的"产物；``tag`` 用来区分新旧。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in evaluate.SNAPSHOT_FILES:
        (data_dir / name).write_text(f'["{tag}"]', encoding="utf-8")


def _write_data_products(data_dir, tag):
    """写入三份能被 load_runs 读懂的产物（评分点 + 卡片）。"""
    data_dir.mkdir(parents=True, exist_ok=True)

    class _RubricPoint:
        def __init__(self, **kw):
            self.__dict__.update(kw)

        def to_dict(self):
            return dict(self.__dict__)

    (data_dir / "rubric.json").write_text(
        json.dumps(_rubric(), ensure_ascii=False), encoding="utf-8"
    )
    (data_dir / "cards.json").write_text(
        json.dumps(_cards(), ensure_ascii=False), encoding="utf-8"
    )
    (data_dir / "assignment.json").write_text(
        json.dumps({"tag": tag}, ensure_ascii=False), encoding="utf-8"
    )


# ---------- 单元：两个新判据 ----------


def test_missing_doc_files_names_each_doc(tmp_path):
    docs = [
        {"doc_id": "ok", "file": str(tmp_path / "有.pdf")},
        {"doc_id": "gone", "file": str(tmp_path / "没有.pdf")},
        {"doc_id": "blank", "file": ""},
    ]
    (tmp_path / "有.pdf").write_text("x", encoding="utf-8")
    missing = evaluate.missing_doc_files(docs)
    assert len(missing) == 2
    assert any("gone" in item for item in missing)
    assert any("blank" in item for item in missing)


def test_snapshot_ready_needs_manifest_or_legacy_products(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert evaluate.snapshot_ready(tmp_path / "从来没有") is False
    assert evaluate.snapshot_ready(empty_dir) is False          # 空目录 ≠ 跑过

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "rubric.json").write_text("[]", encoding="utf-8")
    assert evaluate.snapshot_ready(legacy) is True              # 老格式仍然认

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / evaluate.SNAPSHOT_MANIFEST).write_text("{}", encoding="utf-8")
    assert evaluate.snapshot_ready(fresh) is True               # 拒拆（产物为空）也算跑过


# ---------- 闸 1：作业书不在本机 ----------


def test_missing_assignment_file_stops_before_touching_data(env, monkeypatch, capsys):
    """核心回归：以前会先删 data/ 三份产物、再崩在 PyMuPDF 上。"""
    _use_docs(monkeypatch, [{"doc_id": "d1", "file": str(env.tmp / "不存在.pdf")}])
    _write_baseline(env)
    _write_products(env.data_dir, "真机产物")
    env.report.write_text("OLD 门③ 证据", encoding="utf-8")

    calls = []
    monkeypatch.setattr(evaluate, "main_chain", lambda argv: calls.append(argv) or 0)

    assert evaluate.main([]) == 2

    assert calls == []                                          # 主链路没跑
    for name in evaluate.SNAPSHOT_FILES:                        # 产物一个没少
        assert (env.data_dir / name).read_text(encoding="utf-8") == '["真机产物"]'
    assert env.report.read_text(encoding="utf-8") == "OLD 门③ 证据"
    assert not env.bak.exists()                                 # 连备份都没必要产生
    assert "没开工" in capsys.readouterr().err


# ---------- 闸 2：--no-rerun 缺快照 ----------


def test_no_rerun_without_snapshots_returns_2_and_keeps_evidence(env, monkeypatch, capsys):
    """核心回归：以前会把 5/5 ✅ 覆盖成 0/5 ❌ 并退 0。"""
    doc_file = env.tmp / "有.pdf"
    doc_file.write_text("x", encoding="utf-8")
    _use_docs(monkeypatch, [{"doc_id": "d1", "file": str(doc_file)}])
    _write_baseline(env)
    env.report.write_text("OLD 门③ 证据", encoding="utf-8")

    def _never():
        raise AssertionError("--no-rerun 不该去读 .env / 配置")

    monkeypatch.setattr(evaluate, "load_config", _never)
    monkeypatch.setattr(
        evaluate, "main_chain", lambda argv: pytest.fail("--no-rerun 不该跑主链路")
    )

    assert evaluate.main(["--no-rerun"]) == 2

    assert env.report.read_text(encoding="utf-8") == "OLD 门③ 证据"   # 证据原样
    assert not env.bak.exists()
    assert "快照" in capsys.readouterr().err


def test_no_rerun_rejects_empty_snapshot_dir(env, monkeypatch):
    doc_file = env.tmp / "有.pdf"
    doc_file.write_text("x", encoding="utf-8")
    _use_docs(monkeypatch, [{"doc_id": "d1", "file": str(doc_file)}])
    _write_baseline(env)
    (env.runs_dir / "d1").mkdir(parents=True)                   # 目录在、但里面什么都没有
    assert evaluate.main(["--no-rerun"]) == 2


# ---------- 正常路径仍然工作 ----------


def test_no_rerun_with_snapshots_writes_report_and_returns_0(env, monkeypatch):
    doc_file = env.tmp / "有.pdf"
    doc_file.write_text("x", encoding="utf-8")
    _use_docs(monkeypatch, [{"doc_id": "d1", "file": str(doc_file)}])
    _write_baseline(env)
    _write_snapshot(env, "d1")
    env.report.write_text("OLD 门③ 证据", encoding="utf-8")
    monkeypatch.setattr(
        evaluate, "main_chain", lambda argv: pytest.fail("--no-rerun 不该跑主链路")
    )

    assert evaluate.main(["--no-rerun"]) == 0

    text = env.report.read_text(encoding="utf-8")
    assert "`--no-rerun`" in text                               # 来源行进了报告
    assert "1/1 通过" in text
    assert env.bak.read_text(encoding="utf-8") == "OLD 门③ 证据"   # 旧证据留底
    assert not (env.report.parent / (env.report.name + ".tmp")).exists()


def test_full_run_backs_up_previous_products_before_resetting(env, monkeypatch):
    doc_file = env.tmp / "有.pdf"
    doc_file.write_text("x", encoding="utf-8")
    _use_docs(monkeypatch, [{"doc_id": "d1", "file": str(doc_file)}])
    _write_baseline(env)
    _write_products(env.data_dir, "上一轮真机产物")
    env.report.write_text("OLD 门③ 证据", encoding="utf-8")

    def _stub(argv):
        assert argv == ["--file", str(doc_file)]
        _write_data_products(env.data_dir, "本轮")
        return 0

    monkeypatch.setattr(evaluate, "main_chain", _stub)

    assert evaluate.main([]) == 0

    backups = sorted((env.data_dir / evaluate.DATA_BACKUP_DIR).glob("*/*"))
    assert {path.name for path in backups} == set(evaluate.SNAPSHOT_FILES)
    for path in backups:                                        # 备份里是旧内容
        assert path.read_text(encoding="utf-8") == '["上一轮真机产物"]'
    assert "全量重跑" in env.report.read_text(encoding="utf-8")
    assert env.bak.read_text(encoding="utf-8") == "OLD 门③ 证据"
    # 快照清单写下了"当时实际存在的产物"
    manifest = json.loads(
        (env.runs_dir / "d1" / evaluate.SNAPSHOT_MANIFEST).read_text(encoding="utf-8")
    )
    assert manifest["doc_id"] == "d1"
    assert set(manifest["files"]) == set(evaluate.SNAPSHOT_FILES)


# ---------- 退出码语义 ----------


def test_exit_code_1_when_a_doc_does_not_pass(env, monkeypatch):
    doc_file = env.tmp / "有.pdf"
    doc_file.write_text("x", encoding="utf-8")
    _use_docs(monkeypatch, [{"doc_id": "d1", "file": str(doc_file)}])
    # 人工 2 条、agent 只 1 条 → 条数不一致 ⚠️ → 该份不过
    _write_baseline(
        env,
        points=[
            {"order": 1, "weight": 50, "decomposable": True, "label": "点1"},
            {"order": 2, "weight": 50, "decomposable": True, "label": "点2"},
        ],
    )
    _write_snapshot(env, "d1")

    assert evaluate.main(["--no-rerun"]) == 1                   # 以前恒为 0


def test_exit_code_2_when_main_chain_hard_fails(env, monkeypatch):
    doc_file = env.tmp / "有.pdf"
    doc_file.write_text("x", encoding="utf-8")
    _use_docs(monkeypatch, [{"doc_id": "d1", "file": str(doc_file)}])
    _write_baseline(env)
    monkeypatch.setattr(evaluate, "main_chain", lambda argv: 2)  # 硬失败：什么都不落盘

    assert evaluate.main([]) == 2
    text = env.report.read_text(encoding="utf-8")
    assert "主链路退出码：2" in text                             # 报告如实记下 rc
