"""多群隔离的测试（2026-09-16 补审 §1.10）。

**背景（实测复现，`_audit_tmp\\repro_multigroup.py`）**：机器人可能同时待在好几个群里，
而 `data/` 是全局共享、只有一份（D-57）。原实现里 `state.group_chat_id` 是"**最后说话的群**"
（`app._remember_group()`：任何群消息都覆盖它），于是第二个群里**任意一句话**
（不用 @ 机器人、不用是组员）就能把广播目标改走 —— 实测后果：

  * 组员甲**私聊**的匿名提议（M5）被发到了别的群，本群什么都没看到（a/b）；
  * 组长在别的群 @「报告」，本场的总表 + 核对清单 + 甘特图发到那个群（c）；
  * 组长在别的群 @「重置」，窗口照样开（确认后就删掉本场，d）；
  * **非成员**在别的群 @「方向」，一句就起 30 秒 LLM 并开投票窗口（e）；
  * 别的群发的 PDF 会挤掉本群刚发的那个 `pending_file`（f）。

本文件覆盖的口径：**一场作业只有一个群**（`state.known_groups`，登记确认时写入；
老 state 退回 `group_chat_id`，所以升级前就有的场子也立刻受保护）。
非本场的群：只认「帮助」（问到用法照答）与「登记」（进场的唯一入口）；其余一律一行说明。

**离线**：不联网、不调真 LLM（stub）、不碰 `repo\\data\\`。
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.gateway import replies
from src.gateway.app import Gateway
from src.gateway.events import Inbound, Mention
from src.gateway.router import (
    group_allowed,
    known_group_ids,
    route,
)
from src.models import AssignmentMeta, Member, RubricPoint, Roster, TaskCard
from src.storage import (
    ASSIGNMENT,
    ASSIGNMENTS,
    CARDS,
    MEMBERS,
    PREFERENCES,
    RUBRIC,
    STATE,
    JsonStore,
)

BOT = "ou_bot_self"
BOT_NAME = "喵喵喵"
G1 = "oc_group_1_registered"        # 本场作业登记的群
G2 = "oc_group_2_other"             # 机器人也在的另一个群
NOW = datetime(2026, 9, 16, 20, 0)
_SRC = Path(__file__).resolve().parents[1] / "src"


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


def _inbound(text="", *, chat_type="group", chat_id=G1, sender="ou_leader", mention=True,
             mtype="text", file_name="", file_key="fk_1", mentions=None):
    _seq[0] += 1
    if mentions is None:
        mentions = (
            (Mention(key="@_user_1", open_id=BOT, name=BOT_NAME),) if mention else ()
        )
    return Inbound(
        chat_id=chat_id,
        chat_type=chat_type,
        message_type=mtype,
        text=text,
        mentions=tuple(mentions),
        sender_type="user",
        sender_open_id=sender,
        message_id=f"mm{_seq[0]}",
        file_name=file_name,
        file_key=file_key,
    )


def _state(**over):
    """一场"已登记在 G1"的 state（登记过 ⇒ known_groups = [G1]）。"""
    data = {"known_groups": [G1], "group_chat_id": G1}
    data.update(over)
    return data


# ---------- 1. 判据本身 ----------


def test_known_groups_falls_back_to_group_chat_id_for_old_state():
    """老 state（没有 known_groups）退回 group_chat_id —— 升级前就在跑的场子也受保护。"""
    assert known_group_ids({"known_groups": [G1]}) == [G1]
    assert known_group_ids({"group_chat_id": G1}) == [G1]
    assert known_group_ids({"known_groups": [G1], "group_chat_id": G2}) == [G1]
    assert known_group_ids({}) == []


def test_group_allowed_covers_the_three_pass_cases():
    # ① 还没认过群：保持原行为（不然第一次「登记」都做不了）
    assert group_allowed({}, G2) is True
    # ② 登记过的群
    assert group_allowed(_state(), G1) is True
    assert group_allowed(_state(), G2) is False
    # ③ 正在这个群里登记（换群/首次登记靠这条放行）
    registering = _state(awaiting="register", register={"stage": "collect", "chat_id": G2})
    assert group_allowed(registering, G2) is True
    assert group_allowed(registering, "oc_group_3") is False


# ---------- 2. 路由：别的群什么都不做 ----------


@pytest.mark.parametrize(
    "text",
    ["拆解", "作业书", "方向", "你想做哪一块", "报告", "重置", "完成 T1", "我想提议：随便"],
)
def test_foreign_group_gets_one_line_and_no_pipeline(text):
    outcome = route(_inbound(text, chat_id=G2), _state(), _roster(), has_rubric=True, now=NOW)
    assert [r.text for r in outcome.replies] == [replies.GROUP_NOT_THIS_SESSION]
    assert outcome.pipeline == ""            # ← 不起任何重活（M1/M2/M3/M7/重置都没有）
    assert outcome.state is None             # ← 一个字段都不写


def test_foreign_group_dm_is_unaffected():
    """私聊不受影响：DM 没有"群"这一层（identity 靠花名册）。"""
    outcome = route(_inbound("我想提议：换个方向", chat_type="p2p", chat_id="ou_dm"),
                    _state(), _roster(), now=NOW)
    assert [r.text for r in outcome.replies] == [
        replies.PROPOSAL_POSTED.format(text="换个方向"),
        replies.PROPOSAL_ACK,
    ]
    assert outcome.replies[0].chat_id == G1  # 仍然发回**本场**的群


def test_help_still_works_in_a_foreign_group():
    """「帮助」在别的群照答（只看一眼就回，不读不写 state、也不往别处发）。"""
    outcome = route(_inbound("帮助", chat_id=G2), _state(), _roster(), now=NOW)
    assert [r.text for r in outcome.replies] == [replies.HELP_TEXT]
    assert outcome.state is None


def test_register_is_still_the_way_in_from_another_group():
    """「登记」是本场的唯一入口 —— 在别的群也能发（这就是"换群"）。"""
    outcome = route(_inbound("登记", chat_id=G2), _state(), _roster(), now=NOW)
    assert [r.text for r in outcome.replies] == [replies.REGISTER_FORM]
    assert outcome.state["register"]["chat_id"] == G2


def test_foreign_group_file_is_not_cached():
    """别的群发的文件连缓存都不进（pending_file 只有一个槽位，不能让它顶掉本群的）。"""
    outcome = route(_inbound("", chat_id=G2, mtype="file", file_name="别的课.pdf"),
                    _state(), _roster(), now=NOW)
    assert [r.text for r in outcome.replies] == [replies.GROUP_NOT_THIS_SESSION]
    assert outcome.state is None


def test_non_member_cannot_start_m2_in_this_group():
    """§1.10 e：「方向」补上身份闸 —— 非成员不烧 token、不开窗口（原来谁 @ 都能起）。"""
    state = _state()
    stranger = _inbound("方向", sender="ou_stranger")
    outcome = route(stranger, state, _roster(), has_rubric=True, now=NOW)
    assert [r.text for r in outcome.replies] == [replies.VOTE_NOT_MEMBER]
    assert outcome.pipeline == ""
    assert outcome.state is None
    # 对照：成员发同一句照常起 M2
    ok = route(_inbound("方向"), state, _roster(), has_rubric=True, now=NOW)
    assert ok.pipeline == "direction"


# ---------- 3. 真 Gateway：广播目标不再被别群改写 ----------


class _Sender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []      # (chat_id, text)

    def send(self, message):
        self.sent.append((message.chat_id, message.text))
        return True

    def bot_info(self):
        return {"open_id": BOT, "app_name": BOT_NAME}

    def download(self, pending, target_dir):
        target = Path(target_dir) / (pending.get("file_name") or "作业书.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("作业书正文：要交一份报告。", encoding="utf-8")
        return target


@pytest.fixture
def env(tmp_path):
    store = JsonStore(tmp_path)
    store.ensure_dirs()
    store.save_members(_roster())
    store.save_rubric([RubricPoint(id="R1", quote="要交一份报告", observable="可核对", weight=100)])
    store.save_cards([
        TaskCard(task_id="T1", module_name="模块1", rubric_refs=["R1"], effort_hours=4.0,
                 deliverable="产物", acceptance="验收"),
        TaskCard(task_id="T2", module_name="模块2", rubric_refs=["R1"], effort_hours=4.0,
                 deliverable="产物", acceptance="验收"),
    ])
    store.save_assignment(AssignmentMeta(course="课程", title="作业", submission="交付",
                                         deadline="", source_file="作业书.txt"))
    store.save_state({"known_groups": [G1], "group_chat_id": G1})
    sender = _Sender()
    gateway = Gateway(config=None, store=store, sender=sender, downloader=sender)
    return gateway, store, sender


def test_other_group_chatter_cannot_steal_the_broadcast_target(env):
    """§1.10 a/b：别群一句闲聊不再改写 `group_chat_id` ⇒ 匿名提议仍发回本场群。"""
    gateway, store, sender = env

    gateway.handle(_inbound("这周末聚餐吗", chat_id=G2, sender="ou_stranger", mention=False))
    assert store.load_state()["group_chat_id"] == G1          # ← 没被改走

    gateway.handle(_inbound("我想提议：把周三的会挪到周四", chat_type="p2p", chat_id="ou_dm",
                            sender="ou_a"))
    posted = [item for item in sender.sent if item[0] == G1]
    assert any("把周三的会挪到周四" in text for _, text in posted)
    assert not any(chat_id == G2 for chat_id, _ in sender.sent)   # 别的群一个字都没收到


def test_legacy_state_without_known_groups_is_protected_too(env):
    """老 state（只有 group_chat_id）也要受保护 —— 靠 `known_group_ids()` 的回退。"""
    gateway, store, _ = env
    store.save_state({"group_chat_id": G1})                   # 模拟升级前写下的 state
    gateway.handle(_inbound("这周末聚餐吗", chat_id=G2, sender="ou_stranger", mention=False))
    assert store.load_state()["group_chat_id"] == G1


def test_reset_keeps_the_known_group(env):
    """重置/换作业书那套清理必须保住 `known_groups`，否则保护会退回宽松档。"""
    gateway, store, _ = env
    gateway._wipe_session_data()
    state = store.load_state()
    assert state["known_groups"] == [G1]
    assert state["group_chat_id"] == G1
    assert store.load_members() is not None                    # 花名册保留
    assert store.path(MEMBERS).exists()


def test_reminder_scan_skips_a_foreign_group(env):
    """§1.10：M6 催办宁可漏催，也不把 @ 发到不是本场的群。"""
    gateway, store, sender = env
    store.save_state({"known_groups": [G1], "group_chat_id": G2})   # 被写坏的 state
    due = gateway.scan_reminders(NOW)
    assert due == []
    assert not any(chat_id == G2 for chat_id, _ in sender.sent)


def test_register_confirm_pins_the_group(env):
    """登记确认 = 这个群就是本场的群（known_groups 与 group_chat_id 一起被钉住）。"""
    gateway, store, _ = env
    store.save_state({})                                        # 干净起盘
    gateway.handle(_inbound("登记", chat_id=G2))
    form = _inbound(
        "登记\n组长：@_user_1\n组员：@_user_2 @_user_3",
        chat_id=G2,
        mentions=(
            Mention(key="@_user_1", open_id="ou_leader", name="组长"),
            Mention(key="@_user_2", open_id="ou_a", name="组员甲"),
            Mention(key="@_user_3", open_id="ou_b", name="组员乙"),
        ),
    )
    gateway.handle(form)
    gateway.handle(_inbound("同意", chat_id=G2, mentions=()))
    state = store.load_state()
    assert state["known_groups"] == [G2]
    assert state["group_chat_id"] == G2
    # 从此别的群只能问用法/重新登记
    outcome = route(_inbound("报告", chat_id=G1), state, _roster(), now=NOW)
    assert [r.text for r in outcome.replies] == [replies.GROUP_NOT_THIS_SESSION]


# ---------- 4. 顺带修掉的时间炸弹（与本分支同批） ----------


def test_reset_confirm_exemption_follows_the_injected_now():
    """「确定重置」的 @ 门豁免必须**跟着传入的 now 走**，不许偷看墙上时间。

    原来 `_is_reset_reply()` 自己调 `datetime.now()`，`router._is_reset_reply` 这条链
    在 `route()` 手里 `now` 明明有值的情况下仍读墙上时间 ⇒ `test_reset.py` 里把时间冻在
    20:00 的用例，一到墙上时间 20:05（窗口 TTL 5 分钟）就**永久变红**。
    实测：同一个分支 19:36 全绿、20:06 起必挂（`feat/reset-command` 上原样复现，
    不是本次改动引入的）。这条用例把"传进来的 now 说了算"钉死。
    """
    from src.gateway.router import RESET_CONFIRM_WORD, RESET_WORD

    fresh = {"reset": {"kind": "wipe", "chat_id": G1, "requested_by": "ou_leader",
                       "opened_at": NOW.isoformat(timespec="seconds"),
                       "expires_at": (NOW + timedelta(minutes=5)).isoformat(timespec="seconds")}}
    # 窗口内 + 没 @ 机器人 ⇒ 豁免 @ 门，确认照走（哪怕墙上时间早就过了 20:05）
    passed = route(_inbound(RESET_CONFIRM_WORD, chat_id=G1, mention=False), fresh, _roster(),
                   bot_open_id=BOT, bot_name=BOT_NAME, now=NOW)
    assert passed.pipeline == "reset_confirm"

    # 反过来：把 now 推到窗口之外 ⇒ 豁免失效，没 @ 就被静默丢掉
    dropped = route(_inbound(RESET_CONFIRM_WORD, chat_id=G1, mention=False), fresh, _roster(),
                    bot_open_id=BOT, bot_name=BOT_NAME, now=NOW + timedelta(minutes=6))
    assert dropped.replies == () and dropped.pipeline == ""
    assert RESET_WORD != RESET_CONFIRM_WORD
