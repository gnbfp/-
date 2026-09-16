"""方向投票窗口「没落定就被顶掉」时要说一句话（2026-09-16 真机发现 → D-61 补注）。

**真机现场**（2026-09-16 20:13–20:17，`人工测试记录-20260916.md` §7 坑 1）：
三个人分别投 `1` / `2` / `3` ⇒ **没有任何方向过半**；紧接着 20:17:48 的一句
「你想做哪一块」切到 M4 状态机，`preference.open_window` 把 `state.vote` **整块清掉** ——
群里**一句交代都没有**，`data/direction.json` 从未落盘，最后的报告里也就没有"已定方向"。

D-61 ③ 的关闭三条（过半 / 超时 / 封盘）当时**都不适用** ⇒ 这是它没定义的那个转移。
现在定的口径：切走时**先报票数明细**，并说明"这些票作废、要定就重发「方向」"。
（"要不要自动封盘 / 取票最多"是产品拍板的事，代码不替人拍板 —— D-36 同款纪律。）

**离线**：不联网、不调 LLM、不碰 `repo\\data\\`。
"""

from datetime import datetime, timedelta

from src.gateway import replies
from src.gateway.events import Inbound, Mention
from src.gateway.router import route
from src.gateway.vote import discarded_note
from src.models import Member, Roster, TaskCard

BOT = "ou_bot_self"
BOT_NAME = "喵喵喵"
GROUP = "oc_group"
LEADER = "ou_leader"
NOW = datetime(2026, 9, 16, 20, 0)
CANDIDATES = [
    {"id": 1, "title": "上中下游三采样点水质对比评估河流健康", "note": "", "rubric_refs": ["R1"]},
    {"id": 2, "title": "农田与居民区河段污染来源对比调查", "note": "", "rubric_refs": ["R1"]},
    {"id": 3, "title": "面向社区居民的河流健康科普报告", "note": "", "rubric_refs": ["R1"]},
]


def _roster():
    return Roster(
        leader=LEADER,
        members=[
            Member(open_id=LEADER, name="组长"),
            Member(open_id="ou_a", name="组员甲"),
            Member(open_id="ou_b", name="组员乙"),
        ],
        registered_at="2026-09-16T09:00:00",
        confirmed_by=LEADER,
    )


def _cards():
    return [
        TaskCard(task_id=f"T{i}", module_name=f"模块{i}", rubric_refs=["R1"], effort_hours=4.0,
                 deliverable="产物", acceptance="验收")
        for i in (1, 2, 3)
    ]


_seq = [0]


def _inbound(text="", *, sender=LEADER, chat_id=GROUP, mention=True):
    _seq[0] += 1
    return Inbound(
        chat_id=chat_id,
        chat_type="group",
        message_type="text",
        text=text,
        mentions=(Mention(key="@_user_1", open_id=BOT, name=BOT_NAME),) if mention else (),
        sender_type="user",
        sender_open_id=sender,
        message_id=f"fv{_seq[0]}",
    )


def _state(*, votes=None, closed=False, group=GROUP, awaiting="vote"):
    block = {
        "chat_id": group,
        "opened_at": NOW.isoformat(timespec="seconds"),
        "opened_by": LEADER,
        "candidates": [dict(item) for item in CANDIDATES],
        "votes": dict(votes or {}),
    }
    if closed:
        block["closed"] = True
    return {"awaiting": awaiting, "vote": block, "group_chat_id": group}


def _texts(outcome):
    return [r.text for r in outcome.replies]


# ---------- 该说话的时候 ----------


def test_switching_to_m4_says_the_votes_were_discarded():
    """真机同款：窗口里有票、没落定，一句「你想做哪一块」把它顶掉 → 必须先交代。"""
    state = _state(votes={"ou_a": 1})
    outcome = route(_inbound("你想做哪一块"), state, _roster(), cards=_cards(), now=NOW)

    note = _texts(outcome)[0]
    assert note.startswith("▲ 上一个方向投票还没过半")
    assert "1 号 1 票" in note                       # 票数明细要报出来
    assert "作废" in note                            # 并且说清后果
    assert outcome.state["vote"] is None             # 窗口确实被切走了
    assert outcome.state["awaiting"] == "preference"  # 且新窗口开起来了


