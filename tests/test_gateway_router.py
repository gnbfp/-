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


def test_direction_is_placeholder():
    assert _texts(route(_inbound("方向"), {}, None)) == [replies.PLACEHOLDER_DIRECTION]


def test_preference_prompt_is_placeholder():
    assert _texts(route(_inbound("你想做哪一块"), {}, None)) == [replies.PLACEHOLDER_PREFERENCE]


def test_proposal_accepts_both_colon_widths():
    for prefix in PROPOSAL_PREFIXES:
        assert _texts(route(_inbound(prefix + "加一个图表"), {}, None)) == [
            replies.PLACEHOLDER_PROPOSAL
        ]


def test_complete_matches_tn_with_spaces_and_case():
    for text in ("完成 T3", "完成T3", "完成 t7", "完成  T12"):
        assert COMPLETE_PATTERN.match(text)
        assert _texts(route(_inbound(text), {}, None)) == [replies.PLACEHOLDER_COMPLETE]


def test_register_starts_the_state_machine():
    outcome = route(_inbound("登记"), {}, None)
    assert outcome.state["awaiting"] == "register"
    assert replies.REGISTER_FORM in _texts(outcome)[0]


def test_unmatched_text_returns_command_list():
    assert _texts(route(_inbound("今天天气不错"), {}, None)) == [replies.COMMAND_LIST_TEXT]


# ---------- 状态优先 ----------


def test_awaiting_vote_wins_over_prefix_matching():
    outcome = route(_inbound("2", ), {"awaiting": "vote"}, None)
    assert _texts(outcome) == [replies.PLACEHOLDER_VOTE]


def test_awaiting_preference_wins_over_prefix_matching():
    outcome = route(_inbound("你想做哪一块"), {"awaiting": "preference"}, None)
    assert _texts(outcome) == [replies.PLACEHOLDER_PREFERENCE]


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
    assert _texts(route(_inbound("方向"), state, None)) == [replies.PLACEHOLDER_DIRECTION]
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
    assert _texts(outcome) == [replies.PLACEHOLDER_DIRECTION]
    assert outcome.state is None                      # 窗口不动


def test_stranger_command_with_mention_is_not_swallowed():
    """真机复现：窗口里旁人发「@机器人 方向」一个字都不回（最恶劣）。"""
    inbound = _inbound("@_user_1 方向", mentions=_AT, sender_open_id="ou_stranger")
    outcome = route(inbound, _window(), None)
    assert _texts(outcome) == [replies.PLACEHOLDER_DIRECTION]
    assert outcome.state is None


def test_initiator_command_without_mention_still_passes_through():
    """对照：同一句不带 @ 一直是正常的。"""
    assert _texts(route(_inbound("方向"), _window(), None)) == [replies.PLACEHOLDER_DIRECTION]


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
