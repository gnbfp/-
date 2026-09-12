"""M1 输入解析 —— 作业书纯文本 → 作业元信息 + 评分点清单（LLM 调用点 1/3）。

依据：requirements.md §6.0 / §6.1 / §7.5、docs/ARCHITECTURE.md §5、定稿文档 §3.2、
D-03 / D-15 / D-21。

LLM 只负责"生成"，下列判定全部在代码里（B8）：
  * 必填字段是否齐、取值是否合法（``models.validate()``）。
  * **``quote`` 必须是作业书原文**——归一化空白后做子串校验。这是防 LLM 编造
    评分点的唯一硬手段（§11 风险表：编造 ⇒ 覆盖率虚高、M8 复算对不上）。

本模块不写盘：产物交回调用方（CLI / gateway）落 ``data/``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.intelligence.llm import LLMClient, LLMOutputError, as_number
from src.models import AssignmentMeta, RubricPoint, SchemaError

__all__ = ["ParsedAssignment", "parse_assignment", "M1_SYSTEM"]

M1_SYSTEM = """你是小组作业机器人里的「M1 输入解析」模块，唯一任务是把作业书文本变成结构化 JSON。
只输出 JSON 对象本身，不要解释、不要 markdown 代码块。

输出结构：
{
  "assignment": {"course": "课程名", "title": "作业标题",
                 "submission": "交付形式", "deadline": "YYYY-MM-DDTHH:MM"},
  "rubric": [
    {"id": "R1", "quote": "作业书原文片段", "weight": 40,
     "observable": "可观察的验收描述", "status": "normal"}
  ]
}

关于输入文本：它可能来自 PDF / Word 抽取的正文；表格被单独抽出，用一行
"=== 表格区域 ===" 标记，其下每行形如 "- 列名：值｜列名：值"。
评分标准常常就是这样一张表。

【评分点是什么 —— 最重要】
* 只收「评分标准 / 成绩评定 / 考核方式 / 评分细则 / 评分表 / 评分依据」这类小节里的评分项，
  以及全文中其他带明确分值（如"30 分""30%"）的评分项。
* 下列内容一律不是评分点，即使原文写了"必须""应""需"：
  交付/提交要求、格式要求、字数页数、页眉页脚、装订方式、学术诚信与抄袭条款、
  参考文献规范、技术选型建议、参考资料链接、课程目的与课程目标、分组要求、选题建议。
* 一条评分点 = 一个评分项：表格里的一行、编号的一段、或并列的一个分项。
  同一句里并列的多个评分项必须各自出一条。例如原文
  "模型的完整性（模型30 分）、格式的规范性（满分10 分）、方案的科学性（60 分……）"
  要出 3 条，而不是 1 条。同一评分项不许重复出，也不许把一句话切成多条。

硬规则：
1. quote 必须是上面文本里逐字出现的原文片段，不许改写、概括、翻译或拼接。
2. weight 取该评分项的分值："30 分" → 30，"30%" → 30，"满分10 分" → 10。
   该评分项确实没有标分值时才填 null。绝不编造。
3. 无法客观核对的要求（如"内容充实""排版美观"）→ status 填 "ambiguous"；
   其余填 "normal"。
4. 先通读全文，把评分标准小节找全；找到就必须一个不漏地抽出来。
   确实全文都没有评分标准 → rubric 返回空数组 []，不要用正文要求凑数。
5. id 从 R1 起连续编号，不要重复。
6. 找不到的字段填空字符串。绝不编造。
7. deadline 用 "YYYY-MM-DDTHH:MM"；原文没有明确截止时间就留空字符串。
8. 只做解析：不要拆任务、不要提改进建议、不要评价作业书。"""


@dataclass(frozen=True)
class ParsedAssignment:
    """M1 产出：元信息 + 评分点。"""

    meta: AssignmentMeta
    points: tuple[RubricPoint, ...]


def parse_assignment(
    text: str, client: LLMClient, *, source_file: str = ""
) -> ParsedAssignment:
    """单次 LLM 调用（含契约内的自动重试），返回已校验的解析结果。

    ``source_file`` 由调用方（CLI / gateway）传真实文件名：它描述的是**这份文件**，
    属于程序已知的事实，不该让 LLM 从正文里猜。
    """

    def parse(payload) -> ParsedAssignment:
        return _validate(text, payload, source_file)

    user = "作业书文本如下：\n\n" + text
    return client.chat_json(M1_SYSTEM, user, parse)


def _normalize(value: str) -> str:
    """去掉所有空白，绕开 PDF 换行 / 空格导致的"看起来不一样"。"""
    return re.sub(r"\s+", "", value or "")


def _validate(text: str, payload, source_file: str = "") -> ParsedAssignment:
    problems: list[str] = []
    if not isinstance(payload, dict):
        raise LLMOutputError(f"顶层必须是 JSON 对象，得到 {type(payload).__name__}")

    meta = _validate_meta(payload.get("assignment"), problems, source_file)
    points = _validate_points(payload.get("rubric"), text, problems)

    if problems:
        raise LLMOutputError("；".join(problems))
    return ParsedAssignment(meta=meta, points=tuple(points))


def _validate_meta(
    raw, problems: list[str], source_file: str = ""
) -> AssignmentMeta | None:
    if not isinstance(raw, dict):
        problems.append("缺 assignment 对象")
        return None
    data = dict(raw)
    if source_file:
        data["source_file"] = source_file
    try:
        meta = AssignmentMeta.from_dict(data)
        meta.validate()
        return meta
    except (SchemaError, TypeError, ValueError) as exc:
        problems.append(f"assignment 字段不合法：{exc}")
        return None


def _validate_points(raw, text: str, problems: list[str]) -> list[RubricPoint]:
    """校验评分点数组。**空数组合法**（作业书里没找到评分标准，D-48）：

    放行后不会去碰 LLM 重试 —— M3 的 ``check()`` 直接拒拆、gateway 回
    「没找到评分标准」，绝不拿正文要求凑数。只有"根本不是数组"才算问题。
    """
    if not isinstance(raw, list):
        problems.append("rubric 必须是数组")
        return []

    haystack = _normalize(text)
    points: list[RubricPoint] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"rubric[{index}] 不是对象")
            continue
        data = dict(item)
        if "weight" in data:
            data["weight"] = as_number(data["weight"])
        try:
            point = RubricPoint.from_dict(data)
            point.validate()
        except (SchemaError, TypeError, ValueError) as exc:
            problems.append(f"rubric[{index}] 字段不合法：{exc}")
            continue
        if _normalize(point.quote) not in haystack:
            problems.append(
                f"rubric[{index}]({point.id}) 的 quote 不是作业书原文：{point.quote[:40]!r}"
            )
            continue
        points.append(point)

    ids = [p.id for p in points]
    duplicates = sorted({pid for pid in ids if ids.count(pid) > 1})
    if duplicates:
        problems.append(f"评分点 id 重复：{duplicates}")
    return points
