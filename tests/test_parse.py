"""M1 解析校验的单测：必填字段、quote 原文核对、模糊点不硬拆（§6.0 / §6.1 / D-03）。"""

import pytest

from src.intelligence.llm import LLMOutputError
from src.intelligence.parse import parse_assignment

TEXT = """编译原理课程设计作业书

一、作业要求
1. 实现一个词法分析器，占 40 分。
2. 撰写实验报告，要求内容充实、排版美观，占 30 分。
3. 课堂答辩，占 30 分。

截止时间：2026-09-19 23:59
交付形式：源码 + 报告
"""


class FakeClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def chat_json(self, system, user, parse, **kwargs):
        self.calls.append((system, user))
        return parse(self.payloads.pop(0))


def _payload(**overrides):
    payload = {
        "assignment": {
            "course": "编译原理",
            "title": "C 语言课程设计",
            "submission": "源码 + 报告",
            "deadline": "2026-09-19T23:59",
            "source_file": "作业书.txt",
        },
        "rubric": [
            {
                "id": "R1",
                "quote": "实现一个词法分析器，占 40 分",
                "weight": 40,
                "observable": "能对样例源码输出 token 序列",
                "status": "normal",
            },
            {
                "id": "R2",
                "quote": "要求内容充实、排版美观",
                "weight": 30,
                "observable": "无法客观核对",
                "status": "ambiguous",
            },
        ],
    }
    payload.update(overrides)
    return payload


def test_parses_meta_and_points():
    result = parse_assignment(TEXT, FakeClient([_payload()]))
    assert result.meta.course == "编译原理"
    assert [p.id for p in result.points] == ["R1", "R2"]
    assert result.points[1].status == "ambiguous"


def test_weight_string_is_coerced_to_number():
    payload = _payload()
    payload["rubric"][0]["weight"] = "40"
    result = parse_assignment(TEXT, FakeClient([payload]))
    assert result.points[0].weight == 40.0


def test_quote_must_be_verbatim_from_text():
    payload = _payload()
    payload["rubric"][0]["quote"] = "实现一个语法分析器"      # 原文没有这句
    with pytest.raises(LLMOutputError) as exc:
        parse_assignment(TEXT, FakeClient([payload]))
    assert "quote 不是作业书原文" in str(exc.value)


def test_quote_with_different_whitespace_still_matches():
    payload = _payload()
    payload["rubric"][0]["quote"] = "实现一个\n词法分析器，占 40 分"
    assert len(parse_assignment(TEXT, FakeClient([payload])).points) == 2


def test_missing_assignment_is_rejected():
    with pytest.raises(LLMOutputError) as exc:
        parse_assignment(TEXT, FakeClient([_payload(assignment=None)]))
    assert "assignment" in str(exc.value)


def test_empty_rubric_is_rejected():
    with pytest.raises(LLMOutputError):
        parse_assignment(TEXT, FakeClient([_payload(rubric=[])]))


def test_duplicate_ids_are_rejected():
    payload = _payload()
    payload["rubric"][1]["id"] = "R1"
    with pytest.raises(LLMOutputError) as exc:
        parse_assignment(TEXT, FakeClient([payload]))
    assert "重复" in str(exc.value)


def test_missing_weight_is_allowed():
    payload = _payload()
    payload["rubric"][0].pop("weight")
    assert parse_assignment(TEXT, FakeClient([payload])).points[0].weight is None
