"""M2 候选方向生成单测（方案 §4 组 1）。全程离线：用假 LLM 客户端，零 token。"""

import pytest

from src.intelligence.direction import TITLE_MAX, generate_directions
from src.intelligence.llm import LLMError, LLMOutputError
from src.models import RubricPoint


class FakeLLM:
    """只实现 ``chat_json``：把 payload 直接交给 ``parse``，让校验异常原样冒出来。

    这样测的是 ``_validate_directions`` 的判定，不掺 ``chat_json`` 自己的重试逻辑。
    """

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_json(self, system, user, parse, *, temperature=0.2, max_retries=2):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        assert "M2 方向候选" in system          # 用的是对的 system prompt
        return parse(self.payload)


def _points():
    return [
        RubricPoint(id="R1", quote="系统方案设计", observable="有方案文档"),
        RubricPoint(id="R2", quote="系统实现", observable="可运行"),
        RubricPoint(id="R3", quote="报告文档", observable="有报告"),
    ]


def _payload(*titles, refs=("R1",), note="对上 R1 系统方案"):
    return {
        "directions": [
            {"id": index + 1, "title": title, "note": note, "rubric_refs": list(refs)}
            for index, title in enumerate(titles)
        ]
    }


# ---------- 条数：必须 2–3（§2.1）----------


def test_two_directions_pass():
    result = generate_directions(_points(), None, FakeLLM(_payload("题目甲", "题目乙")))
    assert result.ok
    assert [d.id for d in result.directions] == [1, 2]
    assert result.directions[0].title == "题目甲"
    assert result.failures == ()


def test_three_directions_pass():
    result = generate_directions(_points(), None, FakeLLM(_payload("甲", "乙", "丙")))
    assert result.ok
    assert [d.id for d in result.directions] == [1, 2, 3]


def test_one_direction_is_rejected_with_a_reason():
    with pytest.raises(LLMOutputError) as excinfo:
        generate_directions(_points(), None, FakeLLM(_payload("只有一个")))
    assert "2–3" in str(excinfo.value)


def test_four_directions_are_rejected_with_a_reason():
    with pytest.raises(LLMOutputError) as excinfo:
        generate_directions(_points(), None, FakeLLM(_payload("甲", "乙", "丙", "丁")))
    assert "2–3" in str(excinfo.value)


# ---------- 字段级校验 ----------


def test_unknown_rubric_ref_is_rejected():
    with pytest.raises(LLMOutputError) as excinfo:
        generate_directions(_points(), None, FakeLLM(_payload("甲", "乙", refs=("R9",))))
    assert "不存在的评分点" in str(excinfo.value)


def test_empty_title_is_rejected():
    with pytest.raises(LLMOutputError) as excinfo:
        generate_directions(_points(), None, FakeLLM(_payload("甲", "")))
    assert "title 不能为空" in str(excinfo.value)


def test_overlong_title_is_rejected():
    long_title = "题" * (TITLE_MAX + 1)
    with pytest.raises(LLMOutputError) as excinfo:
        generate_directions(_points(), None, FakeLLM(_payload("甲", long_title)))
    assert "title 超过" in str(excinfo.value)


def test_overlong_note_is_rejected():
    long_note = "说" * 61
    with pytest.raises(LLMOutputError) as excinfo:
        generate_directions(
            _points(), None, FakeLLM(_payload("甲", "乙", note=long_note))
        )
    assert "note 超过" in str(excinfo.value)


def test_non_consecutive_ids_are_rejected():
    payload = _payload("甲", "乙")
    payload["directions"][1]["id"] = 7
    with pytest.raises(LLMOutputError) as excinfo:
        generate_directions(_points(), None, FakeLLM(payload))
    assert "连续编号" in str(excinfo.value)


# ---------- 空 rubric 不调 LLM（D-48 口径）----------


def test_empty_rubric_does_not_call_the_llm():
    client = FakeLLM(_payload("甲", "乙"))
    result = generate_directions([], None, client)
    assert client.calls == 0                     # 没烧 token
    assert result.directions == ()
    assert not result.ok
    assert "不生成候选方向" in result.failures[0]


# ---------- LLM 彻底失败：抛 LLMError，不返回半成品 ----------


def test_llm_failure_propagates_as_llm_error():
    with pytest.raises(LLMError):
        generate_directions(_points(), None, FakeLLM(LLMError("连续 3 次未通过校验")))
