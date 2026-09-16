"""「重置」+「换 PDF 也要确认」的测试（第 11 条指令，2026-09-16）。

口径（用户拍板）：
  * 指令「重置」+ 确认词「确定重置」，**两段式**：第一步只列清单、一个字节都不删；
  * 「重置」**只认组长**；确认只认**发起那次重置的人**（换 PDF 那一路的发起人
    由 M1/M3 的身份闸保证是花名册成员）；
  * 只清**这一场**的产物：保留 `members.json`（花名册）与 `seen.json`（事件去重表）；
  * **先备份再删**（`data/.reset-backup/<时间戳>/`）；
  * **换 PDF 也走同一次确认**，而且顺序是"**先跑新的、成功了才清下游**"——
    新文件不合格时盘上一个字节都不动（D-49 ② 的精神）。

**离线**：不联网、不调真 LLM（stub）、不碰 `repo\\data\\`。
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.gateway import replies
from src.gateway.app import Gateway
from src.gateway.events import Inbound, Mention
from src.gateway.router import (
    RESET_CONFIRM_WORD,
    RESET_WORD,
    confirm_reset,
    request_reset,
    reset_block,
    reset_expired,
    route,
)
from src.models import AssignmentMeta, Member, RubricPoint, Roster, TaskCard
from src.storage import (
    ASSIGNMENT,
    ASSIGNMENTS,
    CARDS,
    DIRECTION,
    MEMBERS,
    PREFERENCES,
    RUBRIC,
    SEEN,
    STATE,
    UPLOADS,
    JsonStore,
)

BOT = "ou_bot_self"
BOT_NAME = "喵喵喵"
GROUP = "oc_group"
NOW = datetime(2026, 9, 16, 20, 0)
SRC = Path(__file__).resolve().parents[1] / "src"


def _roster():
    return Roster(
        leader="ou_leader",
        members=[
            Member(open_id="ou_leader", name="组长"),
            Member(open_id="ou_a", name="组员甲"),
        ],
        registered_at="2026-09-16T09:00:00",
        confirmed_by="ou_leader",
    )


_seq = [0]


def _inbound(text="", *, chat_type="group", chat_id=GROUP, sender="ou_leader", mention=True):
    _seq[0] += 1
    return Inbound(
        chat_id=chat_id,
        chat_type=chat_type,
        message_type="text",
        text=text,
        mentions=(Mention(key="@_user_1", open_id=BOT, name=BOT_NAME),) if mention else (),
        sender_type="user",
        sender_open_id=sender,
        message_id=f"m{_seq[0]}",          # 每条唯一：否则会被 P0-A 的事件去重跳过
    )


# ---------- 夹具：真 Gateway + 假发送器/下载器/LLM ----------


class _Sender:
    def __init__(self):
        self.texts: list[str] = []
        self.images: list[str] = []

    def send(self, message):
        self.texts.append(message.text)
        return True

    def bot_info(self):
        return {"open_id": BOT, "app_name": BOT_NAME}

    def download(self, pending, target_dir):
        # 用 .txt：夹具里写的是纯文本，用 .pdf 后缀会让 PyMuPDF 去解析而报 FileDataError
        name = pending.get("file_name") or "作业书.txt"
        target = Path(target_dir) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("作业书正文：要交一份报告。", encoding="utf-8")
        return target


class _StubLLM:
    def __init__(self, *, has_points=True):
        self.has_points = has_points

    def chat_json(self, system, user, parse, **kw):
        if "M1 输入解析" in system:
            return parse(
                {
                    "assignment": {
                        "course": "新课程",
                        "title": "新作业",
                        "submission": "新交付",
                        "deadline": "",
                    },
                    "rubric": (
                        # quote 必须是**下载到的正文里的原文**（M1 的防编造硬校验）
                        [
                            {"id": "R1", "quote": "要交一份报告", "weight": 100,
                             "observable": "可核对", "status": "normal"}
                        ]
                        if self.has_points
                        else []
                    ),
                }
            )
        return parse(
            {
                "cards": [
                    {"task_id": "T1", "module_name": "新卡", "rubric_refs": ["R1"],
                     "effort_hours": 4, "deliverable": "新产物", "acceptance": "新验收"}
                ]
            }
        )


def _seed_old_session(store: JsonStore) -> None:
    """摆一场"上一场作业"的产物（含下游），用来验证清得干不干净。"""
    store.save_assignment(
        AssignmentMeta(course="旧课程", title="旧作业", submission="旧交付",
                       deadline="", source_file="旧作业书.txt")
    )
    store.save_rubric([RubricPoint(id="R_old", quote="旧评分点", observable="旧", status="normal")])
    store.save_cards([TaskCard(task_id="T_seed", module_name="旧卡", rubric_refs=["R_old"],
                               effort_hours=4.0, deliverable="旧", acceptance="旧")])
    store.save_direction({"winner": {"id": 1, "title": "旧方向"}})
    store.save_preferences([])
    store.save_state({"group_chat_id": GROUP, "awaiting": "vote", "vote": {"votes": {"ou_a": 1}}})
    store.path("assignments.json").write_text(
        json.dumps([{"task_id": "T_seed", "assignee": "ou_a", "source": "auto"}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    (Path(store.uploads) / "旧作业书.txt").write_bytes(b"OLD BODY")


def _wait_for(predicate, *, seconds=3.0):
    """等后台线程把话说完（``run_pipeline`` 是另一个线程；测试里别抢跑）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def env(tmp_path):
    store = JsonStore(tmp_path)
    store.ensure_dirs()
    store.save_members(_roster())
    store.write_raw(SEEN, ["om_seen_1"])         # 事件去重表里有东西（重置不该清它）
    sender = _Sender()
    gateway = Gateway(config=None, store=store, sender=sender, downloader=sender)
    gateway._llm_client = _StubLLM()
    gateway._llm = lambda: gateway._llm_client   # type: ignore[method-assign]
    return gateway, store, sender


