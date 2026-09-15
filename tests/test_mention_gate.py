"""群里必须 @机器人才响应（§8.1 备选方案 1 → D-69）+ 无参「完成」与结算私聊（D-70）。

纯路由单测：不碰飞书、不碰网络。

规则（需求方 2026-09-15 拍板）：
  * 群里的**文字**消息必须 @ 到机器人自己，否则**静默丢弃**（治"组员讨论时误触发"）；
  * **文件 / 图片免 @**（飞书不允许"文字 + 附件"同一条，发文件时根本 @ 不上）；
  * **私聊一律免 @**；
  * **登记表单豁免**（它必须 @ 组员，形状见 D-45）；
  * 机器人自身标识拿不到 → **宽松放行**（宁可偶尔误触发，不让机器人变哑巴）。
"""

from datetime import datetime

from src.gateway import replies
from src.gateway.events import Inbound, Mention
from src.gateway.router import route
from src.models import AssignmentRecord, Member, Roster, TaskCard

BOT = "ou_bot_self"
GROUP = "oc_g"
NOW = datetime(2026, 9, 15, 20, 0, 0)


def _mention(open_id=BOT, name="喵喵喵", key="@_user_1"):
    return Mention(key=key, open_id=open_id, name=name)


def _inbound(text="", *, chat_type="group", mentions=(), **over):
    data = dict(
        chat_id=GROUP if chat_type == "group" else "ou_user",
        chat_type=chat_type,
        message_type="text",
        text=text,
        mentions=tuple(mentions),
        sender_type="user",
        sender_open_id="ou_zhang",
        message_id="m1",
    )
    data.update(over)
    return Inbound(**data)


def _roster():
    return Roster(
        leader="ou_zhang",
        members=[
            Member(open_id="ou_zhang", name="张三"),
            Member(open_id="ou_li", name="李四"),
        ],
        registered_at="2026-09-15T09:00:00",
        confirmed_by="ou_zhang",
    )


def _cards():
    return [
        TaskCard(
            task_id="T1",
            module_name="模块1",
            rubric_refs=["R1"],
            effort_hours=1.0,
            deliverable="交付物",
            acceptance="验收标准",
        ),
        TaskCard(
            task_id="T2",
            module_name="模块2",
            rubric_refs=["R1"],
            effort_hours=2.0,
            deliverable="交付物",
            acceptance="验收标准",
        ),
    ]


def _assignments():
    return [
        AssignmentRecord(task_id="T1", assignee="ou_zhang", source="auto"),
        AssignmentRecord(task_id="T2", assignee="ou_zhang", source="auto"),
    ]


def _vote_state():
    return {
        "awaiting": "vote",
        "group_chat_id": GROUP,
        "vote": {
            "chat_id": GROUP,
            "opened_at": NOW.isoformat(timespec="seconds"),
            "opened_by": "ou_zhang",
            "candidates": [{"id": 1, "title": "方向A"}, {"id": 2, "title": "方向B"}],
            "votes": {},
        },
    }


# ---------- 群里：文字必须 @ ----------


def test_group_command_without_mention_is_silent():
    """组员讨论时打出的指令词，不 @ 就一个字都不回（D-69 的正题）。"""
    outcome = route(
        _inbound("拆解"),
        {},
        _roster(),
        has_rubric=True,
        bot_open_id=BOT,
        bot_name="喵喵喵",
    )
    assert outcome.replies == ()
    assert outcome.pipeline == ""            # 也不许偷偷起重活（必修 4 同源）


def test_group_command_mentioning_the_bot_is_handled():
    outcome = route(
        _inbound("@_user_1 拆解", mentions=[_mention()]),
        {},
        _roster(),
        has_rubric=True,
        bot_open_id=BOT,
        bot_name="喵喵喵",
    )
    assert [r.text for r in outcome.replies] == [replies.DECOMPOSING]
    assert outcome.pipeline == "decompose"


def test_group_command_mentioning_someone_else_is_silent():
    """@ 的是队友，不是机器人 → 同样静默（这是"必须 @"而不是"必须有 @"）。"""
    outcome = route(
        _inbound("@_user_2 拆解", mentions=[_mention(open_id="ou_li", name="李四", key="@_user_2")]),
        {},
        _roster(),
        has_rubric=True,
        bot_open_id=BOT,
        bot_name="喵喵喵",
    )
    assert outcome.replies == ()


def test_mention_matched_by_name_when_open_id_is_unknown():
    """只知道名字（open_id 没取到）时，按 name 兜一次。"""
    outcome = route(
        _inbound("@_user_1 拆解", mentions=[_mention()]),
        {},
        _roster(),
        has_rubric=True,
        bot_open_id="",
        bot_name="喵喵喵",
    )
    assert [r.text for r in outcome.replies] == [replies.DECOMPOSING]


def test_loose_mode_when_bot_identity_is_unknown():
    """两个标识都拿不到（启动取标识失败）→ 宽松放行，机器人不能变哑巴。"""
    outcome = route(
        _inbound("拆解"),
        {},
        _roster(),
        has_rubric=True,
        bot_open_id="",
        bot_name="",
    )
    assert [r.text for r in outcome.replies] == [replies.DECOMPOSING]


