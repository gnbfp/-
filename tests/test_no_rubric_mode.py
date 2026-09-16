"""无评分点模式（口径 A，2026-09-16）—— 需求层反转，所以测试也要把"边界"钉住。

口径（用户 2026-09-16 拍板）：
  * **默认仍然拒拆**（D-48 不改，不拿正文要求凑数）；
  * 网关回「没找到评分标准」时**给一条口令**：组长发「按正文拆」才走无评分点模式；
  * 口令三重校验：只认组长 + 只在原会话 + 窗口 5 分钟；
  * 拆出来的卡 ``rubric_refs`` 必须是**空数组**（没有评分点可溯源，不许编造编号）；
  * 自检**不判覆盖率**（没有分母），清单里显式写「不适用」——绝不假装 0% 或 100%；
  * M8 门③ 不收这类作业书（分母是人工基线的评分点，两者不可比）。

**离线**：不联网、不调真 LLM（stub）、不碰 `data/`。
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from src.gateway import replies
from src.gateway.events import Inbound
from src.gateway.router import confirm_no_rubric, no_rubric_block, no_rubric_expired, route
from src.intelligence.decompose import (
    DecomposeResult,
    _refusal,
    check,
    decompose_from_body,
)
from src.intelligence.llm import LLMClient, LLMError
from src.models import AssignmentMeta, Member, RubricPoint, Roster, SchemaError, TaskCard
from src.report.checklist import render_checklist

BOT = "ou_bot_self"
GROUP = "oc_group"
NOW = datetime(2026, 9, 16, 11, 0)
SRC = Path(__file__).resolve().parents[1] / "src"


# ---------- 夹具 ----------


def _meta():
    return AssignmentMeta(
        course="科学", title="河流健康调查", submission="报告", deadline="", source_file="x.pdf"
    )


def _leader_roster():
    return Roster(
        leader="ou_leader",
        members=[
            Member(open_id="ou_leader", name="组长"),
            Member(open_id="ou_a", name="组员甲"),
        ],
        registered_at="2026-09-16T09:00:00",
        confirmed_by="ou_leader",
    )


def _inbound(text="", *, chat_type="group", chat_id=GROUP, sender="ou_leader", mentions=()):
    return Inbound(
        chat_id=chat_id,
        chat_type=chat_type,
        message_type="text",
        text=text,
        mentions=tuple(mentions),
        sender_type="user",
        sender_open_id=sender,
        message_id="m1",
    )


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _StubHttp:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0

    def post(self, url, json=None, headers=None):  # noqa: A002
        self.calls += 1
        content = self.contents[min(self.calls - 1, len(self.contents) - 1)]
        return _Resp({"choices": [{"message": {"content": content}}]})


def _client(contents):
    http = _StubHttp(contents)
    client = LLMClient(api_key="k", base_url="https://example.invalid", model="m", http=http)
    return client, http


def _body_payload(*refs):
    """``refs`` 逐张卡给 rubric_refs（默认每张都是空数组）。"""
    if not refs:
        refs = ([],)
    return json.dumps(
        {
            "cards": [
                {
                    "task_id": f"T{i}",
                    "module_name": f"做第 {i} 块",
                    "rubric_refs": list(ref),
                    "effort_hours": 4,
                    "deliverable": "一份交付物",
                    "acceptance": "该卡来自正文要求，不是评分点，需组长确认：写完就行",
                }
                for i, ref in enumerate(refs, 1)
            ]
        },
        ensure_ascii=False,
    )


# ---------- 1. 数据模型：空 refs 合法，但"塞原文"仍然拒 ----------


def test_card_with_empty_refs_is_valid_in_body_mode():
    card = TaskCard(
        task_id="T1", module_name="写报告", rubric_refs=[], effort_hours=4.0,
        deliverable="第二章", acceptance="该卡来自正文要求，不是评分点，需组长确认",
    )
    card.validate()                                        # 不抛 = 合法


def test_card_still_rejects_non_string_refs():
    """D-03 的核心约束没变：数组里只能放 id 字符串，不许塞原文。"""
    card = TaskCard(
        task_id="T1", module_name="写报告", rubric_refs=["原文片段…"], effort_hours=4.0,
        deliverable="第二章", acceptance="验收",
    )
    card.validate()                                        # 字符串仍然合法

    broken = TaskCard(
        task_id="T2", module_name="写报告", rubric_refs=[{"quote": "原文"}], effort_hours=4.0,
        deliverable="第二章", acceptance="验收",
    )
    try:
        broken.validate()
    except SchemaError as exc:
        assert "rubric_refs" in str(exc)
    else:                                                  # pragma: no cover
        raise AssertionError("塞了非字符串的 rubric_refs 应当被拒")


# ---------- 2. 自检：默认拒拆，放行后只判卡/均衡/环 ----------


def test_empty_rubric_still_refuses_by_default():
    """口径 A 的前提：**默认行为一个字都没改**（D-48 保留）。"""
    assert _refusal([]) is not None
    assert check([], []) and "拒拆" in check([], [])[0]


def test_all_ambiguous_still_refuses_even_in_body_mode():
    """无评分点模式**不覆盖**"全部 ambiguous"那一档 —— 那不是"没有评分标准"。"""
    all_ambiguous = [
        RubricPoint(id="R1", quote="q", observable="o", status="ambiguous"),
        RubricPoint(id="R2", quote="q", observable="o", status="ambiguous"),
    ]
    assert _refusal(all_ambiguous, allow_empty_rubric=True) is not None


def test_body_mode_check_skips_coverage_but_keeps_balance_and_cycle():
    one_card = [
        TaskCard(task_id="T1", module_name="m", rubric_refs=[], effort_hours=4.0,
                 deliverable="d", acceptance="a"),
    ]
    assert check(one_card, [], allow_empty_rubric=True) == []       # 不判覆盖率

    unbalanced = [
        TaskCard(task_id="T1", module_name="m", rubric_refs=[], effort_hours=1.0,
                 deliverable="d", acceptance="a"),
        TaskCard(task_id="T2", module_name="m", rubric_refs=[], effort_hours=20.0,
                 deliverable="d", acceptance="a"),
    ]
    failures = check(unbalanced, [], allow_empty_rubric=True)
    assert any("工时不均衡" in item for item in failures)            # 均衡照判

    cyclic = [
        TaskCard(task_id="T1", module_name="m", rubric_refs=[], effort_hours=4.0,
                 deliverable="d", acceptance="a", depends_on=["T2"]),
        TaskCard(task_id="T2", module_name="m", rubric_refs=[], effort_hours=4.0,
                 deliverable="d", acceptance="a", depends_on=["T1"]),
    ]
    assert any("成环" in item for item in check(cyclic, [], allow_empty_rubric=True))


# ---------- 3. decompose_from_body ----------


def test_decompose_from_body_produces_cards_with_empty_refs():
    client, http = _client([_body_payload([], [])])

    result = decompose_from_body("作业书正文……要做一份报告……", client)

    assert result.mode == "body"
    assert len(result.cards) == 2
    assert all(card.rubric_refs == [] for card in result.cards)
    assert result.failures == ()                            # 自检通过
    assert http.calls == 1


def test_decompose_from_body_rejects_fabricated_refs_and_retries():
    """模型编了个 R1 → 当作校验失败喂回重拆，而不是"悄悄清掉"（那会掩盖它不听话）。"""
    client, http = _client([_body_payload(["R1"]), _body_payload([])])

    result = decompose_from_body("正文……", client)

    assert http.calls == 2                                  # 第 2 次才合规
    assert all(card.rubric_refs == [] for card in result.cards)


def test_decompose_from_body_gives_up_with_a_clear_error():
    client, http = _client([_body_payload(["R1"])])         # 一直编

    try:
        decompose_from_body("正文……", client)
    except LLMError as exc:
        assert "rubric_refs" in str(exc)
    else:                                                   # pragma: no cover
        raise AssertionError("一直编造引用时应当抛 LLMError")

    assert http.calls == 3                                  # max_retries=2


# ---------- 4. 口令的三重校验 ----------


def _window(chat_id=GROUP, *, minutes_ago=0):
    block = no_rubric_block(
        Path("uploads/河流健康调查.pdf"), _meta(), _inbound(chat_id=chat_id),
        now=NOW - timedelta(minutes=minutes_ago),
    )
    return {"no_rubric": block}


def test_leader_in_the_same_chat_can_confirm():
    outcome = confirm_no_rubric(_inbound("按正文拆"), _window(), _leader_roster(), NOW)
    assert [r.text for r in outcome.replies] == [replies.NO_RUBRIC_STARTED]
    assert outcome.pipeline == "no_rubric"


def test_non_leader_is_refused():
    outcome = confirm_no_rubric(
        _inbound("按正文拆", sender="ou_a"), _window(), _leader_roster(), NOW
    )
    assert [r.text for r in outcome.replies] == [replies.NO_RUBRIC_NEED_LEADER]
    assert outcome.pipeline == ""


def test_stranger_is_refused_too():
    outcome = confirm_no_rubric(
        _inbound("按正文拆", sender="ou_stranger"), _window(), _leader_roster(), NOW
    )
    assert [r.text for r in outcome.replies] == [replies.NO_RUBRIC_NEED_LEADER]


def test_another_chat_cannot_confirm_for_this_file():
    """D-47 同款：别的会话里投的文件，不该被这个会话的人拍板。"""
    outcome = confirm_no_rubric(
        _inbound("按正文拆", chat_id="oc_other"), _window(), _leader_roster(), NOW
    )
    assert [r.text for r in outcome.replies] == [replies.NO_RUBRIC_NO_PENDING]
    assert outcome.pipeline == ""


def test_expired_window_is_refused():
    state = _window(minutes_ago=6)                          # TTL = 5 分钟
    assert no_rubric_expired(state["no_rubric"], NOW) is True
    outcome = confirm_no_rubric(_inbound("按正文拆"), state, _leader_roster(), NOW)
    assert [r.text for r in outcome.replies] == [replies.NO_RUBRIC_NO_PENDING]


def test_without_a_window_it_is_refused_even_for_the_leader():
    outcome = confirm_no_rubric(_inbound("按正文拆"), {}, _leader_roster(), NOW)
    assert [r.text for r in outcome.replies] == [replies.NO_RUBRIC_NO_PENDING]
    assert outcome.pipeline == ""


def test_dirty_timestamp_does_not_expire_the_window():
    """时间戳脏了当没过期（与 ``_stale_pending`` 同款防御）—— 别因脏数据卡死人。"""
    assert no_rubric_expired({"expires_at": "不是时间"}) is False


def test_route_dispatches_the_command():
    # 群里必须 @ 机器人（D-69）—— 这里带上 mention，走的是真机上的那条路径
    from src.gateway.events import Mention

    outcome = route(
        _inbound("@_user_1 按正文拆",
                 mentions=(Mention(key="@_user_1", open_id=BOT, name="喵喵喵"),)),
        _window(), _leader_roster(), bot_open_id=BOT, bot_name="喵喵喵", now=NOW,
    )
    assert outcome.pipeline == "no_rubric"


def test_without_a_bot_mention_the_command_is_dropped_by_the_gate():
    """群里不 @ 我 = 当大家在聊天（D-69），「按正文拆」也不例外。"""
    outcome = route(_inbound("按正文拆"), _window(), _leader_roster(),
                    bot_open_id=BOT, bot_name="喵喵喵", now=NOW)
    assert outcome.replies == ()
    assert outcome.pipeline == ""


# ---------- 5. 清单：覆盖率必须写"不适用" ----------


def test_checklist_says_not_applicable_instead_of_a_fake_ratio():
    card = TaskCard(task_id="T1", module_name="写报告", rubric_refs=[], effort_hours=4.0,
                    deliverable="第二章", acceptance="该卡来自正文要求，不是评分点，需组长确认")
    result = DecomposeResult(cards=(card,), failures=(), generations=1, mode="body")

    text = render_checklist(_meta(), [], (card,), result)

    assert "不适用" in text
    assert "0%" not in text.split("覆盖率")[1].splitlines()[0]
    assert "100%" not in text.split("覆盖率")[1].splitlines()[0]


def test_reject_case_still_says_rejected_not_unavailable():
    """对照组：真的"没卡也没评分点"（拒拆）还是那句老话，别被无评分点模式抢了。"""
    result = DecomposeResult(cards=(), failures=("拒拆",), generations=0)
    text = render_checklist(_meta(), [], (), result)
    assert "拒拆（不是 100%）" in text


# ---------- 6. B2 守卫：M4–M7 仍然零 LLM ----------


def test_runners_of_m4_to_m7_still_have_no_llm():
    """无评分点模式也没有给 M4–M7 加智能（B2）：这些模块连 LLM 都不该 import。"""
    for name in ("vote.py", "preference.py", "allocation.py", "reminder.py",
                 "complete.py", "replies.py"):
        text = (SRC / "gateway" / name).read_text(encoding="utf-8")
        assert "LLMClient" not in text and "_llm(" not in text, name
    for name in ("report/checklist.py", "report/gantt.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        assert "LLMClient" not in text and "_llm(" not in text, name
