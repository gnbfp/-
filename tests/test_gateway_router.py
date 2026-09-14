"""M0 路由单测（§7.1 / D-33 / D-42，方案 §4 / §9）。不碰飞书、不碰网络。"""

from datetime import datetime, timedelta

from src.gateway import replies
from src.gateway.events import Inbound, Mention, Outcome
from src.gateway.router import (
    COMPLETE_PATTERN,
    PROPOSAL_PREFIXES,
    remember_file,
    route,
    strip_mentions,
)
from src.models import AssignmentRecord, Member, Roster, TaskCard


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


def _texts(outcome):
    return [r.text for r in outcome.replies]


def _m4_fixtures():
    """M4 要的三张卡 + 三个人（花名册顺序就是兜底并列时的先后）。"""
    cards = [
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
    roster = Roster(
        leader="ou_zhang",
        members=[
            Member(open_id=open_id, name=name)
            for open_id, name in (("ou_zhang", "张三"), ("ou_li", "李四"), ("ou_wang", "王五"))
        ],
        registered_at="2026-09-13T09:00:00",
        confirmed_by="ou_zhang",
    )
    return cards, roster


# ---------- 剥 @段 ----------


def test_strip_mentions_removes_placeholder():
    assert strip_mentions("@_user_1 拆解").strip() == "拆解"


def test_strip_mentions_uses_mention_keys():
    mentions = (Mention(key="@_user_9", open_id="ou_bot", name="机器人"),)
    assert strip_mentions("@_user_9 作业书", mentions).strip() == "作业书"


def test_mentions_survive_stripping():
    mentions = (Mention(key="@_user_1", open_id="ou_zhang", name="张三"),)
    inbound = _inbound("@_user_1 登记", mentions=mentions)
    assert route(inbound, {}, None).state["awaiting"] == "register"
    assert inbound.mentions[0].open_id == "ou_zhang"


# ---------- 自消息 / 文件消息 ----------


def test_bot_own_message_is_dropped():
    outcome = route(_inbound("拆解", sender_type="app"), {}, None)
    assert outcome == Outcome()


def test_file_message_is_cached_not_processed():
    inbound = _inbound("", message_type="file", file_key="fk_1", file_name="作业书.pdf")
    outcome = route(inbound, {"awaiting": None}, None)
    assert outcome.state["pending_file"]["file_key"] == "fk_1"
    assert "作业书.pdf" in _texts(outcome)[0]
    assert outcome.download_file_key == ""          # 下载归 app 层


def test_image_gets_a_rejection_reply():
    """用户 2026-09-12 拍板：图片回一句短拒收，但仍然**不入缓存**。"""
    outcome = route(_inbound("", message_type="image", file_key="ik_1"), {}, None)
    assert _texts(outcome) == [replies.IMAGE_REJECTED]
    assert outcome.state is None                    # 不写 state ⇒ 缓存没被动过
    assert outcome.pipeline == ""


def test_image_does_not_evict_a_cached_file():
    """必修 3 的现场：先发 PDF 再接一张图，缓存里必须还是那个 PDF。"""
    state = route(
        _inbound("", message_type="file", file_key="fk_1", file_name="作业书.pdf"), {}, None
    ).state
    outcome = route(_inbound("", message_type="image", file_key="ik_1"), state, None)
    assert outcome.state is None                    # 图片不写 state ⇒ 缓存没被动过
    assert state["pending_file"]["file_key"] == "fk_1"


def test_other_message_types_are_ignored():
    assert route(_inbound("", message_type="sticker"), {}, None) == Outcome()


# ---------- 缓存文件的有效期（D-46）----------

NOW = datetime(2026, 9, 12, 13, 30, 0)


def _pending(minutes_ago: int) -> dict:
    """一个"minutes_ago 分钟前收到"的缓存文件。"""
    return {
        "file_key": "fk_1",
        "file_name": "作业书.pdf",
        "resource_type": "file",
        "chat_id": "c1",
        "message_id": "m1",
        "received_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds"),
    }


def test_stale_pending_file_is_ignored():
    """隔了一场再发「作业书」，不该静默复用上一场的文件（D-46）。"""
    outcome = route(_inbound("作业书"), {"pending_file": _pending(31)}, None, now=NOW)
    assert _texts(outcome) == [replies.FILE_MISSING]
    assert outcome.pipeline == ""


