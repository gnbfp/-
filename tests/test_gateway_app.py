"""M0 组装层单测：依赖注入 FakeSender / FakeDownloader / FakeLLM（方案 §9）。"""

from datetime import datetime

import pytest

from src.gateway import app as app_module
from src.gateway import replies
from src.gateway.events import Inbound, Mention
from src.intelligence.extract import ExtractError
from src.intelligence.llm import LLMError
from src.models import AssignmentMeta, RubricPoint, TaskCard
from src.storage import JsonStore

DOC = "作业书：1 实现词法分析器 40 分。2 撰写实验报告 60 分。"

M1_PAYLOAD = {
    "assignment": {
        "course": "编译原理",
        "title": "课程设计",
        "submission": "源码 + 报告",
        "deadline": "2026-09-19T23:59",
        "source_file": "LLM 猜的",
    },
    "rubric": [
        {
            "id": "R1",
            "quote": "实现词法分析器",
            "weight": 40,
            "observable": "可运行",
            "status": "normal",
        },
        {
            "id": "R2",
            "quote": "撰写实验报告",
            "weight": 60,
            "observable": "有报告",
            "status": "normal",
        },
    ],
}

M3_PAYLOAD = {
    "cards": [
        {
            "task_id": "T1",
            "module_name": "实现词法分析器",
            "rubric_refs": ["R1"],
            "effort_hours": 4,
            "depends_on": [],
            "deliverable": "一个源文件",
            "acceptance": "从 R1 原文改写",
        },
        {
            "task_id": "T2",
            "module_name": "撰写实验报告",
            "rubric_refs": ["R2"],
            "effort_hours": 4,
            "depends_on": [],
            "deliverable": "一份报告",
            "acceptance": "从 R2 原文改写",
        },
    ]
}


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True

    @property
    def texts(self):
        return [m.text for m in self.sent]


class FakeDownloader:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def download(self, pending, target_dir):
        self.calls.append(dict(pending))
        if self.error:
            raise self.error
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / (pending.get("file_name") or "作业书.txt")
        path.write_text(DOC, encoding="utf-8")
        return path


class FakeLLM:
    def __init__(self, error=None):
        self.error = error

    def chat_json(self, system, user, parse, **kwargs):
        if self.error:
            raise self.error
        payload = M1_PAYLOAD if "M1 输入解析" in system else M3_PAYLOAD
        return parse(payload)


