"""M3 生成循环的单测：生成 → check() → 失败喂回重拆，最多 2 轮（§7.3 / D-16 / D-18）。"""

import pytest

from src.intelligence.decompose import decompose
from src.intelligence.llm import LLMOutputError
from src.models import RubricPoint


class FakeClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.users = []

    def chat_json(self, system, user, parse, **kwargs):
        self.users.append(user)
        return parse(self.payloads.pop(0))


def _rubric():
    return [
        RubricPoint(id="R1", quote="实现词法分析器", observable="可运行", status="normal"),
        RubricPoint(id="R2", quote="撰写实验报告", observable="有报告", status="normal"),
    ]


def _card(task_id, refs, hours):
    return {
        "task_id": task_id,
        "module_name": "实现词法分析器",
        "rubric_refs": list(refs),
        "effort_hours": hours,
        "depends_on": [],
        "deliverable": "一个源文件",
        "acceptance": "从 R1 原文改写",
    }


def test_first_pass_ok_uses_one_generation():
    client = FakeClient([{"cards": [_card("T1", ["R1", "R2"], 4)]}])
    result = decompose(_rubric(), client)
    assert result.ok
    assert result.generations == 1
    assert len(client.users) == 1


def test_missing_point_triggers_regeneration_with_feedback():
    client = FakeClient(
        [
            {"cards": [_card("T1", ["R1"], 4)]},              # 漏了 R2
            {"cards": [_card("T1", ["R1", "R2"], 4)]},        # 补上
        ]
    )
    result = decompose(_rubric(), client)
    assert result.ok
    assert result.generations == 2
    assert "R2" in client.users[1]                            # 失败详情喂回去了


def test_unbalanced_cards_trigger_regeneration():
    client = FakeClient(
        [
            {"cards": [_card("T1", ["R1"], 8), _card("T2", ["R2"], 2)]},
            {"cards": [_card("T1", ["R1"], 4), _card("T2", ["R2"], 4)]},
        ]
    )
    assert decompose(_rubric(), client).ok
    assert "不均衡" in client.users[1]


def test_two_failed_rounds_return_best_effort_and_failures():
    client = FakeClient(
        [
            {"cards": [_card("T1", ["R1"], 4)]},
            {"cards": [_card("T1", ["R1"], 4)]},
        ]
    )
    result = decompose(_rubric(), client)
    assert not result.ok
    assert result.generations == 2
    assert len(result.cards) == 1                             # 输出当前最优，交给的人（D-18）
    assert any("R2" in failure for failure in result.failures)


def test_all_ambiguous_is_refused_without_llm_call():
    client = FakeClient([])                                   # 没有任何可消费载荷
    rubric = [RubricPoint(id="R1", quote="内容充实", observable="不可核对", status="ambiguous")]
    result = decompose(rubric, client)
    assert not result.ok
    assert result.generations == 0
    assert client.users == []


def test_unknown_rubric_ref_is_a_schema_error():
    client = FakeClient([{"cards": [_card("T1", ["R9"], 4)]}])
    with pytest.raises(LLMOutputError) as exc:
        decompose(_rubric(), client)
    assert "不存在的评分点" in str(exc.value)


def test_duplicate_task_ids_are_rejected():
    client = FakeClient([{"cards": [_card("T1", ["R1"], 4), _card("T1", ["R2"], 4)]}])
    with pytest.raises(LLMOutputError):
        decompose(_rubric(), client)


def test_empty_cards_payload_is_rejected():
    client = FakeClient([{"cards": []}])
    with pytest.raises(LLMOutputError):
        decompose(_rubric(), client)
