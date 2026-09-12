"""M0 路由单测（§7.1 / D-33 / D-42，方案 §4 / §9）。不碰飞书、不碰网络。"""

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


def test_image_message_is_dropped_not_cached():
    """必修 3：图片静默丢弃，不回话也不进缓存。"""
    assert route(_inbound("", message_type="image", file_key="ik_1"), {}, None) == Outcome()


def test_image_does_not_evict_a_cached_file():
    """必修 3 的现场：先发 PDF 再接一张图，缓存里必须还是那个 PDF。"""
    state = route(
        _inbound("", message_type="file", file_key="fk_1", file_name="作业书.pdf"), {}, None
    ).state
    assert route(_inbound("", message_type="image", file_key="ik_1"), state, None) == Outcome()
    assert state["pending_file"]["file_key"] == "fk_1"


def test_other_message_types_are_ignored():
    assert route(_inbound("", message_type="sticker"), {}, None) == Outcome()


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