# ---------- 1. 单元：窗口与两端校验 ----------


def test_request_reset_is_group_and_leader_only():
    private = request_reset(_inbound(RESET_WORD, chat_type="p2p"), {}, _roster(), NOW)
    assert [r.text for r in private.replies] == [replies.RESET_NEED_GROUP]

    member = request_reset(_inbound(RESET_WORD, sender="ou_a"), {}, _roster(), NOW)
    assert [r.text for r in member.replies] == [replies.RESET_NEED_LEADER]

    leader = request_reset(_inbound(RESET_WORD), {}, _roster(), NOW)
    assert leader.pipeline == "reset_request"
    assert leader.state["reset"]["kind"] == "wipe"
    assert leader.state["reset"]["requested_by"] == "ou_leader"


def test_confirm_needs_the_original_requester():
    state = {"reset": reset_block("wipe", _inbound(RESET_WORD), NOW)}
    other = confirm_reset(_inbound(RESET_CONFIRM_WORD, sender="ou_a"), state, _roster(), NOW)
    assert [r.text for r in other.replies] == [replies.RESET_NEED_REQUESTER]
    assert other.pipeline == ""
    same = confirm_reset(_inbound(RESET_CONFIRM_WORD), state, _roster(), NOW)
    assert same.pipeline == "reset_confirm"


def test_confirm_needs_a_live_window_in_the_same_chat():
    assert confirm_reset(_inbound(RESET_CONFIRM_WORD), {}, _roster(), NOW).pipeline == ""

    stale = {"reset": reset_block("wipe", _inbound(RESET_WORD), NOW - timedelta(minutes=6))}
    assert reset_expired(stale["reset"], NOW) is True
    assert [r.text for r in confirm_reset(_inbound(RESET_CONFIRM_WORD), stale, _roster(), NOW).replies] \
        == [replies.RESET_NO_PENDING]

    elsewhere = {
        "reset": reset_block("wipe", _inbound(RESET_WORD, chat_id="oc_other"), NOW)
    }
    assert [r.text for r in confirm_reset(_inbound(RESET_CONFIRM_WORD), elsewhere, _roster(), NOW).replies] \
        == [replies.RESET_NO_PENDING]


def test_confirm_word_is_exempt_from_the_mention_gate_only_while_the_window_is_open():
    """同一个坑登记窗踩过（「同意」被 @ 门吃掉）—— 这里一开始就豁免。"""
    state = {"reset": reset_block("wipe", _inbound(RESET_WORD), NOW)}
    passed = route(
        _inbound(RESET_CONFIRM_WORD, mention=False), state, _roster(),
        bot_open_id=BOT, bot_name=BOT_NAME, now=NOW,
    )
    assert passed.pipeline == "reset_confirm"

    dropped = route(
        _inbound(RESET_CONFIRM_WORD, mention=False), {}, _roster(),
        bot_open_id=BOT, bot_name=BOT_NAME, now=NOW,
    )
    assert dropped.replies == () and dropped.pipeline == ""


