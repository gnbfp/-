import json

import pytest

from src import storage
from src.models import Member, Roster, RubricPoint, SchemaError, TaskCard
from src.storage import JsonStore


@pytest.fixture()
def store(tmp_path):
    return JsonStore(tmp_path)


def _card(**over):
    data = dict(
        task_id="T1",
        module_name="实现登录模块",
        rubric_refs=["R1"],
        effort_hours=4.0,
        deliverable="一个源文件",
        acceptance="从 R1 原文改写",
    )
    data.update(over)
    return TaskCard(**data)


def test_roundtrip_cards(store):
    store.save_cards([_card()])
    cards = store.load_cards()
    assert [c.task_id for c in cards] == ["T1"]
    assert cards[0].rubric_refs == ["R1"]


def test_missing_files_are_empty_not_error(store):
    assert store.load_cards() == []
    assert store.load_rubric() == []
    assert store.load_preferences() == []
    assert store.load_assignments() == []
    assert store.load_assignment() is None
    assert store.load_members() is None
    assert store.load_proposals() == []
    assert store.load_state() == {}


def test_write_is_atomic_and_leaves_no_tmp(store):
    store.save_cards([_card()])
    assert list(store.root.glob("*.tmp")) == []
    assert (store.root / storage.CARDS).exists()


def test_validate_runs_on_write_and_leaves_file_intact(store):
    store.save_cards([_card()])
    bad = _card(task_id="T2", rubric_refs=[{"id": "R1", "quote": "原文"}])
    with pytest.raises(SchemaError):
        store.save_cards([bad])
    assert [c.task_id for c in store.load_cards()] == ["T1"]


def test_mutate_many_is_typed_and_persists(store):
    store.save_rubric([RubricPoint(id="R1", quote="原文一段", observable="可核对")])

    def add(points):
        points.append(RubricPoint(id="R2", quote="原文两段", observable="可核对"))
        return points

    result = store.mutate_many(storage.RUBRIC, RubricPoint, add)
    assert [p.id for p in result] == ["R1", "R2"]
    assert [p.id for p in store.load_rubric()] == ["R1", "R2"]


def test_mutate_many_does_not_write_when_fn_raises(store):
    store.save_rubric([RubricPoint(id="R1", quote="原文", observable="可核对")])

    def boom(points):
        raise RuntimeError("生成失败")

    with pytest.raises(RuntimeError):
        store.mutate_many(storage.RUBRIC, RubricPoint, boom)
    assert [p.id for p in store.load_rubric()] == ["R1"]


def test_mutate_many_does_not_write_when_validation_fails(store):
    store.save_cards([_card()])

    def corrupt(cards):
        cards[0].rubric_refs = [{"id": "R1"}]        # D-03 违规
        return cards

    with pytest.raises(SchemaError):
        store.mutate_many(storage.CARDS, TaskCard, corrupt)
    assert store.load_cards()[0].rubric_refs == ["R1"]


def test_mutate_raw_for_state(store):
    def set_awaiting(state):
        state["awaiting"] = "vote"
        return state

    store.mutate_raw(storage.STATE, set_awaiting, default={})
    assert store.load_state() == {"awaiting": "vote"}


def test_corrupt_json_raises_instead_of_guessing(store):
    (store.root / storage.CARDS).write_text("{不是合法 JSON", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        store.load_cards()


def test_members_roundtrip(store):
    roster = Roster(
        leader="u_zhang",
        registered_at="ts",
        confirmed_by="u_zhang",
        members=[
            Member(open_id="u_zhang", name="张三"),
            Member(open_id="u_li", name="李四"),
        ],
    )
    store.save_members(roster)
    back = store.load_members()
    assert back.leader == "u_zhang"
    assert [m.name for m in back.members] == ["张三", "李四"]


def test_lock_is_process_wide_not_per_instance():
    a = JsonStore("x")
    b = JsonStore("y")
    assert a._lock is b._lock
