"""M8 评测入口 —— 驱动主链路 → 快照 → 复算（D-51）。

    python -m eval.evaluate                 # 跑全部 5 份：调主链路 → 快照 → 复算 → 写 eval/report.md
    python -m eval.evaluate --only bim      # 只跑一份（调试用）
    python -m eval.evaluate --no-rerun      # 不重跑 LLM，只用 eval/runs/ 里的快照复算（省 token）

**复用不复制**：驱动主链路只调 ``src.main.main(["--file", ...])`` 这一个入口，
不重新编排 M1/M3（编排只有一处）。

**M8 自己零智能**：不调 LLM、不判定；产物只写 ``eval/report.md`` 与 ``eval/runs/``。

**四道闸（2026-09-16 加固）**：``eval/report.md`` 是门③ 的**唯一证据**，
而它以前是"跑到最后无条件覆盖、并且无条件退 0"，所以本入口在动它之前要先过闸：

1. **作业书文件不在本机 → 退 2，什么都不碰。** 以前是先把 ``data/`` 里那三份产物
   ``unlink`` 掉、再去打开作业书；作业书是别的机器上的绝对路径时，结果是
   **线上产物被删 + traceback**，而 ``report.md`` 只因为恰好崩在写盘之前才侥幸留下。
2. **``--no-rerun`` 而快照缺失 → 退 2，不写报告。** ``eval/runs/`` 被 ``.gitignore`` 排除
   （D-41：快照含真实作业书原文），所以"快照缺失"在新 clone 上是必现状态；
   以前会把 5/5 ✅ 静默改写成 0/5 ❌ 并退 0，唯一证据当场作废。
3. **写报告前先备份成 ``eval/report.md.bak``，并且原子替换。** 旧证据留一份、不留半截文件。
4. **退出码反映结果**：0 = 全过；1 = 有份未过；2 = 硬失败（前置闸不过 / 主链路 rc=2）。
   以前恒为 0，脚本与 CI 分不出"跑好了"和"跑坏了"。
"""

from __future__ import annotations

import argparse
import json
import os
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

__all__ = ["main", "load_docs", "missing_doc_files", "resolve_doc_file", "snapshot_ready"]

EVAL_DIR = Path(__file__).resolve().parent
DOCS_JSON = EVAL_DIR / "docs.json"
BASELINE_DIR = EVAL_DIR / "baseline"
RUNS_DIR = EVAL_DIR / "runs"
REPORT_MD = EVAL_DIR / "report.md"
REPORT_BAK = EVAL_DIR / "report.md.bak"
SNAPSHOT_FILES = ("assignment.json", "rubric.json", "cards.json")
# 快照清单（本次加固新增）：唯一能区分"这份跑过但拒拆"与"这份根本没跑过"的东西。
SNAPSHOT_MANIFEST = "_snapshot.json"
# 跑评测前清掉的线上产物先备份到这里（data/ 整个目录已被 .gitignore 排除）。
DATA_BACKUP_DIR = ".eval-backup"
STAMP_FORMAT = "%Y-%m-%d_%H%M%S"
# 真实作业书所在的目录（**不入库**）。`docs.json` 里只写文件名，目录从环境变量来 ——
# 这样公开仓库里不会出现任何人的本机路径（PII），换台机器也只需换个环境变量。
DOCS_DIR_ENV = "EVAL_DOCS_DIR"


def load_docs(path: str | Path = DOCS_JSON) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{path}: 顶层必须是非空数组")
    return payload


def resolve_doc_file(raw: str | Path) -> Path:
    """作业书文件 → 本机真实路径。

    绝对路径原样返回（兼容老用法）；相对路径（`docs.json` 里现在只存**文件名**）
    按环境变量 ``EVAL_DOCS_DIR`` 拼。**没配这个变量时**返回原样 —— 由闸 1 报
    "这些作业书在本机不存在"，并在提示里告诉人去配它。
    """
    path = Path(str(raw))
    if path.is_absolute():
        return path
    base = os.environ.get(DOCS_DIR_ENV, "").strip()
    return (Path(base) / path) if base else path


def missing_doc_files(docs: list[dict]) -> list[str]:
    """这些 doc 的作业书文件在本机不存在 —— 有则整个评测都不许开工。

    以前没有这道闸：``main_chain`` 里 ``Path(args.file)`` 直接交给 PyMuPDF，
    而 ``pymupdf.FileNotFoundError`` 继承 ``RuntimeError``（**不是 OSError**），
    ``src/main.py`` 的 ``except OSError`` 接不住 —— 于是"作业书不在本机"这条极常见的
    情形，表现是 traceback + 线上产物已被删。
    """
    missing: list[str] = []
    for doc in docs:
        raw = str(doc.get("file") or "")
        resolved = resolve_doc_file(raw) if raw else Path("")
        if not raw or not resolved.exists():
            missing.append(f"{doc.get('doc_id') or '(没写 doc_id)'} → {raw or '(没写 file)'}")
    return missing