def test_fresh_pending_file_still_works():
    for minutes in (0, 29, 30):           # 恰好 30 分钟还算新鲜（TTL 判的是"超过"）
        outcome = route(_inbound("作业书"), {"pending_file": _pending(minutes)}, None, now=NOW)
        assert _texts(outcome) == [replies.PARSING], minutes
        assert outcome.pipeline == "assignment"


def test_pending_file_without_timestamp_stays_usable():
    """老 state 没有 received_at：不因为缺字段就失效。"""
    outcome = route(_inbound("作业书"), {"pending_file": {"file_key": "fk_1"}}, None, now=NOW)
    assert _texts(outcome) == [replies.PARSING]
    assert outcome.pipeline == "assignment"


# ---------- 缓存文件的会话隔离（D-47）----------


def _cached_in(chat_id: str, minutes_ago: int = 0) -> dict:
    pending = _pending(minutes_ago)
    pending["chat_id"] = chat_id
    return {"pending_file": pending}


def test_a_file_cached_in_another_chat_is_not_used():
    """D-42 ④ 的演示方式就是各自私聊投递 ⇒ 没有会话隔离必然解析错人的作业书。"""
    outcome = route(
        _inbound("作业书", chat_id="c_me"), _cached_in("c_someone_else"), None, now=NOW
    )
    assert _texts(outcome) == [replies.FILE_MISSING]
    assert outcome.pipeline == ""


def test_a_file_cached_in_my_own_chat_is_used():
    outcome = route(_inbound("作业书", chat_id="c_me"), _cached_in("c_me"), None, now=NOW)
    assert _texts(outcome) == [replies.PARSING]
    assert outcome.pipeline == "assignment"


def test_pending_file_without_chat_id_stays_usable():
    """旧 state 没有 chat_id：不因为缺字段就失效（与 TTL 的防御同款）。"""
    pending = {key: value for key, value in _pending(0).items() if key != "chat_id"}
    outcome = route(_inbound("作业书", chat_id="c_me"), {"pending_file": pending}, None, now=NOW)
    assert _texts(outcome) == [replies.PARSING]
    assert outcome.pipeline == "assignment"


# ---------- 7 条前缀 ----------


def test_assignment_without_pending_file_asks_for_it():
    assert _texts(route(_inbound("作业书"), {}, None)) == [replies.FILE_MISSING]


def test_assignment_with_pending_file_acks_and_leaves_state():
    state = {"pending_file": {"file_key": "fk_1"}}
    outcome = route(_inbound("作业书"), state, None)
    assert _texts(outcome) == [replies.PARSING]
    assert outcome.state is None                    # 谁清 pending_file：app 层跑完再清


def test_decompose_without_rubric_points_to_assignment():
    assert _texts(route(_inbound("拆解"), {}, None, has_rubric=False)) == [replies.NEEDS_RUBRIC]


def test_decompose_with_rubric_acks():
    assert _texts(route(_inbound("拆解"), {}, None, has_rubric=True)) == [replies.DECOMPOSING]


def test_direction_without_rubric_points_to_assignment():
    """M2：没有评分点就不生成候选（D-48 口径），也不起重活。"""
    outcome = route(_inbound("方向"), {}, None, has_rubric=False)
    assert _texts(outcome) == [replies.NEEDS_RUBRIC]
    assert outcome.pipeline == ""


def test_preference_command_without_cards_points_to_assignment():
    """M4：没有任务卡就先去拆作业书（占位文案已随 M4 落地删掉）。"""
    assert _texts(route(_inbound("你想做哪一块"), {}, None)) == [replies.PREFERENCE_NEED_CARDS]


def test_proposal_accepts_both_colon_widths():
    """M5：全角 / 半角冒号都认，正文**原样**转达，且原样落盘留痕。"""
    for prefix in PROPOSAL_PREFIXES:
        outcome = route(_inbound(prefix + "加一个图表"), {"group_chat_id": "c1"}, None)
        assert _texts(outcome) == [
            "有组员提议：加一个图表",
            replies.PROPOSAL_ACK,
        ]
        assert outcome.save_proposal["text"] == "加一个图表"