def test_the_note_goes_to_the_vote_group_not_the_sender_chat():
    """交代要发到**开窗那个群**（票是那个群的），不是这条消息的所在会话。"""
    state = _state(votes={"ou_a": 2}, group="oc_vote_group")
    state["group_chat_id"] = "oc_vote_group"
    outcome = route(
        _inbound("你想做哪一块", chat_id="oc_vote_group"), state, _roster(),
        cards=_cards(), now=NOW,
    )
    assert outcome.replies[0].chat_id == "oc_vote_group"


def test_two_votes_at_different_numbers_also_get_explained():
    """1/2 各一票同样没落定 —— 也要交代（这才是真机那一幕的样子）。"""
    state = _state(votes={"ou_a": 1, "ou_b": 2})
    outcome = route(_inbound("你想做哪一块"), state, _roster(), cards=_cards(), now=NOW)
    note = _texts(outcome)[0]
    assert "1 号 1 票" in note and "2 号 1 票" in note


def test_discarded_note_helper_is_the_single_decision_point():
    """判据只有一处：``vote.discarded_note()``（没窗口 / 没票 / 已冻住 / 已过期 → 不说话）。"""
    assert discarded_note({}, NOW) == ""
    assert discarded_note(_state().get("vote"), NOW) == ""                      # 一张票都没有
    assert discarded_note(_state(votes={"ou_a": 1}).get("vote"), NOW) != ""
    assert discarded_note(_state(votes={"ou_a": 1}, closed=True).get("vote"), NOW) == ""
    stale = _state(votes={"ou_a": 1}).get("vote")
    stale["opened_at"] = (NOW - timedelta(minutes=11)).isoformat(timespec="seconds")
    assert discarded_note(stale, NOW) == ""                                     # 过期归超时那条路


# ---------- 不该说话的时候 ----------


def test_no_note_when_nobody_voted():
    """一张票都没投过 ⇒ 没有"谁的票被作废"，不必刷屏（但状态机照常切换）。"""
    outcome = route(_inbound("你想做哪一块"), _state(), _roster(), cards=_cards(), now=NOW)
    assert outcome.replies[0].text.startswith("任务卡清单（回复序号即可")   # 第一句就是正常清单
    assert "作废" not in "".join(_texts(outcome))
    assert outcome.state["awaiting"] == "preference"


def test_no_note_after_the_timeout_freeze():
    """超时冻住时 `VOTE_TIMEOUT` 已经报过明细 ⇒ 别再重复刷屏（冻住的窗口等组长拍板）。"""
    outcome = route(_inbound("你想做哪一块"), _state(votes={"ou_a": 1}, closed=True),
                    _roster(), cards=_cards(), now=NOW)
    assert "作废" not in "".join(_texts(outcome))


def test_no_note_when_the_command_does_not_take_over_the_window():
    """「拆解」这类不碰窗口的指令：窗口与票**原样留着**，也不该冒出交代。"""
    state = _state(votes={"ou_a": 1})
    outcome = route(_inbound("拆解"), state, _roster(), has_rubric=True, now=NOW)
    assert outcome.pipeline == "decompose"
    assert outcome.state is None                       # 没改状态 ⇒ 窗口还在
    assert "作废" not in "".join(_texts(outcome))


def test_the_note_is_not_repeated_on_later_messages():
    """交代只出现一次：切走之后窗口已经没了，后面的消息不该再提。"""
    state = _state(votes={"ou_a": 1})
    route(_inbound("你想做哪一块"), state, _roster(), cards=_cards(), now=NOW)
    after = _state(awaiting="preference", votes={})
    after["vote"] = None
    follow = route(_inbound("完成 T1"), after, _roster(), cards=_cards(), now=NOW)
    assert "作废" not in "".join(_texts(follow))
