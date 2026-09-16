"""M8 复算 —— 覆盖率与均衡度的**独立**实现（D-19 / D-51）。

**故意不 import ``src/intelligence/coverage.py``**：M8 是"人工基线 vs agent 产物"的
裁判，复用同一份实现等于自己改自己的卷子（``coverage.py`` 的 docstring 也把这条写死了）。
有单测守着这条纪律 —— 见 ``tests/test_eval_report.py`` 的 D-19 守卫。

本模块**不调 LLM、不落盘、不判定**，只读 ``eval/baseline/*.json`` 与
``eval/runs/<doc_id>/*.json`` 算数并渲染。

口径（D-51）：
  * 分母 = **人工基线**里 ``decomposable=true`` 的点（**不看 agent 的 status**）；
  * 分子 = 被任一卡片 ``rubric_refs`` 引用、且在人工分母里的点；
  * 均衡度 = ``1 - (max-min)/sum``（不筛 rubric_refs）；``sum=0`` → ``N/A``；
  * 对齐 = 人工 ``order=n`` ↔ agent ``R{n}``，另做条数 / 分值两项校验；
  * 门③ = **逐份** ≥ 80% 且无 ⚠️（不用平均）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "BaselineError",
    "DocResult",
    "PASS_THRESHOLD",
    "GATE_MIN_DOCS",
    "load_baseline",
    "load_runs",
    "compute",
    "render",
]

PASS_THRESHOLD = 0.80          # 门③：逐份 ≥ 80% 且无 ⚠️（§4.4，工程默认）
GATE_MIN_DOCS = 5              # 门③是"前 5 份作业书"的门（D-51），样本不足就不能认可门③（F6）

_DOC_FIELDS = ("doc_id", "title", "source_file", "points")
_POINT_FIELDS = ("order", "weight", "decomposable", "label")


class BaselineError(ValueError):
    """人工基线格式不对。消息里必须点名**哪个文件、哪个字段**。"""


@dataclass(frozen=True)
class DocResult:
    """一份作业书的复算结果。"""

    doc_id: str
    title: str
    rc: int | None
    baseline_total: int
    baseline_decomposable: int
    agent_total: int
    agent_normal: int
    covered: int
    ratio: float
    balance: float | None
    card_count: int
    missing: tuple[int, ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...]
    card_summary: tuple[str, ...]

    @property
    def passing(self) -> bool:
        return self.ratio >= PASS_THRESHOLD and not self.warnings


def load_baseline(path: str | Path) -> dict[str, Any]:
    """读一份人工基线；字段缺失就报错点名（不刷 traceback）。"""
    path = Path(path)
    if not path.exists():
        raise BaselineError(f"{path}: 基线文件不存在")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BaselineError(f"{path}: 不是合法 JSON（{exc.msg}）") from None
    if not isinstance(payload, dict):
        raise BaselineError(f"{path}: 顶层必须是 JSON 对象")
    for name in _DOC_FIELDS:
        if name not in payload:
            raise BaselineError(f"{path}: 缺少字段 {name}")
    points = payload["points"]
    if not isinstance(points, list) or not points:
        raise BaselineError(f"{path}: points 必须是非空数组")
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise BaselineError(f"{path}: points[{index}] 必须是对象")
        for name in _POINT_FIELDS:
            if name not in point:
                raise BaselineError(f"{path}: points[{index}] 缺少字段 {name}")
    return payload


def load_runs(run_dir: str | Path) -> tuple[list[dict], list[dict]]:
    """读某个 doc 快照里的评分点与任务卡；缺文件当空列表（拒拆就是空）。"""
    run_dir = Path(run_dir)
    return _load_list(run_dir / "rubric.json"), _load_list(run_dir / "cards.json")


def _load_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise BaselineError(f"{path}: 顶层必须是数组")
    return payload


def compute(
    baseline: dict[str, Any],
    rubric: Sequence[dict],
    cards: Sequence[dict],
    *,
    rc: int | None = None,
) -> DocResult:
    """复算一份：人工基线 × agent 产物 → 覆盖率 + 均衡度 + 校验。**纯函数。**"""
    points = list(baseline["points"])
    warnings: list[str] = []
    notes: list[str] = []

    agent_by_id = {
        item.get("id"): item for item in rubric if isinstance(item, dict) and item.get("id")
    }

    # 校验 1：条数（对不上就没法逐条比，该份不算通过）
    if len(points) != len(rubric):
        warnings.append(f"⚠️ 条数不一致（人工 {len(points)} / agent {len(rubric)}）")

    # 校验 2：分值（提示"人工抄错或 agent 抽错行"）
    for point in points:
        agent = agent_by_id.get(f"R{point['order']}")
        if agent is None:
            continue
        human_weight, agent_weight = point.get("weight"), agent.get("weight")
        if human_weight is not None and agent_weight is not None and human_weight != agent_weight:
            warnings.append(
                f"⚠️ 分值不一致（第 {point['order']} 条：人工 {human_weight} / agent {agent_weight}）"
            )

    # 覆盖率：分母由人工基线定，分子看卡片实际引用（D-51 ①）
    referenced = {ref for card in cards for ref in (card.get("rubric_refs") or [])}
    denominator = [point for point in points if point.get("decomposable")]
    covered = [point for point in denominator if f"R{point['order']}" in referenced]
    ratio = len(covered) / len(denominator) if denominator else 0.0
    missing = tuple(
        point["order"] for point in denominator if f"R{point['order']}" not in referenced
    )

    # 说明：agent 判模糊、人工判可拆 —— M8 不采纳 agent（答辩要能解释"为什么数字不一样"）
    disagreed = [
        f"R{point['order']}"
        for point in points
        if point.get("decomposable")
        and agent_by_id.get(f"R{point['order']}") is not None
        and agent_by_id[f"R{point['order']}"].get("status") != "normal"
    ]
    if disagreed:
        notes.append(
            f"⚠️ 口径差异：agent 把 {'/'.join(disagreed)} 判为 ambiguous，"
            f"**M8 不采纳**，按人工分母 {len(denominator)} 计算"
        )

    hours = [float(card.get("effort_hours") or 0) for card in cards]
    total_hours = sum(hours)
    balance = None if total_hours <= 0 else 1 - (max(hours) - min(hours)) / total_hours

    return DocResult(
        doc_id=baseline["doc_id"],
        title=baseline["title"],
        rc=rc,
        baseline_total=len(points),
        baseline_decomposable=len(denominator),
        agent_total=len(rubric),
        agent_normal=sum(1 for item in rubric if item.get("status") == "normal"),
        covered=len(covered),
        ratio=ratio,
        balance=balance,
        card_count=len(cards),
        missing=missing,
        warnings=tuple(warnings),
        notes=tuple(notes),
        card_summary=tuple(
            f"{card.get('task_id')}→{'、'.join(card.get('rubric_refs') or [])}" for card in cards
        ),
    )


def render(results: Sequence[DocResult], generated_at: str, source: str = "") -> str:
    """渲染 ``eval/report.md``（门③ 的证据）。

    ``source``（可选）写进报告第二行：这份结论是**怎么来的**（全量重跑还是 ``--no-rerun``
    快照复算）。以前报告里零来源信息 —— 一份 ``--no-rerun`` 产物与一份真跑出来的产物
    长得一模一样，事后没人能分辨，也就没法核查。
    """
    lines = [
        f"# M8 覆盖率复算报告（生成于 {generated_at}）",
        "",
    ]
    if source:
        lines += [f"> 来源：{source}", ""]
    lines += [
        "| 作业书 | 人工可拆 | agent 覆盖 | 覆盖率 | 均衡度 | 卡数 | 校验 | 状态 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        lines.append(
            f"| {result.title} | {result.baseline_decomposable} | {result.covered} | "
            f"{result.ratio:.0%} | {_balance_text(result.balance)} | {result.card_count} | "
            f"{'；'.join(result.warnings) if result.warnings else 'ok'} | "
            f"{'✅' if result.passing else '❌'} |"
        )

    passed = sum(1 for result in results if result.passing)
    total = len(results)
    average = sum(result.ratio for result in results) / total if total else 0.0
    # 样本不足时只报子集成绩、**不打门③结论**：否则只跑 1 份也会印一个
    # "D5 门③ ✅"，把子集误报成过门（F6）。
    if total < GATE_MIN_DOCS:
        verdict = f"样本不足（D5 门③ 要求 {GATE_MIN_DOCS} 份），只报子集成绩，不给门③结论"
    else:
        verdict = f"D5 门③ {'✅' if passed == total else '❌'}"
    lines += [
        "",
        f"逐份判据（每份 ≥ {PASS_THRESHOLD:.0%} 且无 ⚠️）：**{passed}/{total} 通过** → {verdict}",
        f"平均覆盖率：{average:.0%}｜循环口径与评测口径的分母差异见 `eval/report.py` 顶部 docstring",
        "",
        "## 明细",
        "",
    ]
    for result in results:
        lines.append(f"### {result.title}（{result.doc_id}）")
        lines.append(
            f"- 人工：{result.baseline_total} 条（可拆 {result.baseline_decomposable}）｜"
            f"agent：{result.agent_total} 条（agent 自判 normal {result.agent_normal} 条）"
        )
        if result.rc is not None:
            lines.append(f"- 主链路退出码：{result.rc}（0 达标 / 1 拒拆或未达标 / 2 硬失败）")
        lines.extend(f"- {note}" for note in result.notes)
        lines.extend(f"- {warning}" for warning in result.warnings)
        lines.append(
            "- 未覆盖："
            + ("无" if not result.missing else "、".join(f"R{order}" for order in result.missing))
        )
        cards = "、".join(result.card_summary) if result.card_summary else "无"
        lines.append(f"- 卡片（{result.card_count} 张）：{cards}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _balance_text(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"