def test_complete_matches_tn_with_spaces_and_case():
    """容忍空格与大小写；群里发只回"去私聊"（M6 §2.1）。"""
    for text in ("完成 T3", "完成T3", "完成 t7", "完成  T12"):
        assert COMPLETE_PATTERN.match(text)
        assert _texts(route(_inbound(text), {}, None)) == [replies.COMPLETE_NEED_DM]


def test_complete_requires_the_whole_command():
    """"完成 T3 谢谢" 不当成标记完成 —— 整句必须就是这条指令，编号不许猜。"""
    assert not COMPLETE_PATTERN.match("完成 T3 谢谢")
    assert _texts(route(_inbound("完成 T3 谢谢"), {}, None)) == [replies.COMMAND_LIST_TEXT]


def test_complete_uses_the_assignments_the_app_layer_loaded():
    """router 是纯函数：分配记录由 app 层传进来，私聊标记才认得出"是你的卡"。"""
    cards, roster = _m4_fixtures()
    outcome = route(
        _inbound("完成 T1", chat_type="p2p"),
        {},
        roster,
        cards=cards,
        assignments=[AssignmentRecord("T1", "ou_user", "volunteer_1")],
        now=datetime(2026, 9, 14, 10, 0, 0),
    )
    assert outcome.save_complete == {
        "task_id": "T1",
        "completed_at": "2026-09-14T10:00:00",
    }


def _report_fixtures():
    cards, roster = _m4_fixtures()               # 组长 = ou_zhang
    return cards, roster, [AssignmentRecord("T1", "ou_zhang", "volunteer_1")]


def test_report_is_leader_only_and_group_only():
    """M7 触发点 = 方案 A（D-64）：私聊不发报告，组员要报告只回一句。"""
    cards, roster, assignments = _report_fixtures()
    common = dict(cards=cards, assignments=assignments)
    assert _texts(
        route(_inbound("报告", chat_type="p2p", sender_open_id="ou_zhang"), {}, roster, **common)
    ) == [replies.REPORT_NEED_GROUP]
    assert _texts(route(_inbound("报告", sender_open_id="ou_li"), {}, roster, **common)) == [
        replies.REPORT_NEED_LEADER
    ]


def test_report_needs_roster_then_assignments():
    cards, roster, assignments = _report_fixtures()
    assert _texts(route(_inbound("报告", sender_open_id="ou_zhang"), {}, None, cards=cards)) == [
        replies.REPORT_NEED_ROSTER
    ]
    assert _texts(
        route(_inbound("报告", sender_open_id="ou_zhang"), {}, roster, cards=cards)
    ) == [replies.REPORT_NEED_ASSIGNMENTS]


def test_report_by_the_leader_starts_the_pipeline():
    cards, roster, assignments = _report_fixtures()
    outcome = route(
        _inbound("报告", sender_open_id="ou_zhang"),
        {},
        roster,
        cards=cards,
        assignments=assignments,
    )
    assert _texts(outcome) == [replies.REPORT_GENERATING]
    assert outcome.pipeline == "report"


def test_register_starts_the_state_machine():
    outcome = route(_inbound("登记"), {}, None)
    assert outcome.state["awaiting"] == "register"
    assert replies.REGISTER_FORM in _texts(outcome)[0]


def test_unmatched_text_returns_command_list():
    assert _texts(route(_inbound("今天天气不错"), {}, None)) == [replies.COMMAND_LIST_TEXT]


# ---------- 状态优先 ----------


def test_awaiting_vote_without_a_window_falls_through():
    """脏 state（awaiting=vote 但没有窗口块）不能吃掉消息：照走兜底文案。

    真正的收票 / 落定在 tests/test_vote.py；这里只保底"没有窗口就放行"。
    """
    outcome = route(_inbound("2"), {"awaiting": "vote"}, None)
    assert _texts(outcome) == [replies.COMMAND_LIST_TEXT]


def test_awaiting_preference_only_counts_digits_in_p2p():
    """D-54：窗口开着时**群里的裸数字不算志愿**（照走兜底文案），私聊才算。

    窗口长达 5 小时、而 awaiting 是全局的 —— 不限定会话的话，群里谁打一个数字
    都会被记成志愿序号。
    """
    state = {
        "awaiting": "preference",
        "preference": {"opened_at": NOW.isoformat(timespec="seconds")},
        "group_chat_id": "c1",
    }
    cards, roster = _m4_fixtures()

    assert _texts(route(_inbound("3"), state, roster, cards=cards, now=NOW)) == [
        replies.COMMAND_LIST_TEXT
    ]
    outcome = route(
        _inbound("3", chat_type="p2p", sender_open_id="ou_li"),
        state,
        roster,
        cards=cards,
        now=NOW,
    )
    assert outcome.save_preference["ranked_task_ids"] == ["T3"]
    assert outcome.pipeline == ""


