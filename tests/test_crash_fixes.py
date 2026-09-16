"""三条"整链路崩 / 停摆"的回归（F1，2026-09-16 加固）。

三条都是**实测复现过**的，各自守一条：

1. **提示词 ↔ schema 冲突**：提示词规则 6 说"找不到的字段填空字符串"，
   而 `AssignmentMeta.validate()` 判 `course` / `title` / `submission` 必填 ——
   两条规则互相打死时，M1 **连续 3 次重试全败、整条链路失败**，用户只看到一句通用
   「解析失败」。现在两边统一成"空是合法的"，空值走 `check_meta_fields()` 软警告。
2. **带时区的 `deadline`**：`fromisoformat` 吃下 `+08:00` / `Z` 后返回 **aware** datetime，
   与项目里到处用的 naive `datetime.now()` 相减/比较直接 `TypeError` ——
   M7 报告 + 甘特图整条崩、M6 催办每轮都崩（异常被吞成一行日志，表现为"机器人不催了"）。
3. **`content: null`**：上游偶发返回 `null` 时，`json.loads(None)` 抛 TypeError，
   而 `chat_json` 的四个 `except` 里没有 TypeError ⇒ 重试机制当场失效（实测只发 1 次请求）。

**离线**：不联网、不调真 LLM（http 层换成 stub）、不碰 `data/`。
"""

import json
from datetime import datetime, timedelta

from src.gateway.reminder import scan as reminder_scan
from src.intelligence.extract import check_deadline, check_meta_fields
from src.intelligence.llm import LLMClient, LLMError
from src.intelligence.parse import parse_assignment
from src.models import (
    AssignmentMeta,
    AssignmentRecord,
    SchemaError,
    TaskCard,
    parse_deadline,
)
from src.report.gantt import plan_bars

TEXT = (
    "课程设计任务书\n"
    "题目：图书馆管理系统\n"
    "一、评分标准\n"
    "1. 需求分析（30 分）\n"
)


# ---------- LLM 桩：按顺序吐预置的 content ----------


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _StubHttp:
    """内容按顺序取；用光之后重复最后一个。记录请求次数。"""

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


def _m1_payload(*, course="", title="图书馆管理系统", submission="", deadline=""):
    return json.dumps(
        {
            "assignment": {
                "course": course,
                "title": title,
                "submission": submission,
                "deadline": deadline,
            },
            "rubric": [
                {
                    "id": "R1",
                    "quote": "需求分析（30 分）",
                    "weight": 30,
                    "observable": "有需求分析章节",
                    "status": "normal",
                }
            ],
        },
        ensure_ascii=False,
    )


# ---------- 1. 提示词 ↔ schema：空字段不再打死整条链路 ----------


def test_m1_accepts_empty_meta_fields_without_retrying():
    """以前：空 course → 3 次重试全败 → LLMError（整条 M1 链路失败）。"""
    client, http = _client([_m1_payload()])

    parsed = parse_assignment(TEXT, client, source_file="作业书.pdf")

    assert parsed.meta.course == ""
    assert parsed.meta.submission == ""
    assert parsed.meta.source_file == "作业书.pdf"      # 调用方注入的事实，仍在
    assert http.calls == 1                              # 一次就过，不再烧重试
    assert len(parsed.points) == 1


def test_empty_meta_fields_surface_as_a_soft_warning():
    meta = AssignmentMeta(
        course="", title="图书馆管理系统", submission="", deadline="", source_file="x.pdf"
    )
    warning = check_meta_fields(meta)
    assert warning is not None
    assert "course" in warning and "submission" in warning
    assert "title" not in warning                        # 只点名真正空的那些


def test_filled_meta_fields_produce_no_warning():
    meta = AssignmentMeta(
        course="编译原理", title="词法分析器", submission="源码 + 报告",
        deadline="", source_file="x.pdf",
    )
    assert check_meta_fields(meta) is None