def test_confirm_word_does_not_start_a_new_request():
    """「确定重置」不以「重置」开头 —— 两条前缀不打架（守卫）。"""
    assert not RESET_CONFIRM_WORD.startswith(RESET_WORD)
    outcome = route(
        _inbound(RESET_CONFIRM_WORD), {}, _roster(),
        bot_open_id=BOT, bot_name=BOT_NAME, now=NOW,
    )
    assert outcome.pipeline == ""          # 没有窗口 ⇒ 只回「现在没有等我确认的重置」


# ---------- 2. 「重置」端到端 ----------


def test_reset_request_lists_what_will_be_cleared_without_deleting(env):
    gateway, store, sender = env
    _seed_old_session(store)

    gateway.handle(_inbound(RESET_WORD), now=NOW)
    _wait_for(lambda: any("清之前先给你看清楚" in t for t in sender.texts))

    assert replies.RESET_REQUEST_HEADER in sender.texts[-1]
    assert "旧作业" in sender.texts[-1] and "旧方向" in sender.texts[-1]
    # 一个字节都没删
    assert store.load_assignment().title == "旧作业"
    assert (Path(store.uploads) / "旧作业书.txt").exists()


def test_reset_confirm_wipes_this_session_but_keeps_roster_and_seen(env):
    gateway, store, sender = env
    _seed_old_session(store)

    gateway.handle(_inbound(RESET_WORD), now=NOW)
    _wait_for(lambda: any("清之前先给你看清楚" in t for t in sender.texts))
    gateway.handle(_inbound(RESET_CONFIRM_WORD), now=NOW)
    _wait_for(lambda: any("已重置" in t for t in sender.texts))

    assert "已重置" in sender.texts[-1]
    for name in (ASSIGNMENT, RUBRIC, CARDS, DIRECTION, ASSIGNMENTS, PREFERENCES):
        assert not store.path(name).exists(), name
    assert not (Path(store.uploads) / "旧作业书.txt").exists()
    # 保留：花名册 / 事件去重表（老条目还在 —— 它本来就会随消息增长，重置不许清空它）/
    # 已认下的群
    assert store.load_members().leader == "ou_leader"
    assert "om_seen_1" in (store.read_raw(SEEN) or [])
    assert store.load_state() == {"group_chat_id": GROUP}
    # 备份里有旧产物
    backups = sorted((Path(store.root) / ".reset-backup").glob("*/*"))
    assert {p.name for p in backups if p.is_file()} >= {"assignment.json", "rubric.json", "cards.json"}


def test_reset_on_an_already_clean_board_says_so_and_clears_the_window(env):
    gateway, store, sender = env

    gateway.handle(_inbound(RESET_WORD), now=NOW)
    _wait_for(lambda: sender.texts != [])

    assert sender.texts[-1] == replies.RESET_NOTHING
    assert "reset" not in (store.load_state() or {})


# ---------- 3. 换 PDF 走同一次确认，而且"先跑成功、再清下游" ----------


def _pending(store, name="新作业书.txt"):
    state = store.load_state() or {}
    state["pending_file"] = {
        "file_key": "fk_new", "file_name": name, "resource_type": "file",
        "chat_id": GROUP, "message_id": "m_file",
        "received_at": NOW.isoformat(timespec="seconds"),
    }
    store.save_state(state)


def test_replacing_the_pdf_asks_before_overwriting(env):
    gateway, store, sender = env
    _seed_old_session(store)
    _pending(store)

    gateway.handle(_inbound("作业书"), now=NOW)
    _wait_for(lambda: any("换一份作业书会把它们整个换掉" in t for t in sender.texts))

    assert replies.RESET_REPLACE_HEADER in sender.texts[-1]
    assert store.load_assignment().title == "旧作业"       # 还没动
    assert not (Path(store.uploads) / "新作业书.txt").exists()   # 连下载都还没下（省 token）


