"""M0 组装层单测：依赖注入 FakeSender / FakeDownloader / FakeLLM（方案 §9）。"""

import itertools
from datetime import datetime

import pytest

from src.gateway import app as app_module
from src.gateway import replies
from src.gateway.events import Inbound, Mention
from src.intelligence.extract import ExtractError
from src.intelligence.llm import LLMError
from src.models import AssignmentMeta, Member, Roster, RubricPoint, TaskCard
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


_MSG_SEQ = itertools.count(1)


def _inbound(text="", **over):
    data = dict(
        chat_id="c1",
        chat_type="group",
        message_type="text",
        text=text,
        sender_type="user",
        sender_open_id="ou_user",
        # 每条默认一个新 id：handle() 会按 message_id 去重（P0-A），
        # 夹具共用 "m1" 会被当成重复事件、第二个 handle 直接空转。
        message_id=f"m{next(_MSG_SEQ)}",
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


# ---------- M4 志愿分配（§2.1~§2.4 / D-52~D-54）----------


def _seed_m4(store):
    """三张卡 + 三个人（组长 = ou_user，就是默认的发送者）。"""
    store.save_cards(
        [
            TaskCard(
                task_id=f"T{index}",
                module_name=f"模块{index}",
                rubric_refs=["R1"],
                effort_hours=1.0,
                deliverable="交付物",
                acceptance="验收标准",
            )
            for index in (1, 2, 3)
        ]
    )
    store.save_members(
        Roster(
            leader="ou_user",
            members=[
                Member(open_id=open_id, name=name)
                for open_id, name in (("ou_user", "张三"), ("ou_b", "李四"), ("ou_c", "王五"))
            ],
            registered_at="2026-09-13T09:00:00",
            confirmed_by="ou_user",
        )
    )


def _dm(text, sender):
    return _inbound(text, chat_type="p2p", chat_id=sender, sender_open_id=sender)


def test_m4_group_command_opens_the_window_and_dms_everyone(env):
    gateway, store, sender, _ = env
    _seed_m4(store)

    gateway.handle(_inbound("你想做哪一块"))

    state = store.load_state()
    assert state["awaiting"] == "preference"
    assert state["group_chat_id"] == "c1"
    assert sender.sent[0].chat_id == "c1"                     # 清单发群
    assert [m.receive_id_type for m in sender.sent[1:]] == ["open_id"] * 3
    assert [m.chat_id for m in sender.sent[1:]] == ["ou_user", "ou_b", "ou_c"]


def test_m4_collects_preferences_and_settles_into_assignments(env):
    gateway, store, sender, _ = env
    _seed_m4(store)
    gateway.handle(_inbound("你想做哪一块"))

    gateway.handle(_dm("1", "ou_b"))
    gateway.handle(_dm("1 2", "ou_c"))

    assert [p.user_id for p in store.load_preferences()] == ["ou_b", "ou_c"]
    assert store.load_preferences()[1].ranked_task_ids == ["T1", "T2"]

    sender.sent.clear()
    gateway.handle(_inbound("你想做哪一块"))                   # 组长重发 → 二次确认（P0-B）

    assert store.load_state()["awaiting"] == "preference"      # 不再直接封盘
    assert sender.texts == [replies.PREFERENCE_CONFIRM_SEAL.format(done=2, missing=1)]

    sender.sent.clear()
    gateway.handle(_inbound("封盘"))                           # 组长确认 → 结算

    assert store.load_state()["awaiting"] is None
    assert {a.task_id: (a.assignee, a.source) for a in store.load_assignments()} == {
        "T1": ("ou_b", "volunteer_1"),
        "T2": ("ou_c", "volunteer_2"),
        "T3": ("ou_user", "auto"),
    }
    assert sender.sent[0].chat_id == "c1"
    assert "分配总表" in sender.texts[0]
    assert "第一志愿 1 人 / 第二志愿 1 人 / 兜底 1 人" in sender.texts[0]


def test_m4_resubmission_overwrites_the_earlier_preference(env):
    gateway, store, _, _ = env
    _seed_m4(store)
    gateway.handle(_inbound("你想做哪一块"))

    gateway.handle(_dm("1", "ou_b"))
    first = store.load_preferences()[0].submitted_at
    gateway.handle(_dm("2 3", "ou_b"))

    stored = store.load_preferences()
    assert len(stored) == 1                                   # 后投覆盖先投，不是追加
    assert stored[0].ranked_task_ids == ["T2", "T3"]
    assert stored[0].submitted_at >= first


def test_m4_group_digits_during_the_window_are_ignored(env):
    """D-54 的现场：窗口开着的时候在群里打数字，不该被记成志愿。"""
    gateway, store, sender, _ = env
    _seed_m4(store)
    gateway.handle(_inbound("你想做哪一块"))

    sender.sent.clear()
    gateway.handle(_inbound("3"))

    assert store.load_preferences() == []
    assert sender.texts == [replies.COMMAND_LIST_TEXT]


# ---------- M5 匿名代言（§6.5 / D-55）----------


def test_m5_proposal_is_relayed_anonymously_and_leaves_a_trace(env):
    gateway, store, sender, _ = env
    gateway.handle(_inbound("随便说句话"))                     # 先让机器人认下"群"
    sender.sent.clear()

    gateway.handle(_dm("我想提议：前端用 React", "ou_b"))

    assert sender.sent[0].chat_id == "c1"                     # 转达到群
    assert sender.sent[0].text == "有组员提议：前端用 React"
    assert "ou_b" not in sender.sent[0].text
    assert "李四" not in sender.sent[0].text
    assert sender.sent[1].chat_id == "ou_b"                   # 私聊确认
    assert sender.texts[1] == replies.PROPOSAL_ACK

    proposals = store.load_proposals()
    assert proposals[0]["user_id"] == "ou_b"                  # 留痕真实身份
    assert proposals[0]["text"] == "前端用 React"


def test_m5_without_a_known_group_does_not_pretend_to_post(env):
    gateway, store, sender, _ = env
    gateway.handle(_dm("我想提议：加图表", "ou_b"))
    assert sender.texts == [replies.NEED_GROUP]
    assert store.load_proposals() == []


def test_group_id_is_remembered_from_any_group_message(env):
    gateway, store, _, _ = env
    gateway.handle(_inbound("随便说句话"))
    assert store.load_state()["group_chat_id"] == "c1"

    gateway.handle(_dm("你好", "ou_b"))
    assert store.load_state()["group_chat_id"] == "c1"        # 私聊不会把它改掉


def test_a_failed_direct_message_does_not_eat_the_group_reply(env):
    """一条发失败不能吃掉后面几条：M4 要连发"清单发群 + 每人私聊"。"""
    gateway, store, _, _ = env
    _seed_m4(store)

    class _SelectiveSender(FakeSender):
        def send(self, message):
            if message.receive_id_type == "open_id":
                raise RuntimeError("私聊发不出去")
            return super().send(message)

    gateway.sender = _SelectiveSender()
    gateway.handle(_inbound("你想做哪一块"))

    assert "任务卡清单" in gateway.sender.texts[0]             # 群里的清单照发
    assert store.load_state()["awaiting"] == "preference"


# ---------- M4 真机故障修复（P0-A / P0-C / P0-D / P1-E）----------


def test_duplicate_message_id_is_processed_only_once(env):
    """P0-A：飞书重复投递同一个事件时，第二次必须什么都不做。"""
    gateway, store, sender, _ = env
    inbound = _inbound("随便说句话")

    gateway.handle(inbound)
    sent_before = len(sender.sent)
    outcome = gateway.handle(inbound)

    assert outcome.replies == ()
    assert len(sender.sent) == sent_before


def test_failed_dm_is_reported_in_the_group(env):
    """P0-C：私聊发不出去要在群里说出来，不能只留一行 stderr。"""
    gateway, store, _, _ = env
    _seed_m4(store)

    class _NoDmSender(FakeSender):
        def send(self, message):
            if message.receive_id_type == "open_id":
                raise RuntimeError("code=230101")
            return super().send(message)

    sender = _NoDmSender()
    gateway.sender = sender
    gateway.handle(_inbound("你想做哪一块"))

    assert sender.sent[-1].chat_id == "c1"
    assert sender.texts[-1] == replies.PREFERENCE_DM_FAILED.format(count=3)


def test_register_clears_a_leftover_preference_window(env):
    """P0-D：切到登记状态机必须清掉旧志愿窗口。"""
    gateway, store, _, _ = env
    _seed_m4(store)
    gateway.handle(_inbound("你想做哪一块"))
    assert store.load_state()["awaiting"] == "preference"

    gateway.handle(_inbound("登记"))

    state = store.load_state()
    assert state["awaiting"] == "register"
    assert state["preference"] is None


def test_preference_window_clears_register_residue(env):
    """P0-D 反向：开志愿窗口时清掉登记残留。"""
    gateway, store, _, _ = env
    _seed_m4(store)
    gateway.handle(_inbound("登记"))
    assert store.load_state()["register"] is not None

    gateway.handle(_inbound("你想做哪一块"))

    state = store.load_state()
    assert state["awaiting"] == "preference"
    assert state["register"] is None


def test_task_list_names_the_source_assignment(env):
    """P1-E：清单首行点明这套卡来自哪份作业书。"""
    gateway, store, sender, _ = env
    _seed_m4(store)
    store.save_assignment(
        AssignmentMeta(
            course="软件系统设计",
            title="课程任务书",
            submission="源码 + 报告",
            deadline="",
            source_file="课程任务书.pdf",
        )
    )

    gateway.handle(_inbound("你想做哪一块"))

    assert sender.texts[0].startswith("当前任务卡来自《课程任务书》（3 张）")