class _InlineThread:
    """把后台线程换成同步执行，测试才能确定性地断言流水线结果。"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("src.gateway.app.threading.Thread", _InlineThread)
    store = JsonStore(tmp_path / "data")
    store.ensure_dirs()
    sender = FakeSender()
    downloader = FakeDownloader()
    gateway = app_module.Gateway(
        config=None, store=store, sender=sender, downloader=downloader, llm_client=FakeLLM()
    )
    return gateway, store, sender, downloader


def _inbound(text="", **over):
    data = dict(
        chat_id="c1",
        chat_type="group",
        message_type="text",
        text=text,
        sender_type="user",
        sender_open_id="ou_user",
        message_id="m1",
    )
    data.update(over)
    return Inbound(**data)


def _seed_pending_file(store):
    store.save_state(
        {
            "awaiting": None,
            "pending_file": {
                "file_key": "fk_1",
                "file_name": "作业书.txt",
                "resource_type": "file",
                "message_id": "m1",
                "chat_id": "c1",
                # 必须是"刚刚"：pending_file 有 30 分钟有效期（D-46），
                # 写死的时间戳会让整个夹具随时间流逝变成过期缓存
                "received_at": _now(),
            },
        }
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def test_file_message_is_cached_with_resource_type(env):
    gateway, store, sender, _ = env
    gateway.handle(_inbound("", message_type="file", file_key="fk_9", file_name="a.pdf"))
    pending = store.load_state()["pending_file"]
    assert pending["file_key"] == "fk_9"
    assert pending["resource_type"] == "file"
    assert "a.pdf" in sender.texts[0]


def test_assignment_pipeline_writes_data_and_posts_checklist(env):
    gateway, store, sender, downloader = env
    _seed_pending_file(store)

    gateway.handle(_inbound("作业书"))

    assert sender.texts[0] == replies.PARSING
    assert "评分点核对清单" in sender.texts[1]
    assert "覆盖率：2/2 = 100%" in sender.texts[1]
    assert [p.id for p in store.load_rubric()] == ["R1", "R2"]
    assert [c.task_id for c in store.load_cards()] == ["T1", "T2"]
    assert store.load_assignment().source_file == "作业书.txt"       # 文件名由代码给
    assert "pending_file" not in store.load_state()                  # 消费掉缓存
    assert downloader.calls[0]["file_key"] == "fk_1"


class _RadicalDownloader(FakeDownloader):
    """作业书里混进一个 NFKC 和小表都兜不住的部首（U+2E80）。"""

    def download(self, pending, target_dir):
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "作业书.txt"
        path.write_text(DOC + "\n附注：详\u2e80 第 3 节。", encoding="utf-8")
        return path


def test_residual_radicals_surface_as_a_soft_warning(env):
    """D-43 兜底：漏网部首要在清单里点名，而不是悄悄污染 M1 的 quote 校验。"""
    gateway, store, sender, _ = env
    gateway.downloader = _RadicalDownloader()
    _seed_pending_file(store)

    gateway.handle(_inbound("作业书"))

    report = sender.texts[-1]
    assert "评分点核对清单" in report          # 软警告不拒收：清单照常出
    assert "[软警告]" in report
    assert "部首字符未归一化" in report


def test_extract_rejection_replies_and_clears_pending(env):
    gateway, store, sender, _ = env
    gateway.downloader = FakeDownloader(error=ExtractError("PDF 没有文字层"))
    _seed_pending_file(store)

    gateway.handle(_inbound("作业书"))

    assert "PDF 没有文字层" in sender.texts[1]
    assert "pending_file" not in store.load_state()


def test_llm_failure_degrades_with_a_human_message(env):
    gateway, store, sender, _ = env
    gateway._llm_client = FakeLLM(error=LLMError("连续 3 次未通过校验"))
    _seed_pending_file(store)

    gateway.handle(_inbound("作业书"))

    assert sender.texts[1] == replies.PARSE_FAILED
    assert "pending_file" not in store.load_state()


class _EmptyRubricLLM(FakeLLM):
    """M1 返回空 rubric（文件里没有评分标准），并记录 M3 有没有被调过。"""

    def __init__(self):
        super().__init__()
        self.m3_called = False

    def chat_json(self, system, user, parse, **kwargs):
        if "M1 输入解析" in system:
            return parse({**M1_PAYLOAD, "rubric": []})
        self.m3_called = True
        return parse(M3_PAYLOAD)


def test_empty_rubric_stops_before_m3_and_keeps_existing_products(env):
    """D-48 / D-49②：没找到评分标准 → 不跑 M3、**不落盘**，上一份好产物不能被清空。"""
    gateway, store, sender, _ = env
    store.save_assignment(
        AssignmentMeta(
            course="旧课程",
            title="旧作业",
            submission="旧交付",
            deadline="",
            source_file="旧作业书.pdf",
        )
    )
    store.save_rubric(
        [RubricPoint(id="R_old", quote="旧评分点", observable="旧", status="normal")]
    )
    store.save_cards(
        [
            TaskCard(
                task_id="T_seed",
                module_name="旧卡",
                rubric_refs=["R_old"],
                effort_hours=4.0,
                deliverable="旧产物",
                acceptance="旧验收",
            )
        ]
    )
    llm = _EmptyRubricLLM()
    gateway._llm_client = llm
    _seed_pending_file(store)

    gateway.handle(_inbound("作业书"))

    assert sender.texts[0] == replies.PARSING
    assert sender.texts[-1] == replies.NO_RUBRIC_FOUND
    assert llm.m3_called is False                                   # 没起 M3、不烧第二次 LLM
    # 拒拆时一个字都不写（P2）：三份产物全是旧的
    assert [p.id for p in store.load_rubric()] == ["R_old"]
    assert [c.task_id for c in store.load_cards()] == ["T_seed"]
    assert store.load_assignment().title == "旧作业"


def test_register_confirm_writes_members_json(env):
    gateway, store, sender, _ = env
    store.save_state(
        {
            "awaiting": "register",
            "register": {
                "stage": "confirm",
                "leader": {"open_id": "ou_zhang", "name": "张三"},
                "members": [{"open_id": "ou_li", "name": "李四"}],
                "expires_at": None,
            },
        }
    )
    gateway.handle(_inbound("同意", sender_open_id="ou_initiator"))

    roster = store.load_members()
    assert roster.leader == "ou_zhang"
    assert [m.open_id for m in roster.members] == ["ou_zhang", "ou_li"]
    assert store.load_state()["awaiting"] is None
    assert "已保存" in sender.texts[0]


def test_bot_message_is_ignored_entirely(env):
    gateway, store, sender, _ = env
    gateway.handle(_inbound("拆解", sender_type="app"))
    assert sender.texts == []
    assert store.load_state() == {}


def test_decompose_without_assignment_still_reports_coverage(env):
    gateway, store, sender, _ = env
    store.save_rubric(
        [
            RubricPoint(id="R1", quote="实现词法分析器", observable="可运行"),
            RubricPoint(id="R2", quote="撰写实验报告", observable="有报告"),
        ]
    )
    gateway.handle(_inbound("拆解"))
    assert sender.texts[0] == replies.DECOMPOSING        # 先回执，重活在后台线程
    assert "覆盖率" in sender.texts[1]                   # 报告是第二条
    assert store.load_cards() != []


def test_unmatched_text_gets_command_list(env):
    gateway, _, sender, _ = env
    gateway.handle(_inbound("随便说句话"))
    assert sender.texts == [replies.COMMAND_LIST_TEXT]


def test_a_new_file_arriving_mid_pipeline_survives(env):
    """必修 5 的现场：发 A → 回「作业书」（后台跑十几秒）→ 期间来了 B → A 收尾。

    B 不能被 A 的收尾顺手删掉，否则下次「作业书」会回"请先把作业书文件发给我"，
    而用户明明刚发过。
    """
    gateway, store, sender, _ = env

    class _NewFileDuringPipeline(FakeDownloader):
        def download(self, pending, target_dir):
            path = super().download(pending, target_dir)
            store.save_state(
                {
                    "awaiting": None,
                    "pending_file": {
                        "file_key": "fk_2",
                        "file_name": "作业书-B.pdf",
                        "resource_type": "file",
                        "message_id": "m2",
                        "chat_id": "c1",
                        "received_at": _now(),
                    },
                }
            )
            return path

    gateway.downloader = _NewFileDuringPipeline()
    _seed_pending_file(store)                      # A: m1 / fk_1

    gateway.handle(_inbound("作业书"))

    assert "评分点核对清单" in sender.texts[1]
    assert store.load_state()["pending_file"]["file_key"] == "fk_2"      # B 还在


def test_forget_pending_file_only_clears_its_own(env):
    gateway, store, _, _ = env
    _seed_pending_file(store)                      # message_id = m1

    gateway._forget_pending_file("m2")             # 不是它消费的那个
    assert store.load_state()["pending_file"]["file_key"] == "fk_1"

    gateway._forget_pending_file("m1")
    assert "pending_file" not in store.load_state()


def test_register_window_does_not_eat_commands(env):
    """必修 1 的现场：发过「登记」不填表，群里其它指令照常可用。"""
    gateway, store, sender, downloader = env
    _seed_pending_file(store)
    store.save_state(
        {
            **store.load_state(),
            "awaiting": "register",
            "register": {"stage": "collect", "expires_at": None, "initiator_open_id": "ou_user"},
        }
    )

    gateway.handle(_inbound("今天天气不错"))
    assert sender.texts[-1] == replies.COMMAND_LIST_TEXT

    gateway.handle(_inbound("作业书"))
    assert downloader.calls and downloader.calls[0]["file_key"] == "fk_1"
    assert any("评分点核对清单" in text for text in sender.texts)


def test_register_collect_routes_through_mentions(env):
    gateway, store, sender, _ = env
    gateway.handle(_inbound("登记"))
    assert store.load_state()["awaiting"] == "register"

    mentions = (
        Mention(key="@_user_1", open_id="ou_zhang", name="张三"),
        Mention(key="@_user_2", open_id="ou_li", name="李四"),
        Mention(key="@_user_3", open_id="ou_wang", name="王五"),
    )
    form = "登记\n组长：@_user_1\n组员：@_user_2 @_user_3"
    gateway.handle(_inbound(form, mentions=mentions))
    assert store.load_state()["register"]["stage"] == "confirm"
    assert "共 3 人" in sender.texts[-1]