def test_source_file_is_still_required():
    """``source_file`` 由调用方注入，空了说明调用方漏传 —— 属于代码 bug，该当场炸。"""
    meta = AssignmentMeta(course="c", title="t", submission="s", deadline="", source_file="")
    try:
        meta.validate()
    except SchemaError as exc:
        assert "source_file" in str(exc)
    else:                                                # pragma: no cover
        raise AssertionError("source_file 为空时应当报 SchemaError")


# ---------- 2. 带时区的 deadline ----------


def test_deadline_with_offset_is_normalized_to_local_naive():
    moment = parse_deadline("2026-09-19T23:59+08:00")
    assert moment is not None
    assert moment.tzinfo is None                          # 类型与 datetime.now() 对齐
    expected = datetime(2026, 9, 19, 23, 59) - (
        datetime(2026, 9, 19, 23, 59).astimezone().utcoffset()
        - timedelta(hours=8)
    )
    assert moment == expected


def test_deadline_with_z_suffix_is_normalized():
    moment = parse_deadline("2026-09-19T15:59Z")
    assert moment is not None and moment.tzinfo is None


def test_deadline_timezone_surfaces_as_a_soft_warning():
    meta = AssignmentMeta(
        course="c", title="t", submission="s",
        deadline="2026-09-19T23:59+08:00", source_file="x.pdf",
    )
    warning = check_deadline(meta)
    assert warning is not None and "时区" in warning


def _cards_and_records():
    cards = [
        TaskCard(task_id="T1", module_name="模块一", rubric_refs=["R1"], effort_hours=4.0,
                 deliverable="交付物", acceptance="验收"),
    ]
    records = [AssignmentRecord(task_id="T1", assignee="ou_a", source="volunteer_1")]
    return cards, records


def test_gantt_does_not_crash_on_an_offset_deadline():
    """实测过的现场：M7「报告」+ 甘特图整条崩在 aware/naive 比较上。"""
    cards, _ = _cards_and_records()
    now = datetime(2026, 9, 16, 11, 0)
    meta = AssignmentMeta(
        course="c", title="t", submission="s",
        deadline=(now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M") + "+08:00",
        source_file="x.pdf",
    )

    bars, deadline = plan_bars(cards, [], meta, now=now)

    assert len(bars) == 1
    assert deadline is not None and deadline.tzinfo is None


def test_reminder_scan_does_not_crash_on_an_offset_deadline():
    """实测过的现场：M6 催办每轮都崩，一条都催不出去（异常被 app 层吞成一行日志）。"""
    cards, records = _cards_and_records()
    now = datetime(2026, 9, 16, 11, 0)
    meta = AssignmentMeta(
        course="c", title="t", submission="s",
        deadline=(now + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M") + "+08:00",
        source_file="x.pdf",
    )

    due = reminder_scan(cards, records, meta, [], now=now)

    assert len(due) == 1                                  # 20 小时后到点 → 落进 24h 档


# ---------- 3. content: null ----------


def test_null_content_is_retried_instead_of_killing_the_chain():
    """实测过的现场：以前只发 1 次请求就整条报错（TypeError 逃出全部 except）。"""
    client, http = _client([None, _m1_payload()])

    parsed = parse_assignment(TEXT, client, source_file="作业书.pdf")

    assert parsed.points                                   # 第 2 次拿到了合法输出
    assert http.calls == 2                                 # 确实重试了


def test_all_null_content_raises_llm_error_after_the_retry_budget():
    client, http = _client([None])

    try:
        parse_assignment(TEXT, client, source_file="作业书.pdf")
    except LLMError as exc:
        assert "content" in str(exc)
    else:                                                  # pragma: no cover
        raise AssertionError("全是 null 时应当抛 LLMError")

    assert http.calls == 3                                 # max_retries=2 ⇒ 最多 3 次


def test_blank_string_content_is_treated_the_same_way():
    client, http = _client(["   ", _m1_payload()])

    parse_assignment(TEXT, client, source_file="作业书.pdf")

    assert http.calls == 2
