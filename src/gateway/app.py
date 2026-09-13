"""M0 组装与入口 —— 依赖注入 + ``main()``。

依据：M0 网关方案 §2 / §6 / §7 / §10 / §12。

两条纪律写在这里：
  * **回调里不干重活**（方案 §6）：``on_event`` 只做"路由 + 回话"，M1/M3 那种 10–30 秒
    的活丢后台线程，否则长连接的事件循环被堵住、消息收不到；
  * **判定只有一处**：路由判定在 ``router.py``、覆盖率判定在 ``intelligence/``，
    本文件只做组装、落盘与异常转文案。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

from src.config import ConfigError, load_config
from src.gateway import replies
from src.gateway.client import FeishuClient
from src.gateway.events import Inbound, Outcome, Reply, reply, to_inbound
from src.gateway.router import route
from src.intelligence.coverage import coverage_loop
from src.intelligence.decompose import decompose
from src.intelligence.extract import (
    ExtractError,
    check_deadline,
    check_radical_residue,
    check_weight_sum,
    extract_text,
)
from src.intelligence.llm import LLMClient, LLMError
from src.intelligence.parse import parse_assignment
from src.models import AssignmentRecord, Preference, Roster
from src.report.checklist import render_checklist
from src.storage import PREFERENCES, PROPOSALS, SEEN, JsonStore

__all__ = ["Gateway", "main"]

# data/seen.json 只留最近这么多条 message_id（P0-A）。
_SEEN_LIMIT = 200


class Gateway:
    """把连接层、纯路由、落盘、智能层接起来。全是依赖注入，方便离线测。"""

    def __init__(self, config, store, sender, downloader, llm_client=None) -> None:
        self.config = config
        self.store = store
        self.sender = sender
        self.downloader = downloader
        self._llm_client = llm_client

    # ---------- 飞书回调入口 ----------

    def on_event(self, data) -> None:
        """回调绝不能被业务异常搞崩 —— 崩了长连接还在，但消息就静默丢了。"""
        try:
            self.handle(to_inbound(data))
        except Exception as exc:
            print(
                f"[M0] 处理事件出错（已忽略）：{type(exc).__name__}: {exc}", file=sys.stderr
            )

    def handle(self, inbound: Inbound) -> Outcome:
        """快路径：路由 → 回话 → 落盘 → 需要时起后台重活。

        第一件事是**按 message_id 去重**（P0-A）：飞书会重复投递 / 重连补投同一个
        事件，不去重就会把同一条指令完整跑两遍（新群"先清单、再总表"就是这么来的）。
        重复事件直接丢掉：不发消息、不写业务数据。

        每条消息先打一行日志（P1-G）：真机出问题时先看这一行，否则永远是黑盒。
        """
        print(
            f"[M0] recv id={inbound.message_id} chat={inbound.chat_id} "
            f"from={inbound.sender_open_id} type={inbound.message_type} "
            f"text={inbound.text[:40]}"
        )
        if self._is_duplicate(inbound.message_id):
            # 去重命中也要留痕，否则看不出到底有没有重复投递（P1-G / P0-A）
            print(f"[M0] dup 跳过 id={inbound.message_id}")
            return Outcome()
        self._remember_group(inbound)
        state = self.store.load_state()
        has_rubric = bool(self.store.load_rubric())
        meta = self.store.load_assignment()
        outcome = route(
            inbound,
            state,
            self.store.load_members(),
            has_rubric=has_rubric,
            cards=self.store.load_cards(),
            preferences=self.store.load_preferences(),
            source_title=meta.title if meta else "",
        )
        failures = self._deliver(outcome)
        # 主动私聊发不出去要说出来（P0-C）：否则"群里说清单已发、实际没人收到"。
        self._report_dm_failures(failures, outcome.state or state)

        # 重活起不起，route() 已经判过（Outcome.pipeline）—— 这里不再自己判一遍，
        # 否则"回了「表单没看懂」却照样跑 M1"（必修 4）
        if outcome.pipeline:
            threading.Thread(
                target=self.run_pipeline, args=(outcome.pipeline, inbound, state), daemon=True
            ).start()
        return outcome

    def _is_duplicate(self, message_id: str) -> bool:
        """P0-A：同一条消息只处理一次。落 ``data/seen.json``，只留最近 ``_SEEN_LIMIT`` 条。

        ``message_id`` 为空（老事件 / 单测夹具）时不去重 —— 没有标识就没法认人。
        """
        if not message_id:
            return False
        seen = self.store.read_raw(SEEN, []) or []
        if message_id in seen:
            return True
        self.store.mutate_raw(
            SEEN, lambda items: [*(items or []), message_id][-_SEEN_LIMIT:], default=[]
        )
        return False

    def _deliver(self, outcome: Outcome) -> tuple[Reply, ...]:
        """发出所有回复，返回**发失败的**那些（P0-C）。"""
        failed: list[Reply] = []
        for message in outcome.replies:
            try:
                self.sender.send(message)
            except Exception as exc:
                # 一条发失败不能吃掉后面几条：M4 开窗口要连发"清单发群 + 每人私聊"，
                # 某个人的私聊发不出去，群里的清单必须照发（方案 §7：不能静默失败）。
                failed.append(message)
                print(
                    f"[M0] 发消息失败（已跳过）：{type(exc).__name__}: {exc}", file=sys.stderr
                )
        if outcome.state is not None:
            self.store.save_state(outcome.state)
        if outcome.save_roster is not None:
            self.store.save_members(Roster.from_dict(outcome.save_roster))
        if outcome.save_preference is not None:
            self._save_preference(outcome.save_preference)
        if outcome.save_assignments:
            self._save_assignments(outcome.save_assignments)
        if outcome.save_proposal is not None:
            self._save_proposal(outcome.save_proposal)
        return tuple(failed)

    def _report_dm_failures(self, failures, state) -> None:
        """私聊发不出去就在群里补一句（P0-C）。只统计 ``open_id`` 目标 —— 那才是"人"。"""
        dm_failed = [r for r in failures if r.receive_id_type == "open_id"]
        group = (state or {}).get("group_chat_id") or ""
        if not dm_failed or not group:
            return
        try:
            self.sender.send(
                Reply(
                    chat_id=group,
                    text=replies.PREFERENCE_DM_FAILED.format(count=len(dm_failed)),
                )
            )
        except Exception as exc:
            print(
                f"[M0] 补发失败提示也失败了：{type(exc).__name__}: {exc}", file=sys.stderr
            )

    # ---------- M4 / M5 的落盘 ----------

    def _remember_group(self, inbound: Inbound) -> None:
        """任何群消息都刷新 ``state.group_chat_id``（D-54）。

        M4 的清单 / 总表、M5 的匿名转达都要**主动发到群**，而 router 是纯函数、不读
        文件 —— 所以"群是哪个"由 app 层记进 state。机器人自己的消息不算：那是回声，
        不是"群里有人在活动"。
        """
        if inbound.sender_type == "app" or inbound.chat_type != "group" or not inbound.chat_id:
            return
        state = self.store.load_state()
        if state.get("group_chat_id") == inbound.chat_id:
            return
        state["group_chat_id"] = inbound.chat_id
        self.store.save_state(state)

    def _save_preference(self, payload: dict) -> None:
        """按 ``user_id`` **覆盖**写志愿（后投覆盖先投，D-33 / §6.3）。

        走 ``mutate_many``：它是"类型化列表的原子读-改-写"，正是为 M4 收志愿准备的
        原语 —— 两个组员同时私聊回复时不会丢更新。
        """
        preference = Preference.from_dict(payload)
        self.store.mutate_many(
            PREFERENCES,
            Preference,
            lambda items: [p for p in items if p.user_id != preference.user_id] + [preference],
        )

    def _save_assignments(self, payloads) -> None:
        """整份分配结果一次性覆盖（M4 结算，§6.4）。"""
        self.store.save_assignments([AssignmentRecord.from_dict(p) for p in payloads])

    def _save_proposal(self, payload: dict) -> None:
        """追加一条提议 —— **含真实 ``user_id``**，这是防滥用留痕（§6.5）。

        ``proposals.json`` 的字段级定义在 requirements 里没有（§6.5 只规定了语义），
        所以走裸 JSON 的原子读-改-写，不硬造数据类。
        """
        self.store.mutate_raw(PROPOSALS, lambda items: [*(items or []), payload], default=[])

    # ---------- 慢路径：M1 / M3 ----------

    def run_pipeline(self, kind: str, inbound: Inbound, state: dict) -> None:
        """后台线程里跑。异常一律转成一句人话回群里（方案 §7：不能静默失败）。"""
        try:
            if kind == "assignment":
                self._run_assignment(inbound, state)
            elif kind == "decompose":
                self._run_decompose(inbound)
        except ExtractError as exc:
            self.sender.send(reply(inbound, replies.EXTRACT_REJECTED.format(reason=exc)))
        except LLMError:
            self.sender.send(reply(inbound, replies.PARSE_FAILED))
        except Exception as exc:                      # 兜底也要说话
            self.sender.send(reply(inbound, f"{replies.PARSE_FAILED}（{type(exc).__name__}）"))
        finally:
            if kind == "assignment":
                pending = (state or {}).get("pending_file") or {}
                self._forget_pending_file(pending.get("message_id", ""))

    def _run_assignment(self, inbound: Inbound, state: dict) -> None:
        """作业书 → 下载 → 抽文本 → M1 → **必须续跑 M3** → 核对清单发群（方案 §7）。"""
        pending = (state or {}).get("pending_file") or {}
        path = self.downloader.download(pending, self.store.uploads)

        text = extract_text(path)
        parsed = parse_assignment(text, self._llm(), source_file=path.name)

        # 空 rubric：M1 全文没找到评分标准（D-48）→ 不跑 M3、不拿正文要求凑数，
        # 也**一个字都不落盘** —— 否则拒拆会把上一份好产物清空（D-49 ②）。
        if not parsed.points:
            self.sender.send(reply(inbound, replies.NO_RUBRIC_FOUND))
            return

        self.store.save_assignment(parsed.meta)
        self.store.save_rubric(list(parsed.points))

        result = decompose(parsed.points, self._llm())
        self.store.save_cards(list(result.cards))

        report = render_checklist(parsed.meta, parsed.points, result.cards, result)
        # 三道软校验都只警告、不拒收（§7.5）：权重加总 + D-43 的部首残留 + D-49 的截止时间。
        warnings = [
            w
            for w in (
                check_weight_sum(parsed.points),
                check_radical_residue(text),
                check_deadline(parsed.meta),
            )
            if w
        ]
        if warnings:
            report += "\n\n" + "\n".join(f"[软警告] {w}" for w in warnings)
        self.sender.send(reply(inbound, report))

    def _run_decompose(self, inbound: Inbound) -> None:
        """「拆解」：用现有评分点重跑 M3，再出一份核对清单。"""
        points = self.store.load_rubric()
        result = decompose(points, self._llm())
        self.store.save_cards(list(result.cards))

        meta = self.store.load_assignment()
        if meta is None:
            coverage = coverage_loop(result.cards, points)
            self.sender.send(
                reply(
                    inbound,
                    f"拆解完成：{len(result.cards)} 张任务卡，"
                    f"覆盖率 {len(coverage.covered)}/{len(coverage.eligible)}。",
                )
            )
            return
        self.sender.send(
            reply(inbound, render_checklist(meta, points, result.cards, result))
        )

    def _forget_pending_file(self, file_message_id: str) -> None:
        """只清**这一轮消费掉的那个文件**（按 message_id 认）。

        跑 M1 的十几秒里群里可能又来了新 PDF：无脑 pop 会把新文件一起删掉，之后
        「作业书」回「请先把作业书文件发给我」—— 用户明明刚发过（必修 5）。
        成败都清（不留旧文件），但只在还是同一个文件时才清。
        """
        state = self.store.load_state()
        pending = state.get("pending_file") or {}
        if (pending.get("message_id") or "") != (file_message_id or ""):
            return
        state.pop("pending_file", None)
        self.store.save_state(state)

    def _llm(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = LLMClient.from_config(self.config)
        return self._llm_client


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="M0 飞书网关（长连接，不占端口）")
    parser.add_argument("--seconds", type=int, default=0, help="到点自动退出；0 = 一直跑")
    parser.add_argument("--quiet", action="store_true", help="关掉 SDK 的连接日志")
    args = parser.parse_args(argv)

    try:
        config = load_config()
        config.check_feishu()
        config.check_llm()
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    store = JsonStore(config.data_dir)
    store.ensure_dirs()
    client_kwargs = {}
    if args.quiet:
        import lark_oapi as lark

        client_kwargs["log_level"] = lark.LogLevel.WARNING
    client = FeishuClient(config, **client_kwargs)
    gateway = Gateway(config, store, sender=client, downloader=client)

    print("[M0] 正在建立长连接（首次加载 SDK 约 20 秒，属正常）", file=sys.stderr)
    print("     测试群里 @机器人 发「拆解」即可开始；Ctrl+C 退出", file=sys.stderr)
    if args.seconds > 0:
        threading.Timer(args.seconds, lambda: os._exit(0)).start()

    try:
        client.start(gateway.on_event)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
