"""M8 评测入口 —— 驱动主链路 → 快照 → 复算（D-51）。

    python -m eval.evaluate                 # 跑全部 5 份：调主链路 → 快照 → 复算 → 写 eval/report.md
    python -m eval.evaluate --only bim      # 只跑一份（调试用）
    python -m eval.evaluate --no-rerun      # 不重跑 LLM，只用 eval/runs/ 里的快照复算（省 token）

**复用不复制**：驱动主链路只调 ``src.main.main(["--file", ...])`` 这一个入口，
不重新编排 M1/M3（编排只有一处）。

**M8 自己零智能**：不调 LLM、不判定；产物只写 ``eval/report.md`` 与 ``eval/runs/``。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from src.config import ConfigError, load_config
from src.main import main as main_chain

from eval.report import (
    BaselineError,
    compute,
    load_baseline,
    load_runs,
    render,
)

__all__ = ["main", "load_docs"]

EVAL_DIR = Path(__file__).resolve().parent
DOCS_JSON = EVAL_DIR / "docs.json"
BASELINE_DIR = EVAL_DIR / "baseline"
RUNS_DIR = EVAL_DIR / "runs"
REPORT_MD = EVAL_DIR / "report.md"
SNAPSHOT_FILES = ("assignment.json", "rubric.json", "cards.json")


def load_docs(path: str | Path = DOCS_JSON) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{path}: 顶层必须是非空数组")
    return payload


def _reset_data(data_dir: Path) -> None:
    """跑之前清掉上一份的产物。

    主链路拒拆时**不落盘**（D-49 ④）：不清的话拒拆那份会快照到**上一份**的产物，
    覆盖率就假了。
    """
    for name in SNAPSHOT_FILES:
        (data_dir / name).unlink(missing_ok=True)


def _snapshot(doc_id: str, data_dir: Path) -> None:
    target = RUNS_DIR / doc_id
    target.mkdir(parents=True, exist_ok=True)
    for name in SNAPSHOT_FILES:
        source = data_dir / name
        if source.exists():
            shutil.copyfile(source, target / name)
        else:
            (target / name).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    # 报告里有 ✅/⚠️（设计稿 §6 要求，写进 report.md 是对的），但 GBK 控制台打不出来，
    # 直接 print 会 UnicodeEncodeError。只把控制台这一路改成 replace，不动文件内容。
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

    parser = argparse.ArgumentParser(description="M8 评测：主链路 → 快照 → 复算")
    parser.add_argument("--only", help="只跑一个 doc_id（调试用）")
    parser.add_argument(
        "--no-rerun", action="store_true", help="不重跑 LLM，只用 eval/runs/ 快照复算"
    )
    args = parser.parse_args(argv)

    try:
        docs = load_docs()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[配置错误] 读不了 docs.json：{exc}", file=sys.stderr)
        return 2

    if args.only:
        docs = [doc for doc in docs if doc.get("doc_id") == args.only]
        if not docs:
            print(f"[配置错误] docs.json 里没有 doc_id={args.only}", file=sys.stderr)
            return 2

    data_dir: Path | None = None
    if not args.no_rerun:
        try:
            data_dir = Path(load_config().data_dir)
        except ConfigError as exc:
            print(f"[配置错误] {exc}", file=sys.stderr)
            return 2

    results = []
    for doc in docs:
        doc_id = doc["doc_id"]
        try:
            baseline = load_baseline(BASELINE_DIR / f"{doc_id}.json")
        except BaselineError as exc:
            print(f"[基线错误] {exc}", file=sys.stderr)
            return 2
        rc = None
        if data_dir is not None:
            _reset_data(data_dir)
            rc = main_chain(["--file", doc["file"]])
            _snapshot(doc_id, data_dir)
        rubric, cards = load_runs(RUNS_DIR / doc_id)
        results.append(compute(baseline, rubric, cards, rc=rc))

    text = render(results, datetime.now().strftime("%Y-%m-%d %H:%M"))
    REPORT_MD.write_text(text, encoding="utf-8")
    print(text)
    print(f"[M8] 报告已写入 {REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
