"""@ 门要用的**机器人自身标识**（D-69 自审修正）+ app 层接线。

关键点：标识取不到时**不能永久缓存失败** —— 否则开机第一秒网络抖一下，
@ 门就宽松放行到进程结束，"闲聊数字被计票"这个病又回来了。
"""

from datetime import datetime

from src.gateway.app import Gateway
from src.gateway.events import Inbound
from src.storage import JsonStore

GROUP = "oc_g"


class Recording:
    """既能当 sender（记录发出的消息）又能当 downloader 的假客户端。"""

    def __init__(self, info=None, fail=False):
        self.sent = []
        self.bot_info_calls = 0
        self._info = {"open_id": "ou_bot_self", "app_name": "喵喵喵"} if info is None else info
        self._fail = fail

    def send(self, message):
        self.sent.append(message)
        return True

    def bot_info(self):
        self.bot_info_calls += 1
        if self._fail:
            raise RuntimeError("boom")
        return self._info


class NoBotInfo(Recording):
    """像单测的老夹具那样：没有 bot_info 能力。"""

    bot_info = None


def _gateway(tmp_path, sender):
    store = JsonStore(tmp_path)
    store.ensure_dirs()
    return Gateway(config=None, store=store, sender=sender, downloader=sender)


def _group_inbound(text="", mentions=()):
    return Inbound(
        chat_id=GROUP,
        chat_type="group",
        message_type="text",
        text=text,
        mentions=tuple(mentions),
        sender_type="user",
        sender_open_id="ou_zhang",
        message_id="m1",
    )


# ---------- 标识的取用与缓存 ----------


def test_identity_is_cached_after_success(tmp_path):
    sender = Recording()
    gateway = _gateway(tmp_path, sender)
    assert gateway._bot_identity() == ("ou_bot_self", "喵喵喵")
    assert gateway._bot_identity() == ("ou_bot_self", "喵喵喵")
    assert sender.bot_info_calls == 1              # 只取一次


def test_identity_failure_does_not_cache_forever(tmp_path):
    sender = Recording(fail=True)
    gateway = _gateway(tmp_path, sender)
    assert gateway._bot_identity() == ("", "")     # 这一轮宽松放行
    assert sender.bot_info_calls == 1

    # 把"上次尝试时间"往前拨过节流窗口 → 应该再试一次（而不是永远放弃）
    gateway._bot_identity_last_try -= gateway._BOT_IDENTITY_RETRY_SECONDS + 1
    sender._fail = False
    assert gateway._bot_identity() == ("ou_bot_self", "喵喵喵")
    assert sender.bot_info_calls == 2


def test_identity_failure_within_the_floor_is_not_retried(tmp_path):
    sender = Recording(fail=True)
    gateway = _gateway(tmp_path, sender)
    gateway._bot_identity()
    gateway._bot_identity()
    gateway._bot_identity()
    assert sender.bot_info_calls == 1              # 节流窗口内不反复打接口


def test_sender_without_bot_info_is_not_retried(tmp_path):
    sender = NoBotInfo()
    gateway = _gateway(tmp_path, sender)
    assert gateway._bot_identity() == ("", "")
    assert gateway._bot_identity_ready is True     # 结构性缺失：不用反复试


def test_empty_answer_is_cached(tmp_path):
    """接口答了但没给标识（异常情况）→ 也别每条消息都问一次。"""
    sender = Recording(info={})
    gateway = _gateway(tmp_path, sender)
    assert gateway._bot_identity() == ("", "")
    assert gateway._bot_identity() == ("", "")
    assert sender.bot_info_calls == 1


def test_warm_bot_identity_never_raises(tmp_path):
    gateway = _gateway(tmp_path, Recording(fail=True))
    gateway.warm_bot_identity()                    # 预热失败不许拦住启动
    assert gateway._bot_identity() == ("", "")


def test_warm_bot_identity_resolves_before_the_first_message(tmp_path):
    sender = Recording()
    gateway = _gateway(tmp_path, sender)
    gateway.warm_bot_identity()
    assert gateway._bot_identity_ready is True
    assert sender.bot_info_calls == 1


# ---------- app 层接线（端到端走一遍 handle）----------


def test_group_text_without_mention_is_dropped_end_to_end(tmp_path):
    sender = Recording()
    gateway = _gateway(tmp_path, sender)
    gateway.handle(_group_inbound("拆解"))
    assert sender.sent == []                       # 不 @ 就一个字都不发


def test_group_text_with_mention_reaches_the_router(tmp_path):
    from src.gateway.events import Mention

    sender = Recording()
    gateway = _gateway(tmp_path, sender)
    gateway.handle(
        _group_inbound(
            "@_user_1 拆解",
            mentions=(Mention(key="@_user_1", open_id="ou_bot_self", name="喵喵喵"),),
        )
    )
    assert len(sender.sent) == 1                   # 放行 → 回"还没有评分点…"
    assert "评分" in sender.sent[0].text
