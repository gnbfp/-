"""生成 ``assignment_demo.pdf`` —— **合成**演示作业书。

用法（仓库根目录）：

    python eval/samples/make_demo_assignment.py

只依赖 PyMuPDF。产物是给 D5 门②「主链路命令行跑通」用的可复现输入，
**不是真实作业书**（真实作业书按定稿 §3.9 收集，不入库）。
"""

from __future__ import annotations

from pathlib import Path

import fitz

OUT = Path(__file__).with_name("assignment_demo.pdf")

BODY = [
    "《数据结构》课程设计作业书",
    "",
    "一、任务要求",
    "1. 实现一个基于链表的通讯录管理程序，支持增删改查，占 40 分。",
    "2. 撰写课程设计报告，要求内容充实、排版美观，占 30 分。",
    "3. 课程设计答辩，现场演示程序并回答提问，占 30 分。",
    "",
    "二、交付形式：源代码 + 课程设计报告",
    "三、截止时间：2026-09-19 23:59",
    "",
]

TABLE = [["评分项", "分值"], ["功能实现", "40"], ["设计报告", "30"], ["现场答辩", "30"]]


def main() -> Path:
    doc = fitz.open()
    page = doc.new_page()

    y = 60
    for text in BODY:
        if text:
            page.insert_text(fitz.Point(60, y), text, fontsize=12, fontname="china-s")
        y += 20

    x0, col_w, row_h = 60, 150, 20
    top = y + 8
    for row in range(len(TABLE) + 1):
        yy = top + row * row_h
        page.draw_line(fitz.Point(x0, yy), fitz.Point(x0 + col_w * 2, yy))
    for col in range(3):
        xx = x0 + col * col_w
        page.draw_line(fitz.Point(xx, top), fitz.Point(xx, top + len(TABLE) * row_h))
    for row, cells in enumerate(TABLE):
        for col, value in enumerate(cells):
            page.insert_text(
                fitz.Point(x0 + col * col_w + 6, top + row * row_h + 14),
                value,
                fontsize=11,
                fontname="china-s",
            )

    doc.save(str(OUT))
    doc.close()
    return OUT


if __name__ == "__main__":
    print(f"written: {main()}")