def test_awaiting_register_routes_into_register_machine():
    state = {"awaiting": "register", "register": {"stage": "confirm", "expires_at": None}}
    outcome = route(_inbound("不同意"), state, None)
    assert _texts(outcome) == [replies.REGISTER_CANCELLED]


def test_register_form_sees_raw_text_with_mention_placeholders():
    """回归：登记表单必须拿到带 @ 占位符的原文，否则解析不出人（D-34）。"""
    state = {"awaiting": "register", "register": {"stage": "collect", "expires_at": None}}
    mentions = (
        Mention(key="@_user_1", open_id="ou_zhang", name="张三"),
        Mention(key="@_user_2", open_id="ou_li", name="李四"),
        Mention(key="@_user_3", open_id="ou_wang", name="王五"),
    )
    form = "登记\n组长：@_user_1\n组员：@_user_2 @_user_3"
    outcome = route(_inbound(form, mentions=mentions), state, None)
    assert outcome.state["register"]["stage"] == "confirm"
    assert outcome.state["register"]["leader"]["open_id"] == "ou_zhang"


def test_number_outside_waiting_state_is_not_a_command():
    assert _texts(route(_inbound("2"), {}, None)) == [replies.COMMAND_LIST_TEXT]


# ---------- 登记窗口不是死锁（必修 1）----------


def test_collect_stage_lets_plain_commands_through():
    """死锁回归：collect 阶段没有 @ 的消息必须照常走 7 条前缀。

    修之前：发一次「登记」不填表，全群的指令都被吃掉、且永不超时。
    """
    state = {"awaiting": "register", "register": {"stage": "collect", "expires_at": None}}
    assert _texts(route(_inbound("方向"), state, None)) == [replies.NEEDS_RUBRIC]
    assert _texts(route(_inbound("作业书"), state, None)) == [replies.FILE_MISSING]
    assert _texts(route(_inbound("今天天气不错"), state, None)) == [replies.COMMAND_LIST_TEXT]
    # 有缓存文件时照常干活：窗口不吃指令
    with_file = {**state, "pending_file": {"file_key": "fk_1", "message_id": "m0"}}
    outcome = route(_inbound("作业书"), with_file, None)
    assert _texts(outcome) == [replies.PARSING]
    assert outcome.pipeline == "assignment"


def test_register_then_nonsense_returns_command_list():
    """D5 验收第 5 步：发过「登记」之后再发一句胡话，要回指令列表。"""
    state = route(_inbound("登记"), {}, None).state
    outcome = route(_inbound("今天天气不错"), state, None)
    assert _texts(outcome) == [replies.COMMAND_LIST_TEXT]
    assert outcome.state is None                      # 还留在窗口里，等填表或取消


def test_escape_word_gets_out_of_the_register_window():
    """必修 1：显式逃生词必须有出口。"""
    state = route(_inbound("登记"), {}, None).state
    outcome = route(_inbound("取消登记"), state, None)
    assert _texts(outcome) == [replies.REGISTER_CANCELLED]
    assert outcome.state["awaiting"] is None


def test_confirm_stage_still_takes_plain_text():
    """confirm 阶段机器人自己说了「回复「同意」保存」⇒ 不加 @ 也要接住。"""
    state = {"awaiting": "register", "register": {"stage": "confirm", "expires_at": None}}
    assert _texts(route(_inbound("不同意"), state, None)) == [replies.REGISTER_CANCELLED]


# ---------- 带 @ 的指令不被登记窗口吞掉（必修 6）----------


def _window(stage="collect", initiator="ou_user", expires_at=None):
    return {
        "awaiting": "register",
        "register": {
            "stage": stage,
            "initiator_open_id": initiator,
            "leader": None,
            "members": [],
            "expires_at": expires_at,
        },
    }


