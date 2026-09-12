"""M0 事件翻译与数据结构的单测（方案 §3）。"""

from types import SimpleNamespace

from src.gateway.events import Inbound, Mention, Outcome, Reply, reply, to_inbound


def _event(content='{"text": "hi"}', message_type="text", mentions=(), sender_type="user", **over):
    message = SimpleNamespace(
        chat_id=over.get("chat_id", "c1"),
        chat_type=over.get("chat_type", "group"),
        message_type=message_type,
        content=content,
        mentions=list(mentions),
        message_id=over.get("message_id", "m1"),
    )
    sender = SimpleNamespace(
        sender_type=sender_type,
        sender_id=SimpleNamespace(open_id=over.get("sender_open_id", "ou_sender")),
    )
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


def _mention(key, open_id, name=""):
    return SimpleNamespace(key=key, id=SimpleNamespace(open_id=open_id), name=name)


def test_to_inbound_maps_text_message():
    inbound = to_inbound(_event())
    assert inbound.chat_id == "c1"
    assert inbound.chat_type == "group"
    assert inbound.message_type == "text"
    assert inbound.text == "hi"
    assert inbound.sender_open_id == "ou_sender"
    assert inbound.sender_type == "user"
    assert inbound.message_id == "m1"


def test_to_inbound_keeps_mentions_with_open_id():
    inbound = to_inbound(
        _event(content='{"text": "@_user_1 拆解"}', mentions=[_mention("@_user_1", "ou_bot", "机器人")])
    )
    assert inbound.text == "@_user_1 拆解"
    assert inbound.mentions == (Mention(key="@_user_1", open_id="ou_bot", name="机器人"),)


def test_to_inbound_file_message_reads_file_key():
    inbound = to_inbound(
        _event(content='{"file_key": "fk_1", "file_name": "作业书.pdf"}', message_type="file")
    )
    assert inbound.file_key == "fk_1"
    assert inbound.file_name == "作业书.pdf"


def test_to_inbound_image_message_reads_image_key():
    inbound = to_inbound(_event(content='{"image_key": "ik_1"}', message_type="image"))
    assert inbound.file_key == "ik_1"


def test_to_inbound_tolerates_broken_content_and_empty_event():
    assert to_inbound(_event(content="{不是 JSON")).text == ""
    assert to_inbound(SimpleNamespace()).chat_id == ""


def test_reply_targets_the_incoming_chat():
    inbound = Inbound(chat_id="c9")
    assert reply(inbound, "hi") == Reply(chat_id="c9", text="hi")


def test_outcome_defaults_are_empty_and_immutable():
    outcome = Outcome()
    assert outcome.replies == ()
    assert outcome.state is None
    assert outcome.download_file_key == ""
    assert outcome.save_roster is None