def test_confirm_then_replace_runs_m1_and_clears_the_previous_downstream(env):
    gateway, store, sender = env
    _seed_old_session(store)
    _pending(store)

    gateway.handle(_inbound("作业书"), now=NOW)
    _wait_for(lambda: any("换一份作业书会把它们整个换掉" in t for t in sender.texts))
    gateway.handle(_inbound(RESET_CONFIRM_WORD), now=NOW)
    _wait_for(lambda: any("已重置" in t for t in sender.texts))

    # 新产物（三份）已覆盖
    assert store.load_assignment().title == "新作业"
    assert [p.id for p in store.load_rubric()] == ["R1"]
    assert [c.task_id for c in store.load_cards()] == ["T1"]
    # 上一场的下游被清掉（不然「报告」会把新旧混在一张表里）
    assert not store.path(DIRECTION).exists()
    assert not store.path(ASSIGNMENTS).exists()
    assert store.load_state() == {"group_chat_id": GROUP}
    # 上传目录：新那份留着（无评分点模式要按 source_file 取正文），旧的清掉
    uploads = sorted(p.name for p in Path(store.uploads).glob("*"))
    assert uploads == ["新作业书.txt"]


def test_failed_replace_keeps_the_whole_previous_session(env):
    """新文件抽不到评分点 ⇒ 盘上一个字节都不动（D-49 ② 的精神）。"""
    gateway, store, sender = env
    _seed_old_session(store)
    _pending(store)
    gateway._llm_client = _StubLLM(has_points=False)

    gateway.handle(_inbound("作业书"), now=NOW)
    _wait_for(lambda: any("换一份作业书会把它们整个换掉" in t for t in sender.texts))
    gateway.handle(_inbound(RESET_CONFIRM_WORD), now=NOW)
    _wait_for(lambda: any("旧产物原样没动" in t for t in sender.texts))

    assert replies.NO_RUBRIC_FOUND in sender.texts
    assert "旧产物原样没动" in sender.texts[-1]
    assert store.load_assignment().title == "旧作业"
    assert [p.id for p in store.load_rubric()] == ["R_old"]
    assert store.path(DIRECTION).exists()      # 下游也没被清
    assert (Path(store.uploads) / "旧作业书.txt").exists()


# ---------- 4. 文档一致性守卫 ----------


def test_help_and_command_list_mention_the_new_command():
    assert RESET_WORD in replies.HELP_TEXT
    assert RESET_WORD in replies.COMMAND_LIST_TEXT
    assert RESET_CONFIRM_WORD in replies.HELP_TEXT


# ---------- 5. 时间炸弹防线：TTL 判据必须跟着**注入的 now** 走 ----------


def test_pending_file_ttl_follows_the_injected_clock(env):
    """发文件的有效期由**传进来的 now** 说了算，不许偷看墙上时间（2026-09-16 修）。

    这一条是给两次同类事故上的锁：
      * `test_confirm_word_is_exempt_from_the_mention_gate_only_while_the_window_is_open`
        —— `_is_reset_reply()` 自己读 `datetime.now()`；
      * 本文件三条「换 PDF」用例 —— `_pending()` 把 `received_at` 写成冻结的 `NOW`，
        而 `PENDING_FILE_TTL`（30 分钟）拿墙上时间比 ⇒ **过了 20:30 必挂**
        （实测：同一条 `feat/reset-command` 分支上原样复现，不是合并引入的）。

    修法：`Gateway.handle(inbound, now=…)` 一路传给 `route()`。于是
    「同一份盘面 + 同一个 now」= 同一个结果，跑一万年也不变。
    """
    gateway, store, sender = env
    _seed_old_session(store)
    _pending(store)                                  # received_at = NOW（冻结）

    # ① 用同一个 now：文件算"新鲜"，走「换 PDF 要先确认」那条路
    gateway.handle(_inbound("作业书"), now=NOW)
    _wait_for(lambda: any("换一份作业书会把它们整个换掉" in t for t in sender.texts))
    assert replies.RESET_REPLACE_HEADER in sender.texts[-1]
    assert store.load_assignment().title == "旧作业"          # 还没动

    # ② 把 now 往后推 31 分钟（> 30 分钟有效期）：文件算过期 ⇒ 回「先把文件发给我」
    late = NOW + timedelta(minutes=31)
    gateway.handle(_inbound("作业书"), now=late)
    assert sender.texts[-1] == replies.FILE_MISSING
    assert store.load_assignment().title == "旧作业"          # 依然一个字节都没动