_AT = (Mention(key="@_user_1", open_id="ou_bot", name="喵喵喵"),)
_FORM = "登记\n组长：@_user_2\n组员：@_user_3 @_user_4"
_FORM_MENTIONS = (
    Mention(key="@_user_2", open_id="ou_a", name="甲"),
    Mention(key="@_user_3", open_id="ou_b", name="乙"),
    Mention(key="@_user_4", open_id="ou_c", name="丙"),
)


def test_initiator_command_with_mention_is_not_parsed_as_a_form():
    """真机复现：窗口里发起人发「@机器人 方向」被回成「表单里「组长」要正好 1 个人」。"""
    outcome = route(_inbound("@_user_1 方向", mentions=_AT), _window(), None)
    assert _texts(outcome) == [replies.NEEDS_RUBRIC]
    assert outcome.state is None                      # 窗口不动


def test_stranger_command_with_mention_is_not_swallowed():
    """真机复现：窗口里旁人发「@机器人 方向」一个字都不回（最恶劣）。"""
    inbound = _inbound("@_user_1 方向", mentions=_AT, sender_open_id="ou_stranger")
    outcome = route(inbound, _window(), None)
    assert _texts(outcome) == [replies.NEEDS_RUBRIC]
    assert outcome.state is None


def test_initiator_command_without_mention_still_passes_through():
    """对照：同一句不带 @ 一直是正常的。"""
    assert _texts(route(_inbound("方向"), _window(), None)) == [replies.NEEDS_RUBRIC]


def test_stranger_form_is_silent_and_does_not_advance():
    """§7.7：旁人照表单填一份发出来 —— 不推进、不作废、不回话。"""
    inbound = _inbound(_FORM, mentions=_FORM_MENTIONS, sender_open_id="ou_stranger")
    assert route(inbound, _window(), None) == Outcome()


def test_initiator_form_still_works():
    """防回归：真填表必须照旧推进到 confirm。"""
    outcome = route(_inbound(_FORM, mentions=_FORM_MENTIONS), _window(), None)
    assert outcome.state["register"]["stage"] == "confirm"


def test_register_command_inside_the_window_shows_the_form_again():
    """防回归：表单第一行就是「登记」，不能被前缀抓错、也不能不认。"""
    inbound = _inbound("@_user_1 登记", mentions=_AT)
    assert _texts(route(inbound, _window(), None)) == [replies.REGISTER_FORM]


def test_expired_window_is_still_cleared_by_a_stranger_with_a_mention():
    """上一轮复核的结论不许回退：过期窗口谁说话都由 register_step 清掉。

    必修 6 把 confirm 阶段旁人的消息判成 silent，若不特判过期，「发起人不再开口」
    的过期窗口就再也没人能清了（classify 的 now 参数就是为这条存在的）。
    """
    window = _window(stage="confirm", expires_at="2026-09-12T13:35:00")
    inbound = _inbound("@_user_1 方向", mentions=_AT, sender_open_id="ou_stranger")

    outcome = route(inbound, window, None, now=datetime(2026, 9, 12, 14, 0, 0))

    assert _texts(outcome) == [replies.REGISTER_EXPIRED]
    assert outcome.state["awaiting"] is None


# ---------- 边界 ----------


def test_empty_and_mention_only_text_are_silent():
    assert route(_inbound(""), {}, None) == Outcome()
    assert route(_inbound("   "), {}, None) == Outcome()
    assert route(_inbound("@_user_1"), {}, None) == Outcome()


def test_prefix_tolerates_surrounding_spaces():
    state = {"pending_file": {"file_key": "fk_1"}}
    assert _texts(route(_inbound("  作业书  "), state, None)) == [replies.PARSING]


# ---------- 后台重活判定（必修 4：与「回什么话」同源）----------


def test_pipeline_is_decided_in_one_place():
    """回执才配起重活；兜底、缺料、无效输入一律不起。"""
    window = {"awaiting": "register", "register": {"stage": "collect", "expires_at": None}}
    cases = [
        ({}, _inbound("作业书"), False),                        # 没有缓存文件
        ({"pending_file": {"file_key": "k"}}, _inbound("作业书"), True),
        ({}, _inbound("拆解"), False),                          # 没有评分点
        ({}, _inbound("今天天气不错"), False),
        (window, _inbound("方向"), False),                      # 登记窗口里也不起
        ({}, _inbound("", message_type="file", file_key="k"), False),
        ({}, _inbound("拆解", sender_type="app"), False),
    ]
    for state, inbound, expected in cases:
        assert bool(route(inbound, state, None).pipeline) is expected, inbound.text


