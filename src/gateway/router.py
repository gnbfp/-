"""M0 路由 + M4/M5 的指令入口 —— 纯函数。**铁律：本文件不许 import lark_oapi**（方案 §1）。

依据：`requirements.md` §7.1（D-33「先剥 @段 → 再看状态 → 最后认前缀」+ 7 条前缀）、
D-42（文字与附件必然是两条消息）、**M4 志愿分配 / M5 匿名代言**（D-52~D-55）、
M0 网关方案 §4 / §5 / §6 / §7。

进出都是纯数据（``Inbound`` / ``dict`` / ``Outcome``）：不联网、不发消息、不读文件，
所以这一整套规则可以在没有飞书、没有网络的情况下全量单测。

三条指令的状态机在各自的文件里，本文件只做"接管 or 放行"：
  * 「方向」→ ``vote.command()``（群里起 M2 生成候选并开投票窗口；组长「封盘」拍板）；
  * 「你想做哪一块」→ ``preference.command()``（群里开窗口 / 组长重发=封盘）；
  * 「我想提议：…」→ ``_proposal()``（匿名转达 + 留痕）。

两条文档没写死、按方案 §4 的工程默认（可推翻）落地：
  * 「我想提议」全角 ``：`` 与半角 ``:`` 都认（中文输入法容易出半角）；
  * 「完成 T3」用 ``^完成\\s*[Tt](\\d+)\\s*$``：容忍空格与大小写，但**整句必须就是这条指令**
    （要捕获编号给 M6 用；"完成 T3 谢谢" 之类落到指令列表，不猜）。
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Sequence

from src.gateway import complete, preference, register, replies, vote
from src.gateway.events import Inbound, Mention, Outcome, Reply, reply

__all__ = [
    "PROPOSAL_PREFIXES",
    "COMPLETE_PATTERN",
    "PENDING_FILE_TTL",
    "NO_RUBRIC_TTL",
    "strip_mentions",
    "route",
    "remember_file",
    "known_group_ids",
    "group_allowed",
    "no_rubric_block",
    "no_rubric_expired",
    "confirm_no_rubric",
    "RESET_WORD",
    "RESET_CONFIRM_WORD",
    "reset_block",
    "reset_expired",
    "request_reset",
    "confirm_reset",
]

# 7 条前缀里两条带变体：提议的冒号全半角、完成的 Tn 容忍空格与大小写。
PROPOSAL_PREFIXES = ("我想提议：", "我想提议:")
COMPLETE_PATTERN = re.compile(r"^完成\s*[Tt](\d+)\s*$")
# 无参「完成」= "我该标哪张？"（D-70）：菜单里只放一格，卡片编号由机器人当场列出来。
COMPLETE_BARE = re.compile(r"^完成\s*$")
# 「帮助」/「help」/「使用说明」= 要一份用法清单（D-71）。**在任何状态机之前**处理：
# 看一眼就回答，不改 awaiting、不碰票数/志愿/花名册 —— 纯增量，不影响别的流程。
HELP_WORDS = ("帮助", "help", "使用说明")
# 「重置」= 清掉**这一场**的产物、回到"刚登记完"的状态（第 11 条指令，2026-09-16）。
# 两段式：先出"要清什么"的清单，组长回**确认词**才真删（防误删）；窗口 5 分钟、回别的作废。
RESET_WORD = "重置"
RESET_CONFIRM_WORD = "确定重置"

_MENTION_PLACEHOLDER = re.compile(r"@_user_\d+")

# 缓存文件的有效期（D-46，工程默认，可推翻）。``pending_file`` 是**落盘**的、关掉重启仍在，
# 而唯一的清除时机是「作业书」跑完 —— 所以"发过文件、没接着说「作业书」"的残留会一直留着，
# 跨场次录制时会静默复用一份旧作业书。30 分钟对一场演示足够，只拦跨场次串味。
PENDING_FILE_TTL = timedelta(minutes=30)
# 无评分点模式的确认窗口（口径 A，2026-09-16）。与登记窗口同款 5 分钟：组长看到
# "我没找到评分标准"那句话之后，回来补一句「按正文拆」就够了；过期就当没有。
NO_RUBRIC_TTL = timedelta(minutes=5)
# 「重置」的确认窗口（2026-09-16）：与登记/无评分点同款 5 分钟 —— 组长看清楚清单、
# 回一句「确定重置」就够了；过期就当没这回事，要重来就再发一次「重置」。
RESET_TTL = timedelta(minutes=5)


# ---------- 多群隔离（2026-09-16 补审 §1.10）----------


def known_group_ids(state: dict) -> list[str]:
    """本场作业的群（正常只有一个）。

    优先 ``known_groups`` —— 登记确认时由 ``register._confirm()`` 写入；
    老 state（这个字段还不存在的场子）退回 ``group_chat_id``：**让升级前就存在的场子
    也立刻受保护，不需要人工改数据**。两个都没有 = 还没认过群。
    """
    raw = [g for g in ((state or {}).get("known_groups") or []) if g]
    if raw:
        return raw
    legacy = (state or {}).get("group_chat_id") or ""
    return [legacy] if legacy else []


def group_allowed(state: dict, chat_id: str) -> bool:
    """这个群能不能算"本场的群"。

    三条放行，其余一律不算：
      ① **还没认过群**（旧 state / 刚起盘）—— 保持原行为，不然第一次「登记」都做不了；
      ② ``chat_id`` ∈ 本场的群（登记过的）；
      ③ 正**在这个群里**登记（登记窗口自己记的 ``chat_id``）—— 换群/首次登记都靠这条放行。

    ⚠️ 为什么必须有这道闸（实测，§1.10）：机器人被拉进第二个群时，
    「报告」的产物、M5 的匿名转达、M6 的 @催办都按 ``state.group_chat_id`` 发 ——
    而那个值**任何群说一句话就会被改写**，于是会出现"组员甲的匿名提议发到了别的群"、
    "组长在别的群 @我 起「重置」把本场删了"、"非成员在别的群 @我 起「方向」烧 30 秒 LLM"。
    """
    known = known_group_ids(state)
    if not known:
        return True                              # ① 还没认过群：保持原行为
    if chat_id and chat_id in known:
        return True                              # ② 登记过的群
    block = (state or {}).get("register") or {}
    return bool(chat_id) and chat_id == (block.get("chat_id") or "")   # ③ 正在这个群里登记


def strip_mentions(text: str, mentions: Sequence[Mention] = ()) -> str:
    """剥掉 @段，只留正文。

    先按 mentions 里的占位符精确替换（``@_user_1``），再用正则兜底 —— 消息里的
    @ 段必须剥掉才能做前缀匹配，但 **mentions 本身不能丢**：「登记」靠它的 open_id（D-34）。
    """
    for mention in mentions:
        if mention.key:
            text = text.replace(mention.key, "")
    return _MENTION_PLACEHOLDER.sub("", text or "")


def _is_help(text: str) -> bool:
    """这条消息是不是在要「用法清单」（D-71）。

    ``text`` 必须是**剥掉 @段之后的正文**（调用方已经算好）—— 群里 ``@我 帮助`` 剥完就是「帮助」。
    用 ``startswith``：说「帮助我一下」「使用说明书」也算想求助。
    """
    plain = (text or "").strip().lower()
    return any(plain.startswith(word) for word in HELP_WORDS)


def _is_register_reply(state: dict, text: str) -> bool:
    """登记窗口内，「同意」/「取消」这类**回话词**豁免 @ 门（D-69 ②）。

    机器人自己在提示里说「回复「同意」保存」 —— 用户照做却被静默丢弃不合理，
    而这两个词动作明确、闲聊里打出来也不至于误伤（登记窗口本来就有"回复别的就作废"）。
    只在 ``awaiting == "register"`` 时生效，不影响其它窗口（投票裸数字**不豁免**，
    那正是要治的误触发场景）。
    """
    return (state or {}).get("awaiting") == "register" and register.is_reply_word(text)


def _is_reset_reply(state: dict, text: str, now: datetime | None = None) -> bool:
    """「重置」窗口内，确认词豁免 @ 门。

    这是**同一个坑**：机器人提示"确认请回「确定重置」"，用户照做却被 @ 门静默吃掉 ——
    登记窗的「同意」在 D-69 之后就真机踩过一次（PR #20 修的），这里一开始就豁免。
    只在**窗口开着且没过期**时生效：没有窗口时，群里不 @ 我的「确定重置」照旧丢弃。

    ``now`` **必须由调用方传进来**（2026-09-16 修）：原来这里偷看 ``datetime.now()``，
    于是 ``test_reset.py`` 里那条把时间冻在 20:00 的用例，到了墙上时间 20:05 就**永久变红**
    （实测：同一个分支 19:36 全绿、20:06 开始必挂）。``route()`` 手上本来就有 ``now``，
    传下来即可 —— 这条链上不许再出现隐式的墙上时间。
    """
    block = (state or {}).get("reset") or {}
    if not block or reset_expired(block, now):
        return False
    return (text or "").strip() == RESET_CONFIRM_WORD


def _mentioned_bot(inbound: Inbound, bot_open_id: str = "", bot_name: str = "") -> bool:
    """这条消息 @ 的是不是**机器人自己**（D-69）。

    判据优先级：
      ① mentions 里的 ``open_id`` 命中机器人自身 open_id（最准，app 层启动时取一次）；
      ② 退一步比 ``name``（同名成员的概率可忽略）；
      ③ **两个标识都拿不到 → 放行**：宁可漏拦，不可让机器人突然变哑巴
         （启动时那一次 ``bot/v3/info`` 失败就走这条，并在 app 层打一行告警）。
    """
    mentions = tuple(getattr(inbound, "mentions", ()) or ())
    if not bot_open_id and not bot_name:
        return True
    for mention in mentions:
        if bot_open_id and getattr(mention, "open_id", "") == bot_open_id:
            return True
        if bot_name and getattr(mention, "name", "") == bot_name:
            return True
    return False


def route(
    inbound: Inbound,
    state: dict,
    roster=None,
    *,
    has_rubric: bool = False,
    body_mode: bool = False,
    cards: Sequence = (),
    preferences: Sequence = (),
    assignments: Sequence = (),
    now: datetime | None = None,
    source_title: str = "",
    bot_open_id: str = "",
    bot_name: str = "",
) -> Outcome:
    """一条消息 → 一个 Outcome。顺序严格按 D-33：剥 @段 → 状态 → 前缀 → 兜底。
    ``roster`` / ``cards`` / ``preferences`` 由 app 层从 ``data/`` 读出来传进来 ——
    router 自己不读文件，但仍然能对 M4 给出正确回复：开窗口、收志愿、结算、发总表。
    传进的是**数据**不是**路径**，所以这一整套规则照样能离线全量单测。
    ``has_rubric`` 由 app 层从 ``data/rubric.json`` 读出来传进来 —— router 自己不读文件，
    但仍然能对「拆解」给出正确回复（没有评分点 vs 重跑）。

    ``bot_open_id`` / ``bot_name`` 是**机器人自己**的标识（app 层启动时取一次，D-69）：
    群消息要用它判"这条 @ 的是不是我"。两个都为空 = 取不到 ⇒ **宽松放行**。

    **重活也在这里判**（``Outcome.pipeline``）：回什么话与起不起 M1/M3 必须同源，
    分两处判就会出现"回了「表单没看懂」却照样烧一次 LLM"（必修 4）。
    """
    original = state
    # 0. 机器人自己的消息：权限里开了 include_bot，这条不丢就会自己回自己
    if inbound.sender_type == "app":
        return Outcome()

    # 1. 文件：只缓存，不干活（D-42：文字和附件必然是两条消息）
    #    **免 @ 门**：飞书不允许"文字 + 附件"同一条，所以发文件时没法同时 @（D-69）。
    #    多群隔离（§1.10）：别的群发的文件**连缓存都不进** —— pending_file 只有一个槽位，
    #    让别群的文件挤进来就是"本群刚发的作业书被顶掉"的那种串味。
    if inbound.message_type == "file":
        if inbound.chat_type == "group" and not group_allowed(state, inbound.chat_id):
            return Outcome(replies=(reply(inbound, replies.GROUP_NOT_THIS_SESSION),))
        return remember_file(inbound, state, now)
    #    图片：读不了就直说（用户 2026-09-12 拍板，不再静默），但仍然**不入缓存** ——
    #    必修 3 的底线是"一张图不能把刚发来的作业书 PDF 挤掉"。其余类型保持静默。
    #    同样是**免 @ 门**：图片也发不出 @，且这句是"纠正格式"，不是闲聊兜底（D-69）。
    if inbound.message_type == "image":
        return Outcome(replies=(reply(inbound, replies.IMAGE_REJECTED),))
    if inbound.message_type != "text":
        return Outcome()

    text = strip_mentions(inbound.text, inbound.mentions).strip()

    # 1.5 【群里的文字消息必须 @机器人】（§8.1 备选 1 → D-69）
    #     动机：群里不 @ 就响应的话，组员讨论时打的指令词/数字会被误触发。
    #     三类例外：① **登记窗口内**的登记表单（必须 @ 组员，形状见 register.looks_like_form）
    #              ② 登记窗口的回话词「同意」/「取消」（机器人自己在等他回这句）
    #              ③ 私聊（下面这条 if 只拦 group）
    #     机器人的消息在第 0 步已经滤掉。
    #
    #     ⚠️ 表单豁免**必须同时满足"正处于登记窗口"**（F1，2026-09-16 加固）：光看形状不够。
    #     ``looks_like_form`` 只要求"消息里有任意 @ **且** 任意一行以 `组长：`/`组员：` 开头"，
    #     与 ``awaiting``、与"这条到底是不是那张表单"全都无关。只按形状豁免的话，
    #     群里一句 `拆解\n组员：@甲 @乙 你们看看` 就能绕过 @ 门起 M3
    #     （实测：起了 ``decompose`` 流水线 = 30 秒 LLM + 改状态），
    #     D-69 想治的"讨论时误触发"原样回来了。
    in_register_window = (state or {}).get("awaiting") == "register"
    if inbound.chat_type == "group" and not _mentioned_bot(
        inbound, bot_open_id, bot_name
    ):
        form_exempt = in_register_window and register.looks_like_form(inbound)
        if not form_exempt and not _is_register_reply(state, text) and not _is_reset_reply(
            state, text, now
        ):
            return Outcome()               # 没 @ 我 → 静默丢弃，一个字都不回

    if not text:
        return Outcome()                       # 纯 @ 段 / 空文本：静默，不刷屏

    # 1.6 【帮助】在任何状态机**之前**（D-71）：只看一眼就回答用法，
    #     不读也不写 state、不起 pipeline、不落任何盘 —— 所以"不影响别的进程"。
    #     位置刻意放在 awaiting 之前：登记确认阶段回「帮助」不该被当成"回复别的就作废"。
    if _is_help(text):
        return Outcome(replies=(reply(inbound, replies.HELP_TEXT),))

    # 1.7 【多群隔离】（2026-09-16 补审 §1.10）：**不是本场的群就什么都不做**。
    #     位置：@ 门之后（没 @ 我的闲聊照旧静默）、「帮助」之后（问到用法还是照答，
    #     这条不读不写 state、也回不到群里去，零风险）、状态机之前 ——
    #     必须排在状态机前面：窗口（登记/投票/志愿）是**全局单值**，
    #     让别群的消息进得来，就会出现"别群的人在填本群的登记表""别群的数字被算成票"。
    #     两条放行：①「登记」是进场的唯一入口（换群/首次登记都要它）；
    #              ② 正在登记的那个群由 group_allowed() 的 ③ 放行（「同意」/表单是回话）。
    if (
        inbound.chat_type == "group"
        and not group_allowed(state, inbound.chat_id)
        and not text.startswith("登记")
    ):
        return Outcome(replies=(reply(inbound, replies.GROUP_NOT_THIS_SESSION),))

    # 2. 状态优先：裸数字/表单怎么解释，全看 state.json 的 awaiting
    awaiting = (state or {}).get("awaiting")
    if awaiting == "register":
        # 先给出口：没有逃生词，一次误触「登记」不填表就吃掉整个群的所有指令（必修 1）
        if register.is_cancel(text):
            return register.register_cancel(inbound, state, now)
        # 谁归状态机管由 register.classify() 一条规则说了算（必修 6）：
        # "step" 交给状态机、"silent" 归它但不回话、"pass" 继续往下走 7 条前缀 ——
        # 只按「带 @」接管的话，指令会被当成表单吃掉、或被静默吞掉（真机已复现）
        mode = register.classify((state or {}).get("register") or {}, inbound, inbound.text, now)
        if mode == "step":
            # 表单要吃**原文**：@ 占位符（@_user_1）是"这行 @ 了谁"的唯一线索，
            # 剥掉就再也对不上 open_id 了（D-34：id 只从 @ 结构里取）。
            # 确认阶段则相反：要比字面量「同意」，必须用**剥 @ 后的正文**（D-69 修复）——
            # 群里带 @ 时原文是 "@_user_1 同意"，拿原文比会把每次确认都当"回复别的"作废。
            return register.register_step(inbound.text, inbound, state, now, plain=text)
        if mode == "silent":
            return Outcome()
    if awaiting == "vote":
        # 方向投票窗口（M2，D-35 / D-36）：只认开窗那个群的花名册成员；
        # **不命中一律回退 7 条前缀** —— 窗口开着时「拆解」「作业书」必须照常干活，
        # 不然就是一个吃掉指令的死锁窗口（必修 1 的同款病）。
        # 超时收口（外审必修 A）：**任何**到达的消息都要把到期的窗口收口（发票数明细 +
        # 冻住），不能只等"下一条数字"；但这条消息本身若是指令，收口之后**照常执行**，
        # 不许被吞 —— 所以是"先收口、再照原路走一遍"，不是提前 return。
        closing = (
            vote.close_expired(inbound, state, roster, now) if vote.should_close(text) else None
        )
        if closing is not None:
            state = closing.state            # 收口后的状态：冻住（或已过半落定清空）
        hit = vote.accept(text, inbound, state, roster, now)
        if hit is not None:
            return _with_closing(hit, closing)
        return _with_closing(
            _merge(
                _by_prefix(
                    inbound,
                    state,
                    roster,
                    has_rubric=has_rubric,
                    body_mode=body_mode,
                    cards=cards,
                    preferences=preferences,
                    assignments=assignments,
                    now=now,
                    source_title=source_title,
                ),
                state,
                original,
            ),
            closing,
        )
    if awaiting == "preference":
        # 志愿窗口（5 小时，D-52~D-54）：过期就当场结算，没过期就试收志愿；
        # 都不是（群里发数字 / 私聊发指令）→ 照走 7 条前缀。
        block, expired = preference.read_window(state, now)
        closing = None
        if block and expired:
            # 先结算，但**不提前 return**：这条消息本身若是指令（「完成 T1」「拆解」），
            # 结算之后还要照原路走一遍（同 M2 收口，必修 A）—— 否则窗口一过期，
            # 当事人那句「完成 T1」就永远落不了盘（F3）。
            closing = preference.settle(state, cards, roster, preferences, now)
            if closing is not None and closing.state is not None:
                state = closing.state             # 结算带回的已清状态
            else:
                state = preference.clear(state)   # 结不了（没认下群）：清残留，别卡住 awaiting
        elif block:
            hit = preference.accept(text, inbound, state, cards, roster, preferences, now)
            if hit is not None:
                return hit
        return _with_closing(
            _merge(
                _by_prefix(
                    inbound,
                    state,
                    roster,
                    has_rubric=has_rubric,
                    body_mode=body_mode,
                    cards=cards,
                    preferences=preferences,
                    assignments=assignments,
                    now=now,
                    source_title=source_title,
                ),
                state,
                original,
            ),
            closing,
        )

    return _by_prefix(
        inbound,
        state,
        roster,
        has_rubric=has_rubric,
        body_mode=body_mode,
        cards=cards,
        preferences=preferences,
        assignments=assignments,
        now=now,
        source_title=source_title,
    )


def _main_chain_guard(inbound: Inbound, roster) -> Outcome | None:
    """M1（「作业书」）/ M3（「拆解」）的身份闸 —— **只有花名册成员能触发**。放行返回 ``None``。

    为什么单独给这两条设闸（F1，2026-09-16 加固）：它们起的是**主链路**，跑完会
    ``save_assignment`` / ``save_rubric`` / ``save_cards`` —— **整份覆盖**全组的评分点与任务卡。
    而 M4–M7 的入口本来就有身份校验（非成员不给假确认、`报告` 只认组长、`封盘` 只认组长），
    唯独最重的这两条以前一个校验都没有：任何能私聊到机器人的人，自己发一份 PDF 再说
    「作业书」，就能把全组的产物换掉，而核对清单只回到他的私聊，群里完全不知道
    （实测：陌生人私聊两步就起了 ``pipeline="assignment"``；群里 @ 我 发「拆解」也一样起 M3）。

    **花名册为空（还没登记）时也拒**：这一条与 M5 的「``roster`` 为空 = 谁都算数」宽松口径
    **有意不同** —— M5 最坏结果是群里多一条转达，而这里最坏结果是整份产物被换掉。
    没认人之前，先让组长在群里发一次「登记」。

    ``sender_open_id`` 为空（老事件 / 夹具）也拒：认不出来的人一律按非成员处理。
    """
    known = {member.open_id for member in (getattr(roster, "members", None) or ())}
    if inbound.chat_type == "p2p":
        # 私聊里没有"群"这层身份边界，所以**私聊必须认得出人**（没名册也拒）。
        if not known:
            return Outcome(replies=(reply(inbound, replies.MAIN_CHAIN_NEED_REGISTER),))
    elif not known:
        # 群里、且还没登记：群本身就是边界（「登记」也是在群里做的），先放行。
        return None
    if not inbound.sender_open_id or inbound.sender_open_id not in known:
        return Outcome(replies=(reply(inbound, replies.MAIN_CHAIN_NOT_MEMBER),))
    return None


def _by_prefix(
    inbound: Inbound,
    state: dict,
    roster=None,
    *,
    has_rubric: bool = False,
    body_mode: bool = False,
    cards: Sequence = (),
    preferences: Sequence = (),
    assignments: Sequence = (),
    now: datetime | None = None,
    source_title: str = "",
) -> Outcome:
    """D-33 的第 3、4 步：8 条前缀精确匹配 → 都不中就是指令列表（T01）。"""
    text = strip_mentions(inbound.text, inbound.mentions).strip()

    # M1 / M3 会**整份覆盖**全组产物 ⇒ 先过身份闸，再谈前置条件（F1，2026-09-16 加固）。
    if text.startswith("作业书") or text.startswith("拆解"):
        guard = _main_chain_guard(inbound, roster)
        if guard is not None:
            return guard

    # 无评分点模式（口径 A，2026-09-16）：第 10 条指令，只认组长 + 只认原会话 + 窗口 5 分钟。
    if text.startswith("按正文拆"):
        return confirm_no_rubric(inbound, state, roster, now)

    # 「重置」两段式（第 11 条指令，2026-09-16）：先出清单，组长回确认词才真删。
    # 「确定重置」不以「重置」开头，所以两条前缀互不干扰；确认词排在前面只是为了好读。
    if text.startswith(RESET_CONFIRM_WORD):
        return confirm_reset(inbound, state, roster, now)
    if text.startswith(RESET_WORD):
        return request_reset(inbound, state, roster, now=now)

    if text.startswith("作业书"):
        return _assignment(
            inbound, state, now, has_rubric=has_rubric, has_cards=bool(cards)
        )
    if text.startswith("拆解"):
        return Outcome(
            replies=(reply(inbound, replies.DECOMPOSING if has_rubric else replies.NEEDS_RUBRIC),),
            pipeline="decompose" if has_rubric else "",
        )
    if text.startswith("方向"):
        # M2：群里 = 起后台生成候选 + 开投票窗口；私聊 = 指出"去群里发"（§2.2）。
        # 前置缺失（没评分点 / 没花名册）都在 vote.command() 里判，**都不起 pipeline**。
        return vote.command(
            inbound, state, roster, has_rubric=has_rubric, body_mode=body_mode, now=now
        )
    if text.startswith("你想做哪一块"):
        return preference.command(
            inbound, state, cards, roster, preferences, now, source_title=source_title
        )
    if any(text.startswith(prefix) for prefix in PROPOSAL_PREFIXES):
        return _proposal(text, inbound, state, roster, now)
    match = COMPLETE_PATTERN.match(text)
    if match:
        # 编号由 router 捕获（判定只有一处），剩下的"是不是你的卡 / 标没标过"归 M6
        return complete.accept(match.group(1), inbound, assignments, cards, now)
    if COMPLETE_BARE.match(text):
        # 无参「完成」（D-70）：机器人列出**他自己**名下的卡 + 该怎么标。
        # 菜单只放一格，具体编号由机器人当场报出来 —— 卡数再多也不怕。
        return complete.list_mine(inbound, assignments, cards)
    if text.startswith("登记"):
        return register.register_begin(inbound, state, now, roster)
    if text.startswith("报告"):
        # M7 触发点 = 方案 A（D-64）：只有组长能在群里要报告
        return _report(inbound, roster, assignments)

    return Outcome(replies=(reply(inbound, replies.COMMAND_LIST_TEXT),))


def _report(inbound: Inbound, roster, assignments: Sequence) -> Outcome:
    """M7 执行报告（D-64）—— **只认组长、只认群里**。

    这里只判"能不能起"，真正生成三件套 + 甘特图是后台重活（``pipeline="report"``）；
    两者同源，不会出现"回了「只有组长能要报告」却照样跑一轮"（必修 4 的口径）。
    """
    if inbound.chat_type != "group":
        return Outcome(replies=(reply(inbound, replies.REPORT_NEED_GROUP),))
    if roster is None or not getattr(roster, "members", None):
        return Outcome(replies=(reply(inbound, replies.REPORT_NEED_ROSTER),))
    if not inbound.sender_open_id or inbound.sender_open_id != roster.leader:
        return Outcome(replies=(reply(inbound, replies.REPORT_NEED_LEADER),))
    if not assignments:
        return Outcome(replies=(reply(inbound, replies.REPORT_NEED_ASSIGNMENTS),))
    return Outcome(replies=(reply(inbound, replies.REPORT_GENERATING),), pipeline="report")


def _merge(outcome: Outcome, state: dict, original: dict) -> Outcome:
    """前缀分支的结果里补上"状态被 M4 清理过"这件事。

    过期的志愿窗口清掉 ``awaiting`` 之后，这条消息本身可能是「拆解」这种不写状态的
    指令 —— 那个 Outcome 的 ``state`` 是 ``None``。不合并的话，清掉的 ``awaiting``
    就落不了盘，残留窗口会一直挂着（下次读还是过期、还是要再判一遍）。
    """
    if state is original or outcome.state is not None:
        return outcome
    return replace(outcome, state=state)


def _with_closing(outcome: Outcome, closing: Outcome | None) -> Outcome:
    """把"超时收口"的结果并进这条消息本来该有的结果里（外审必修 A）。

    明细 / 落定**排在这条消息的回复之前**（先交代窗口到期，再回答它问的事）；
    ``state`` 以收口后的为准（冻住 / 已清空）。

    落盘请求合并的口径：这条消息自己产生的优先，收口产生的补上。
      * ``save_direction`` —— M2 落定（只有投票路径有）；
      * ``save_assignments`` —— M4 结算（只有志愿窗口路径有）；
      * ``save_complete`` —— M6 完成标记。
    """
    if closing is None:
        return outcome
    return replace(
        outcome,
        replies=(*closing.replies, *outcome.replies),
        state=outcome.state if outcome.state is not None else closing.state,
        save_direction=outcome.save_direction or closing.save_direction,
        save_assignments=outcome.save_assignments or closing.save_assignments,
        save_complete=outcome.save_complete or closing.save_complete,
    )


def _proposal(
    text: str, inbound: Inbound, state: dict, roster=None, now: datetime | None = None
) -> Outcome:
    """M5 匿名代言（§6.5 / D-55）：私聊提议 → 落盘留痕 → 群里匿名**原样**转达。

    正文只 strip 两端，不修改、不总结、不加工（B2 / §6.5）。原来带的 @ 段会被
    ``strip_mentions`` 一起剥掉 —— 与其它指令一致，也免得把 ``@_user_2`` 这种
    内部占位符泄露到群里。

    **只有花名册成员能代言**（F5）：否则任何陌生人都能用反正不透名的
    “有组员提议：…”往群里灌任何话，还会被当成组员留痕。口径与 M4 收志愿同款：
    非成员不转发、不落盘，只回一句（D-61 ②）；``roster`` 为空 = 还没登记，谁都算数。
    """
    known = {member.open_id for member in (getattr(roster, "members", None) or ())}
    if known and inbound.sender_open_id not in known:
        return Outcome(replies=(reply(inbound, replies.PROPOSAL_NOT_MEMBER),))

    content = ""
    for prefix in PROPOSAL_PREFIXES:
        if text.startswith(prefix):
            content = text[len(prefix):].strip()
            break
    if not content:
        return Outcome(replies=(reply(inbound, replies.PROPOSAL_EMPTY),))

    group = (state or {}).get("group_chat_id") or ""
    if not group or not group_allowed(state, group):
        # 发不到群就别假装发了：不转达、也不落盘（留痕是给"已发布的内容"追责用的）。
        # 多群隔离（§1.10 b）：记下的群若不是本场的群，同样按"没有群"处理 ——
        # 否则匿名提议会被发到别的群，而本群永远看不到（实测就是这样）。
        return Outcome(replies=(reply(inbound, replies.NEED_GROUP),))

    return Outcome(
        replies=(
            Reply(chat_id=group, text=replies.PROPOSAL_POSTED.format(text=content)),
            reply(inbound, replies.PROPOSAL_ACK),
        ),
        save_proposal={
            "user_id": inbound.sender_open_id,
            "text": content,
            "created_at": (now or datetime.now()).isoformat(timespec="seconds"),
        },
    )


def remember_file(inbound: Inbound, state: dict, now: datetime | None = None) -> Outcome:
    """把 file_key 存进 ``state.pending_file``，等文字消息来配对（D-42）。

    只缓存、不下载：下载是 I/O，归 app 层；router 连"要不要下载"都不决定。
    """
    name = inbound.file_name or "（未命名文件）"
    pending = {
        "file_key": inbound.file_key,
        "file_name": inbound.file_name,
        "resource_type": "file",          # 当前只缓存文件；字段集见 D-45
        "chat_id": inbound.chat_id,
        "message_id": inbound.message_id,
        "received_at": (now or datetime.now()).isoformat(timespec="seconds"),
    }
    return Outcome(
        replies=(reply(inbound, replies.FILE_RECEIVED.format(name=name)),),
        state={**(state or {}), "pending_file": pending},
    )


def _assignment(
    inbound: Inbound,
    state: dict,
    now: datetime | None = None,
    *,
    has_rubric: bool = False,
    has_cards: bool = False,
) -> Outcome:
    """「作业书」：有可用文件就起主链路。

    **盘上已经有一场的产物时**（``has_rubric or has_cards``）改成走「重置」那套确认
    （2026-09-16 口径 B）：先列出"换掉会失去什么"，组长回「确定重置」才动手。
    为什么：换 PDF 只覆盖 assignment/rubric/cards **三份文件**，而方向、票数、志愿、分配
    全是上一场的 —— 不确认就换，下一次「报告」会把新旧混在一张总表里。
    **这也顺带省 token**：确认之前不再解析新文件（M1 一次要花 30 秒和一笔钱）。
    """
    if not _pending_file(state, now, inbound=inbound):
        return Outcome(replies=(reply(inbound, replies.FILE_MISSING),))
    if has_rubric or has_cards:
        return Outcome(
            state={**(state or {}), "reset": reset_block("replace", inbound, now)},
            pipeline="reset_request",
        )
    return Outcome(replies=(reply(inbound, replies.PARSING),), pipeline="assignment")


def _pending_file(
    state: dict, now: datetime | None = None, *, inbound: Inbound | None = None
) -> dict:
    """有没有**可用**的缓存文件 —— 唯一的入口，有效期（D-46）与会话（D-47）都在这里判。

    过期、或不是**这个会话**发的文件，都当没有：调用方自然回既有的 ``FILE_MISSING``，
    ``pipeline`` 也不会起。

    ``inbound`` 必须是 keyword-only：``now`` 是第 2 个位置参数，写成位置参数的话
    ``_pending_file(state, now, inbound)`` 会把 inbound 静默塞进 now —— 不报错，
    只是功能失效，极难查。
    """
    if not isinstance(state, dict):
        return {}
    pending = state.get("pending_file") or {}
    if not pending or _stale_pending(pending, now):
        return {}
    # 别的会话发的文件不算数：私聊里每人一个 chat_id，全局单份会被互相顶掉（D-42 ④
    # 定的演示方式就是各自私聊投递，所以这是必现路径，不是边角）。
    # 字段缺省则放行（兼容旧 state，与 _stale_pending 同款防御）。
    if inbound is not None and pending.get("chat_id") and pending["chat_id"] != inbound.chat_id:
        return {}
    return pending


def _stale_pending(pending: dict, now: datetime | None = None) -> bool:
    raw = pending.get("received_at")
    if not raw:
        return False                      # 老 state 没有这个字段：不因此失效
    try:
        received = datetime.fromisoformat(str(raw))
    except ValueError:
        return False                      # 时间戳脏了就当没有 TTL，别因脏数据把文件丢掉
    return (now or datetime.now()) - received > PENDING_FILE_TTL


# ---------- 无评分点模式：待确认窗口（口径 A，2026-09-16）----------
#
# 背景：作业书里没有评分标准时，M1 返回空 rubric，**默认仍然拒拆**（D-48 不改，
# 也不拿正文要求凑数）。但人可以拍板走另一条路：组长确认后，从正文的交付要求拆一版，
# 卡片显式标注"来自正文要求、不是评分点"。拍板权在人（B9），机器不自动硬拆
# （S13：发一篇新闻进来也不该被拆成任务卡）。


def no_rubric_block(path, meta, inbound: Inbound, now: datetime | None = None) -> dict:
    """建一个"等我确认"的无评分点窗口（写进 ``state.no_rubric``）。

    只缓存**文件路径 + 作业元信息**（都很小）：正文确认时重抽一次就行
    （``extract_text`` 是纯函数、不花 token），不必把几万字塞进 ``state.json``。
    ``meta`` 必须留着 —— 拒拆时按 D-49 ② **一个字都不落盘**，确认之后要拿它渲染清单。
    """
    moment = now or datetime.now()
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return {
        "path": str(path),
        "file_name": name,
        "meta": meta.to_dict(),
        "chat_id": inbound.chat_id,
        "opened_at": moment.isoformat(timespec="seconds"),
        "expires_at": (moment + NO_RUBRIC_TTL).isoformat(timespec="seconds"),
    }


def no_rubric_expired(block: dict, now: datetime | None = None) -> bool:
    """窗口过期了没。时间戳脏了当没过期（与 ``_stale_pending`` 同款防御，别因脏数据卡死人）。"""
    raw = (block or {}).get("expires_at")
    if not raw:
        return False                      # 老 state 没有这个字段：不因此失效
    try:
        expires = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    return (now or datetime.now()) >= expires


def confirm_no_rubric(
    inbound: Inbound, state: dict, roster, now: datetime | None = None
) -> Outcome:
    """「按正文拆」：组长确认 → 走无评分点模式（口径 A，2026-09-16）。

    三重校验，缺一不可：
      ① **只认组长** —— 与「报告」「封盘」同款：会整份覆盖产物的动作由组长拍板；
         非成员/认不出的人当然也过不了这一关。
      ② **必须在收到那份文件的会话里发** —— 与「作业书」的会话校验（D-47）同款，
         否则 A 群里的人能替 B 会话里投的那份文件拍板。
      ③ **窗口未过期**（``NO_RUBRIC_TTL``）—— 过期就当没有，让人重新走一遍「作业书」。

    判定与执行同源：这里只决定"起不起"（``pipeline="no_rubric"``），
    真正的重活在 app 层 —— 不会出现"回了「收到，正在拆」却什么都没跑"。
    """
    if not inbound.sender_open_id or inbound.sender_open_id != getattr(roster, "leader", None):
        return Outcome(replies=(reply(inbound, replies.NO_RUBRIC_NEED_LEADER),))
    block = (state or {}).get("no_rubric") or {}
    if not block or no_rubric_expired(block, now):
        return Outcome(replies=(reply(inbound, replies.NO_RUBRIC_NO_PENDING),))
    if block.get("chat_id") and block["chat_id"] != inbound.chat_id:
        return Outcome(replies=(reply(inbound, replies.NO_RUBRIC_NO_PENDING),))
    return Outcome(
        replies=(reply(inbound, replies.NO_RUBRIC_STARTED),), pipeline="no_rubric"
    )


# ---------- 「重置」：清掉这一场的产物（第 11 条指令，2026-09-16）----------
#
# 需求（用户 2026-09-16 提出）：想换一份作业书、或者干脆从头来一遍时，
# 能一句话清掉之前的数据，但**必须机器人再问一次、确认后才执行**（防误删）。
#
# 为什么真需要它：`data/` 是**全局共享、不分群**（D-57 的有意取舍），而换 PDF 只覆盖
# assignment/rubric/cards 三份 —— 方向、票数、志愿、分配全是上一场的，混在一起会让
# 「报告」出假表、旧窗口还会吃新消息。所以"忘掉上一场"要有**一个正式动作**，
# 而不是靠人去手删文件。


def reset_block(kind: str, inbound: Inbound, now: datetime | None = None) -> dict:
    """建一个"等我确认重置"的窗口（写进 ``state.reset``）。

    ``kind``：``"wipe"`` = 只清（第 11 条指令的第一步）；
    ``"replace"`` = 换作业书触发的（清单里会说明"然后按新文件重跑"）。
    """
    moment = now or datetime.now()
    return {
        "kind": kind if kind in ("wipe", "replace") else "wipe",
        "chat_id": inbound.chat_id,
        "requested_by": inbound.sender_open_id,
        "opened_at": moment.isoformat(timespec="seconds"),
        "expires_at": (moment + RESET_TTL).isoformat(timespec="seconds"),
    }


def reset_expired(block: dict, now: datetime | None = None) -> bool:
    """窗口过期了没。时间戳脏了当没过期（与 ``_stale_pending`` 同款防御）。"""
    raw = (block or {}).get("expires_at")
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    return (now or datetime.now()) >= expires


def request_reset(
    inbound: Inbound, state: dict, roster, now: datetime | None = None
) -> Outcome:
    """「重置」第一步：只认组长 + 只认群里，开一个待确认窗口（**不删任何东西**）。

    清单由 app 层渲染（它才读得到盘上的计数）—— 与「方向」「报告」同一个分工：
    router 决定"能不能起"，app 说"具体是什么"。
    """
    if inbound.chat_type != "group":
        return Outcome(replies=(reply(inbound, replies.RESET_NEED_GROUP),))
    if not inbound.sender_open_id or inbound.sender_open_id != getattr(roster, "leader", None):
        return Outcome(replies=(reply(inbound, replies.RESET_NEED_LEADER),))
    return Outcome(
        state={**(state or {}), "reset": reset_block("wipe", inbound, now)},
        pipeline="reset_request",
    )


def confirm_reset(
    inbound: Inbound, state: dict, roster, now: datetime | None = None
) -> Outcome:
    """「确定重置」：确认 → 真删（app 层先整份备份再删）。

    三重校验，缺一不可：
      ① **只认"发起那次重置的人"** —— 「重置」这条命令本身只认组长（``request_reset``），
         所以这里同时满足"只认组长"；而换作业书那一路的发起人是**发文件的那位**
         （他是谁由 M1/M3 的身份闸保证是花名册成员），让他自己确认第二次，防的是误删。
      ② **只在发起那次重置的会话里认**（与「作业书」的 D-47 同款）。
      ③ **窗口未过期**（``RESET_TTL``）。
    """
    block = (state or {}).get("reset") or {}
    requester = block.get("requested_by") or ""
    if not inbound.sender_open_id or inbound.sender_open_id != requester:
        return Outcome(replies=(reply(inbound, replies.RESET_NEED_REQUESTER),))
    if not block or reset_expired(block, now):
        return Outcome(replies=(reply(inbound, replies.RESET_NO_PENDING),))
    if block.get("chat_id") and block["chat_id"] != inbound.chat_id:
        return Outcome(replies=(reply(inbound, replies.RESET_NO_PENDING),))
    return Outcome(pipeline="reset_confirm")
