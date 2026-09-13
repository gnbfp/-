"""M4 分配算法单测（S6 / S7 / S8、§2.4 / §2.5）。纯函数：不碰飞书、不碰网络、零 LLM。"""

from src.gateway.allocation import allocate, render_board, render_task_list
from src.models import Member, Preference, Roster, TaskCard

PEOPLE = (("ou_a", "张三"), ("ou_b", "李四"), ("ou_c", "王五"))


def _card(task_id, hours=1.0, module=None):
    return TaskCard(
        task_id=task_id,
        module_name=module or f"模块{task_id}",
        rubric_refs=["R1"],
        effort_hours=hours,
        deliverable="交付物",
        acceptance="验收标准",
    )


def _roster(count=3):
    members = [Member(open_id=open_id, name=name) for open_id, name in PEOPLE[:count]]
    return Roster(
        leader=members[0].open_id,
        members=members,
        registered_at="2026-09-13T09:00:00",
        confirmed_by=members[0].open_id,
    )


def _want(user_id, task_ids, at):
    return Preference(user_id=user_id, ranked_task_ids=list(task_ids), submitted_at=at)


def _pairs(result):
    return [(r.task_id, r.assignee, r.source) for r in result]


# ---------- S6 / S7 / S8 ----------


def test_s6_no_conflict_everyone_gets_their_first_choice():
    cards = [_card("T1"), _card("T2")]
    result = allocate(
        cards,
        _roster(2),
        [_want("ou_b", ["T1"], "2026-09-13T10:00:00"), _want("ou_a", ["T2"], "2026-09-13T10:01:00")],
    )
    assert _pairs(result) == [("T1", "ou_b", "volunteer_1"), ("T2", "ou_a", "volunteer_1")]


def test_s7_first_come_first_served_and_the_late_one_falls_to_his_second_choice():
    cards = [_card("T1"), _card("T2")]
    result = allocate(
        cards,
        _roster(2),
        [
            _want("ou_b", ["T1", "T2"], "2026-09-13T10:05:00"),
            _want("ou_a", ["T1"], "2026-09-13T10:00:00"),
        ],
    )
    assert _pairs(result) == [("T1", "ou_a", "volunteer_1"), ("T2", "ou_b", "volunteer_2")]


def test_s8_whoever_did_not_submit_still_gets_a_card_by_fallback():
    cards = [_card("T1"), _card("T2")]
    result = allocate(cards, _roster(2), [_want("ou_a", ["T1"], "2026-09-13T10:00:00")])
    assert _pairs(result) == [("T1", "ou_a", "volunteer_1"), ("T2", "ou_b", "auto")]


def test_fallback_hands_out_cards_in_effort_order_to_the_least_loaded():
    cards = [_card("T1", hours=5), _card("T2", hours=1), _card("T3", hours=2)]
    result = allocate(cards, _roster(3), [])
    # 按工时升序（1h → 2h → 5h）依次给"手上最少"的人 —— 三人各一张
    assert _pairs(result) == [
        ("T1", "ou_c", "auto"),
        ("T2", "ou_a", "auto"),
        ("T3", "ou_b", "auto"),
    ]


def test_fewer_cards_than_people_leaves_someone_without_a_task():
    """§2.4 ⑤：做不到"人人有份"时如实标「无任务」，不硬塞。"""
    cards = [_card("T1")]
    result = allocate(cards, _roster(3), [])
    assert _pairs(result) == [("T1", "ou_a", "auto")]
    assert render_board(result, cards, _roster(3)).count("无任务") == 2


def test_every_card_is_handed_out_exactly_once_in_card_order():
    cards = [_card("T3"), _card("T1"), _card("T2")]
    result = allocate(cards, _roster(3), [])
    assert [r.task_id for r in result] == ["T3", "T1", "T2"]
    assert sorted(r.task_id for r in result) == ["T1", "T2", "T3"]


def test_third_choice_is_labeled_volunteer_2_not_auto():
    """志愿里第 2 个及以后的命中都记 volunteer_2；auto 只留给"没填志愿的人"（D-53 修订）。"""
    cards = [_card("T1"), _card("T2"), _card("T3")]
    result = allocate(
        cards,
        _roster(3),
        [
            _want("ou_a", ["T1"], "2026-09-13T10:00:00"),
            _want("ou_c", ["T2"], "2026-09-13T10:01:00"),
            _want("ou_b", ["T1", "T2", "T3"], "2026-09-13T10:02:00"),
        ],
    )
    by_task = {r.task_id: r for r in result}
    assert (by_task["T3"].assignee, by_task["T3"].source) == ("ou_b", "volunteer_2")


def test_only_whoever_did_not_submit_is_labeled_auto():
    """对照组：一个字没填的人，兜底拿到的卡才是 auto。"""
    cards = [_card("T1"), _card("T2")]
    result = allocate(
        cards,
        _roster(2),
        [_want("ou_a", ["T1"], "2026-09-13T10:00:00")],
    )
    by_task = {r.task_id: r for r in result}
    assert (by_task["T1"].assignee, by_task["T1"].source) == ("ou_a", "volunteer_1")
    assert (by_task["T2"].assignee, by_task["T2"].source) == ("ou_b", "auto")


def test_preferences_from_outside_the_roster_are_ignored():
    """旧花名册的残留志愿、陌生人私聊都不能抢走花名册成员的任务（D-53）。"""
    cards = [_card("T1")]
    result = allocate(cards, _roster(2), [_want("ou_stranger", ["T1"], "2026-09-13T09:00:00")])
    assert _pairs(result) == [("T1", "ou_a", "auto")]


# ---------- 渲染 ----------


def test_task_list_numbers_match_what_the_members_type():
    text = render_task_list([_card("T1", hours=4), _card("T2", hours=1.5, module="写报告")])
    assert "1. T1 模块T1（4h）" in text
    assert "2. T2 写报告（1.5h）" in text


def test_board_lists_people_cards_and_the_source_tally():
    cards = [_card("T1"), _card("T2"), _card("T3")]
    roster = _roster(3)
    result = allocate(
        cards,
        roster,
        [
            _want("ou_a", ["T1"], "2026-09-13T10:00:00"),
            _want("ou_b", ["T1", "T2"], "2026-09-13T10:01:00"),
        ],
    )
    board = render_board(result, cards, roster)
    assert board.splitlines()[0] == "分配总表"
    assert "张三 → T1（第一志愿）" in board
    assert "李四 → T2（第二志愿）" in board
    assert "王五 → T3（兜底）" in board
    assert board.splitlines()[-1] == "第一志愿 1 人 / 第二志愿 1 人 / 兜底 1 人"


def test_board_counts_a_person_once_by_his_best_source():
    """一人既有志愿卡又有兜底卡：统计按"他最好的一档"算，人头才不重复计。"""
    cards = [_card(f"T{n}") for n in (1, 2, 3, 4, 5)]
    roster = _roster(2)
    result = allocate(
        cards,
        roster,
        [_want("ou_a", ["T1"], "2026-09-13T10:00:00")],
    )
    board = render_board(result, cards, roster)
    assert "张三 → T1（第一志愿）/ T3（兜底）" in board
    assert "李四 → T2（兜底）/ T4（兜底）" in board
    assert "T5（兜底）" in board                       # 5 张卡 2 个人：谁也躲不掉兜底
    assert board.splitlines()[-1] == "第一志愿 1 人 / 第二志愿 0 人 / 兜底 1 人"