def test_decompose_ack_carries_the_pipeline():
    assert route(_inbound("拆解"), {}, None, has_rubric=True).pipeline == "decompose"


def test_pipeline_never_fires_without_the_matching_ack():
    """必修 4 的核心不变式：起重活 ⇔ 回的就是那句回执。"""
    window = {"awaiting": "register", "register": {"stage": "collect", "expires_at": None}}
    for state, inbound, kwargs in (
        (window, _inbound("拆解"), {"has_rubric": False}),
        (window, _inbound("作业书"), {}),
        (window, _inbound("完成 T3"), {}),
    ):
        outcome = route(inbound, state, None, **kwargs)
        assert outcome.pipeline == ""
        assert _texts(outcome) != [replies.PARSING]

# ---------- 重登记限组长（F1）----------


def test_rebegin_by_a_non_leader_is_refused():
    """F1：已有花名册时，非组长重登记不能夺权 —— 原花名册不动、状态不改。"""
    _, roster = _m4_fixtures()                      # 组长 = ou_zhang
    outcome = route(_inbound("登记", sender_open_id="ou_li"), {}, roster)
    assert _texts(outcome) == [replies.REGISTER_LEADER_ONLY]
    assert outcome.state is None


def test_rebegin_by_the_leader_still_opens_the_form():
    """防回归：组长重登记照常进 collect。"""
    _, roster = _m4_fixtures()
    outcome = route(_inbound("登记", sender_open_id="ou_zhang"), {}, roster)
    assert outcome.state["register"]["stage"] == "collect"


# ---------- 过期志愿窗口不吃掉当前指令（F3）----------


def _expired_preference_state():
    return {
        "awaiting": "preference",
        "preference": {
            "opened_at": (NOW - timedelta(hours=6)).isoformat(timespec="seconds"),
            "chat_id": "c1",
        },
        "group_chat_id": "c1",
    }


def test_expired_window_still_records_a_completion_mark():
    """F3：窗口过期先结算，但这句「完成 T1」照常落盘 —— 不能被总表吞掉。"""
    cards, roster = _m4_fixtures()
    outcome = route(
        _inbound("完成 T1", chat_type="p2p", sender_open_id="ou_li"),
        _expired_preference_state(),
        roster,
        cards=cards,
        assignments=[AssignmentRecord("T1", "ou_li", "volunteer_1")],
        now=NOW,
    )
    assert outcome.save_assignments                              # 总表落了盘
    assert "分配总表" in _texts(outcome)[0]                     # 总表发在前
    assert outcome.save_complete == {
        "task_id": "T1",
        "completed_at": NOW.isoformat(timespec="seconds"),
    }
    assert outcome.state["awaiting"] is None                     # 窗口已清


def test_expired_window_still_runs_a_decompose_command():
    """F3 同款：过期窗口 + 「拆解」→ 既出总表、又照常起重活。"""
    cards, roster = _m4_fixtures()
    outcome = route(
        _inbound("拆解"),
        _expired_preference_state(),
        roster,
        cards=cards,
        has_rubric=True,
        now=NOW,
    )
    assert outcome.save_assignments
    assert outcome.pipeline == "decompose"


# ---------- 匿名代言只认花名册成员（F5）----------


def test_stranger_proposal_is_refused_and_not_recorded():
    """F5：陌生人不能借机器人匿名往群里灌话。"""
    _, roster = _m4_fixtures()
    outcome = route(
        _inbound("我想提议：加一个图表", sender_open_id="ou_x"),
        {"group_chat_id": "c1"},
        roster,
    )
    assert _texts(outcome) == [replies.PROPOSAL_NOT_MEMBER]
    assert outcome.save_proposal is None
    assert not any("有组员提议" in text for text in _texts(outcome))


def test_member_proposal_is_still_relayed():
    """回归：花名册成员照常匿名转达 + 留痕。"""
    _, roster = _m4_fixtures()
    outcome = route(
        _inbound("我想提议：加一个图表", sender_open_id="ou_li"),
        {"group_chat_id": "c1"},
        roster,
    )
    assert "有组员提议：加一个图表" in _texts(outcome)
    assert outcome.save_proposal["user_id"] == "ou_li"