def test_group_digits_without_mention_are_not_votes():
    """§7.1 那条"闲聊数字被计票"的残留风险，靠这道门根治。"""
    outcome = route(
        _inbound("1"),
        _vote_state(),
        _roster(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
        now=NOW,
    )
    assert outcome.replies == ()
    assert outcome.save_direction is None


def test_group_digit_mentioning_the_bot_is_still_a_vote():
    outcome = route(
        _inbound("@_user_1 1", mentions=[_mention()]),
        _vote_state(),
        _roster(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
        now=NOW,
    )
    # 花名册 2 人 → 门槛 1、且 1 票严格过半 ⇒ 这一票会当场落定（D-36）。
    # 这条测试只关心"**@ 了机器人**的数字**被计了**"，所以先看回执、再看它确实落了盘。
    assert outcome.replies[0].text == replies.VOTE_ACK.format(id=1, title="方向A")
    assert outcome.save_direction is not None


# ---------- 免 @ 的三类 ----------


def test_group_file_without_mention_is_still_cached():
    """飞书不允许"文字 + 附件"同一条 ⇒ 发文件时 @ 不上，但必须能读（D-42 / D-69）。"""
    outcome = route(
        _inbound(
            "",
            message_type="file",
            file_key="file_v3_abc",
            file_name="作业书.pdf",
        ),
        {},
        _roster(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
    )
    pending = (outcome.state or {}).get("pending_file") or {}
    assert pending.get("file_key") == "file_v3_abc"


def test_group_image_without_mention_still_replies():
    outcome = route(
        _inbound("", message_type="image"),
        {},
        _roster(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
    )
    assert [r.text for r in outcome.replies] == [replies.IMAGE_REJECTED]


def test_private_message_needs_no_mention():
    outcome = route(
        _inbound("你想做哪一块", chat_type="p2p"),
        {},
        _roster(),
        cards=_cards(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
    )
    assert outcome.replies and "任务卡清单" in outcome.replies[0].text


def test_register_form_without_bot_mention_passes_the_gate():
    """表单必须 @ 组员 ⇒ 豁免 @ 门（否则组长得把机器人再 @ 一遍）。"""
    state = {
        "awaiting": "register",
        "group_chat_id": GROUP,
        "register": {
            "stage": "collect",
            "initiator_open_id": "ou_zhang",
            "leader": None,
            "members": [],
            "expires_at": "2026-09-15T20:05:00",
        },
    }
    outcome = route(
        _inbound(
            "登记\n组长：@_user_2\n组员：@_user_3",
            mentions=[
                _mention(open_id="ou_li", name="李四", key="@_user_2"),
                _mention(open_id="ou_wang", name="王五", key="@_user_3"),
            ],
        ),
        state,
        _roster(),
        bot_open_id=BOT,
        bot_name="喵喵喵",
        now=NOW,
    )
    # 被 @ 门放行 ⇒ 至少留下了痕迹（回话或状态），不是"静默丢弃"的那个空 Outcome
    assert outcome.replies or outcome.state


# ---------- 无参「完成」（D-70）----------


def test_bare_complete_lists_my_cards():
    outcome = route(
        _inbound("完成", chat_type="p2p"),
        {},
        _roster(),
        cards=_cards(),
        assignments=_assignments(),
    )
    text = outcome.replies[0].text
    assert "你手上是" in text and "T1" in text and "T2" in text
    assert "完成 T1" in text                      # 顺手教会怎么写


def test_bare_complete_in_group_asks_for_dm():
    outcome = route(_inbound("完成"), {}, _roster())
    assert [r.text for r in outcome.replies] == [replies.COMPLETE_NEED_DM]


def test_bare_complete_without_assignments_says_so():
    outcome = route(
        _inbound("完成", chat_type="p2p"), {}, _roster(), cards=_cards()
    )
    assert [r.text for r in outcome.replies] == [replies.COMPLETE_NEED_ASSIGNMENTS]


def test_bare_complete_suggests_a_pending_card_first():
    """都标完了 / 有已完成的卡时，给的示例编号要指向**还没标**的那张。"""
    assignments = [
        AssignmentRecord(
            task_id="T1",
            assignee="ou_zhang",
            source="auto",
            completed_at="2026-09-15T10:00:00",
        ),
        AssignmentRecord(task_id="T2", assignee="ou_zhang", source="auto"),
    ]
    outcome = route(
        _inbound("完成", chat_type="p2p"),
        {},
        _roster(),
        cards=_cards(),
        assignments=assignments,
    )
    text = outcome.replies[0].text
    assert "完成 T2" in text                       # T2 还没标 → 拿它当例子
    assert "已完成：T1" in text


def test_bare_complete_does_not_shadow_the_real_command():
    """「完成 T1」仍然走原来的精确匹配，不被无参分支吃掉。"""
    outcome = route(
        _inbound("完成 T1", chat_type="p2p"),
        {},
        _roster(),
        cards=_cards(),
        assignments=_assignments(),
    )
    assert [r.text for r in outcome.replies] == [
        replies.COMPLETE_OK.format(task_id="T1", module="模块1")
    ]
    assert outcome.save_complete["task_id"] == "T1"