def snapshot_ready(run_dir: str | Path) -> bool:
    """这份快照能不能拿来复算（``--no-rerun`` 的唯一判据）。

    认两种形态：
      * 有新清单 ``_snapshot.json`` —— 本次加固之后的产物；
      * 老格式（没有清单年代留下的快照）—— 至少有一份产物文件。

    光看"目录在不在"不够：拒拆那份本来就不落盘（D-49 ④），目录在但可能是空的，
    与"根本没跑过"分不开 —— 而这两种情况的结论完全不同（前者是真实成绩，后者是缺证据）。
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return False
    if (run_dir / SNAPSHOT_MANIFEST).exists():
        return True
    return any((run_dir / name).exists() for name in SNAPSHOT_FILES)


def _stamp() -> str:
    return datetime.now().strftime(STAMP_FORMAT)


def _backup_previous_products(data_dir: Path, stamp: str) -> Path | None:
    """把这一轮要清掉的线上产物整份复制到 ``data/.eval-backup/<时间戳>/``。

    为什么要备份：主链路**拒拆时不落盘**（D-49 ④），所以跑评测前必须先清掉上一份产物，
    否则拒拆那份会快照到上一份的产物、覆盖率就假了。但"清掉"以前是**直接删**——
    主链路随后一失败（作业书不在、LLM 挂了），真机那份产物就没了。
    备份成本是一次 ``copyfile``，恢复成本是重跑一遍真机。
    """
    existing = [name for name in SNAPSHOT_FILES if (data_dir / name).exists()]
    if not existing:
        return None
    target = data_dir / DATA_BACKUP_DIR / stamp
    target.mkdir(parents=True, exist_ok=True)
    for name in existing:
        shutil.copyfile(data_dir / name, target / name)
    return target


def _reset_data(data_dir: Path, stamp: str) -> Path | None:
    """清掉上一份产物（先备份）；返回备份目录，``None`` = 本来就没有旧产物。"""
    backup = _backup_previous_products(data_dir, stamp)
    for name in SNAPSHOT_FILES:
        (data_dir / name).unlink(missing_ok=True)
    return backup


def _snapshot(doc_id: str, data_dir: Path, stamp: str) -> None:
    """把这一份的产物快照到 ``eval/runs/<doc_id>/``，并写清单。

    清单记下**当时实际存在**的产物：``--no-rerun`` 之后要靠它判断"这份跑过没有"。
    """
    target = RUNS_DIR / doc_id
    target.mkdir(parents=True, exist_ok=True)
    present: list[str] = []
    for name in SNAPSHOT_FILES:
        source = data_dir / name
        if source.exists():
            shutil.copyfile(source, target / name)
            present.append(name)
        else:
            (target / name).unlink(missing_ok=True)
    (target / SNAPSHOT_MANIFEST).write_text(
        json.dumps(
            {"doc_id": doc_id, "taken_at": stamp, "files": present},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _source_text(*, no_rerun: bool, count: int) -> str:
    """报告里的来源行 —— 让"这份报告是怎么来的"留在证据里（以前是一份无来源的表）。"""
    mode = (
        "`--no-rerun`（只用 `eval/runs/` 快照复算，**未重跑主链路**）"
        if no_rerun
        else "全量重跑（含主链路，快照已刷新）"
    )
    return f"{mode}｜份数 {count}"


def _write_report(text: str) -> None:
    """先备份旧报告、再原子替换 —— 门③ 的证据不许被写坏，也不该被悄悄覆盖掉。"""
    if REPORT_MD.exists():
        shutil.copyfile(REPORT_MD, REPORT_BAK)
    tmp = REPORT_MD.with_name(REPORT_MD.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, REPORT_MD)


def _exit_code(results) -> int:
    """0 = 全过；1 = 有份未过；2 = 主链路硬失败（rc=2）。"""
    if any(result.rc == 2 for result in results):
        return 2
    return 0 if all(result.passing for result in results) else 1


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

    if args.no_rerun:
        # 闸 2：复算要有快照。缺快照时**一个字都不许写** —— 以前这里会把
        # 5/5 ✅ 覆盖成 0/5 ❌ 并退 0，等于把唯一证据改成反证。
        # 这条只在 --no-rerun 上查：全量重跑本来就不看快照（它是生成快照的那一方）。
        missing_runs = [
            doc["doc_id"] for doc in docs if not snapshot_ready(RUNS_DIR / doc["doc_id"])
        ]
        if missing_runs:
            print(
                f"[前置失败] --no-rerun 要读快照，但这些快照不在：{'、'.join(missing_runs)}",
                file=sys.stderr,
            )
            print(
                f"           期望位置：{RUNS_DIR / '<doc_id>' / SNAPSHOT_MANIFEST}"
                "（eval/runs/ 被 .gitignore 排除，新 clone 上必然为空）",
                file=sys.stderr,
            )
            print(
                "           → 先跑一次完整的 `python -m eval.evaluate` 生成快照；"
                "本次没开工，eval/report.md 未改动。",
                file=sys.stderr,
            )
            return 2

    data_dir: Path | None = None
    if not args.no_rerun:
        # 闸 1：作业书文件必须在，**且必须在动 data/ 之前查**。
        missing = missing_doc_files(docs)
        if missing:
            print("[前置失败] 这些作业书在本机不存在：", file=sys.stderr)
            for item in missing:
                print(f"           {item}", file=sys.stderr)
            print(
                "           → 评测没开工：data/ 里的产物与 eval/report.md 都没动。"
                f"（docs.json 里只写文件名，请把真实作业书目录配到环境变量 {DOCS_DIR_ENV}，"
                "写进 .env 即可 —— .env 已被 gitignore）",
                file=sys.stderr,
            )
            return 2
        try:
            data_dir = Path(load_config().data_dir)
        except ConfigError as exc:
            print(f"[配置错误] {exc}", file=sys.stderr)
            return 2

    stamp = _stamp()
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
            backup = _reset_data(data_dir, stamp)
            if backup is not None:
                print(f"[M8] 上一轮产物已备份到 {backup}")
            rc = main_chain(["--file", str(resolve_doc_file(doc["file"]))])
            _snapshot(doc_id, data_dir, stamp)
        rubric, cards = load_runs(RUNS_DIR / doc_id)
        results.append(compute(baseline, rubric, cards, rc=rc))

    text = render(
        results,
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        source=_source_text(no_rerun=args.no_rerun, count=len(docs)),
    )
    _write_report(text)
    print(text)
    print(f"[M8] 报告已写入 {REPORT_MD}")
    return _exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
