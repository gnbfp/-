"""M7 甘特图渲染 —— 纯模板，**零 LLM**（B2 / S12）。

依据：`requirements.md` §4（M7）、§6.0（作业级 `deadline`）/ §6.4、D-23 / D-49 / D-65，
M6+M7 方案 §3.1。

需求里**没有**"每张卡什么时候开始"这个字段（任务卡只有 `effort_hours`，没有起止日期），
所以时间条的位置是**示意排期**（D-65）：按 `task_id` 顺序把 [今天, deadline] 平均铺开，
一张卡一行，颜色按 `assignee` 分组。它回答的是"谁大概在什么时候做哪张卡"，不是承诺。

三件必须做的事（踩过就知道疼）：
  * `matplotlib.use("Agg")` —— 无界面后端，命令行 / 服务器下不弹窗；
  * **显式设中文字体** —— 默认字体没有汉字，不设整张图的中文全是方框；
  * `deadline` 为空 / 脏 → **不画截止线**、图下注明"截止未标注"，且不许报错（D-49 允许空）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

import matplotlib

matplotlib.use("Agg")                       # 必须在 pyplot 之前选后端

import matplotlib.dates as mdates            # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure         # noqa: E402
from matplotlib.patches import FancyArrowPatch, Patch  # noqa: E402

from src.models import (  # noqa: E402
    AssignmentMeta,
    AssignmentRecord,
    Roster,
    TaskCard,
    parse_deadline,
)

__all__ = ["Bar", "plan_bars", "render_gantt"]

_ONE_DAY = timedelta(days=1)
# 截止认不出来时的示意窗（工程默认，可推翻）：给 7 天，图至少画得出来。
_DEFAULT_SPAN_DAYS = 7
# 按 assignee 分组上色（同一个人同一色）。
_PALETTE = (
    "#4c72b0",
    "#dd8452",
    "#55a868",
    "#c44e52",
    "#8172b3",
    "#937860",
    "#da8bc3",
    "#8c8c8c",
)
# 中文字体在前；DejaVu Sans 兜底补中文字体缺的符号。
CJK_FONTS = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]

# matplotlib 的 Figure 不是线程安全的（必修 C）：M7 报告跑在 pipeline 线程里，
# 与别的渲染并发会串图。一把锁罩住“建图 → 落盘”整段。
_RENDER_LOCK = threading.Lock()


def _apply_cjk_font() -> None:
    """把中文字体设进 rcParams —— 不设就是满屏方框（Windows 自带 msyh.ttc）。"""
    matplotlib.rcParams["font.sans-serif"] = list(CJK_FONTS)
    matplotlib.rcParams["axes.unicode_minus"] = False   # 负号也别退回方框


_apply_cjk_font()


@dataclass(frozen=True)
class Bar:
    """一张卡画在图上的那根时间条（**示意**，见 D-65）。"""

    task_id: str
    module_name: str
    assignee: str
    start: datetime
    end: datetime
    done: bool
    depends_on: tuple[str, ...] = ()


def plan_bars(
    cards: Sequence[TaskCard],
    assignments: Sequence[AssignmentRecord],
    meta: AssignmentMeta | None,
    now: datetime | None = None,
) -> tuple[list[Bar], datetime | None]:
    """纯排期计算：``(每张卡的时间条, deadline)``；``deadline`` 为 ``None`` = 截止未标注。

    **示意排期（D-65）**：没有每张卡的真实起止，就把 ``[今天, deadline]`` 按 ``task_id``
    顺序平均切开、一张卡一格。截止认不出来 → 用 ``_DEFAULT_SPAN_DAYS`` 天的示意窗。
    判定与渲染分开，是为了这段逻辑能离线单测（不用读像素）。
    """
    moment = now or datetime.now()
    deadline = parse_deadline(meta)
    ordered = sorted(cards or (), key=lambda card: card.task_id)
    records = {record.task_id: record for record in (assignments or ())}
    if not ordered:
        return [], deadline

    if deadline is None:
        anchor_end = moment + timedelta(days=_DEFAULT_SPAN_DAYS)
    elif deadline > moment:
        anchor_end = deadline
    else:
        anchor_end = moment + _ONE_DAY          # 已逾期：把条压到今天附近，截止线画在左边

    step = (anchor_end - moment) / len(ordered)
    bars: list[Bar] = []
    for index, card in enumerate(ordered):
        record = records.get(card.task_id)
        bars.append(
            Bar(
                task_id=card.task_id,
                module_name=card.module_name or card.task_id,
                assignee=(record.assignee if record else "") or "",
                start=moment + step * index,
                end=moment + step * (index + 1),
                done=bool(record and record.completed_at),
                depends_on=tuple(card.depends_on or ()),
            )
        )
    return bars, deadline


def render_gantt(
    cards: Sequence[TaskCard],
    assignments: Sequence[AssignmentRecord],
    meta: AssignmentMeta | None,
    path: Path | str,
    roster: Roster | None = None,
) -> Path:
    """把示意甘特图落成 PNG，返回落盘路径。**纯渲染**：判定在 ``plan_bars()`` 里。

    ``roster`` 可选：只是为了图例 / 纵轴显示人名而不是 ``ou_xxx``（纯展示，不参与判定）。
    """
    # 字体在 import 时已设过一次（模块级 _apply_cjk_font()），函数里不再改全局 rcParams。
    bars, deadline = plan_bars(cards, assignments, meta)

    height = max(2.6, 0.55 * len(bars) + 1.8)
    # Figure + Agg canvas 不注册到全局 pyplot（必修 C），锁罩住整段。
    _RENDER_LOCK.acquire()
    fig = None
    try:
        fig = Figure(figsize=(10, height))
        FigureCanvasAgg(fig)                # 挂在 fig 上，不注册到全局
        ax = fig.add_subplot(111)
        colors = _assignee_colors(bars)
        ys = list(range(len(bars)))
        for y, bar in zip(ys, bars):
            start = mdates.date2num(bar.start)
            width = max(mdates.date2num(bar.end) - start, 0.05)
            color = colors.get(bar.assignee, _PALETTE[-1])
            ax.barh(
                y,
                width,
                left=start,
                height=0.5,
                color=color if not bar.done else _darken(color),
                edgecolor="black" if bar.done else "white",
                linewidth=0.8,
            )
        _draw_dependencies(ax, bars, ys)

        ax.set_yticks(ys)
        ax.set_yticklabels([_label(bar, roster) for bar in bars], fontsize=9)
        ax.invert_yaxis()                       # 第一张卡画在最上面
        ax.set_title("执行甘特图（示意排期，非承诺）", fontsize=13)
        ax.set_xlabel("日期")
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        ax.grid(axis="x", linestyle=":", linewidth=0.6, alpha=0.5)

        if deadline is not None:
            ax.axvline(
                mdates.date2num(deadline),
                color="red",
                linestyle="--",
                linewidth=1.5,
                label=f"截止 {deadline:%Y-%m-%d %H:%M}",
            )
        note = "红色虚线 = 作业截止时间" if deadline is not None else "截止未标注（图上不画截止线）"
        ax.text(
            0.0,
            -0.18,
            f"注：{note}。时间条为示意排期（需求无每卡起止字段，见 D-65）。",
            transform=ax.transAxes,
            fontsize=8,
            color="#666666",
        )
        _draw_legend(ax, colors, roster)

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(target, dpi=140, bbox_inches="tight")
    finally:
        if fig is not None:
            fig.clear()                         # 手动清，不关会一直堆在内存里
        _RENDER_LOCK.release()
    return Path(path)


def _assignee_colors(bars: Sequence[Bar]) -> dict[str, str]:
    """同一个人同一个颜色；空 assignee（占位）用最后一色。"""
    colors: dict[str, str] = {}
    for bar in bars:
        if bar.assignee and bar.assignee not in colors:
            colors[bar.assignee] = _PALETTE[len(colors) % len(_PALETTE)]
    return colors


def _display(open_id: str, roster: Roster | None) -> str:
    for member in getattr(roster, "members", None) or ():
        if member.open_id == open_id and member.name:
            return member.name
    return (open_id[:8] if open_id else "未分配")


def _label(bar: Bar, roster: Roster | None) -> str:
    mark = " √" if bar.done else ""      # ✓(U+2713) 中文字体没有，用 √(U+221A)
    return f"{bar.task_id} {bar.module_name}｜{_display(bar.assignee, roster)}{mark}"


def _draw_dependencies(ax, bars: Sequence[Bar], ys: Sequence[int]) -> None:
    """``depends_on`` 依赖箭头：从前置卡右端指向后置卡左端（§3.1）。"""
    index = {bar.task_id: y for bar, y in zip(bars, ys)}
    by_id = {bar.task_id: bar for bar in bars}
    for bar, y in zip(bars, ys):
        for dependency in bar.depends_on:
            predecessor = by_id.get(dependency)
            if predecessor is None or dependency == bar.task_id:
                continue                        # 悬空依赖 / 自依赖：跳过，别画到图外
            ax.add_patch(
                FancyArrowPatch(
                    (mdates.date2num(predecessor.end), index[dependency]),
                    (mdates.date2num(bar.start), y),
                    arrowstyle="->",
                    mutation_scale=12,
                    color="#555555",
                    linewidth=1.0,
                    shrinkA=0,
                    shrinkB=0,
                )
            )


def _darken(color: str) -> str:
    """完成的卡加深色（§3.1）—— 手算 RGB 的 0.6 倍，省一个依赖。"""
    raw = color.lstrip("#")
    channels = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    return "#" + "".join(f"{int(value * 0.6):02x}" for value in channels)


def _draw_legend(ax, colors: dict[str, str], roster: Roster | None) -> None:
    if not colors:
        return
    handles = [
        Patch(facecolor=color, label=_display(open_id, roster))
        for open_id, color in colors.items()
    ]
    ax.legend(handles=handles, loc="lower right", fontsize=8, framealpha=0.85)