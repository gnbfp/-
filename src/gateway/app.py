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
import json
import os
import socket
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from src.config import ConfigError, load_config
from src.gateway import allocation, reminder, replies, vote
from src.gateway.client import FeishuClient
from src.gateway.events import ImageOut, Inbound, Outcome, Reply, reply, to_inbound
from src.gateway.router import route
from src.intelligence.coverage import coverage_loop
from src.intelligence.decompose import DecomposeResult, check, decompose
from src.intelligence.direction import generate_directions
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
from src.report.gantt import render_gantt
from src.storage import (
    ASSIGNMENTS,
    GANTT,
    PREFERENCES,
    PROPOSALS,
    REMINDERS,
    REPORT,
    SEEN,
    JsonStore,
)

__all__ = ["Gateway", "main"]

# data/seen.json 只留最近这么多条 message_id（P0-A）。
_SEEN_LIMIT = 200

# 单实例保护的守护端口（P0-E）：第二个进程 bind 不上就拒绝启动。
INSTANCE_PORT = 47653
LOCK_FILE = "app.lock"


def _stamp() -> str:
    """日志时间戳（P1-J）：明天复盘要靠它把两个进程的行对齐。"""
    return datetime.now().isoformat(timespec="seconds")


class SingleInstance:
    """单实例保护（P0-E）—— 机器上只允许跑一个网关。

    真机故障：同时跑了两个 `python -m src.gateway.app`，同一套凭据 → 两条
    WebSocket → 每条群消息被两个进程各处理一次（重复回话、落盘互相覆盖）。
    `message_id` 去重（P0-A）对两个进程只能偶尔挡住（各读各的 seen.json，有竞态），
    所以要在**进程级**互斥。

    Windows 上按 pid 判存活不安全（`os.kill(pid, 0)` 会真的把进程杀掉），所以
    守卫用 **bind 127.0.0.1:<port>** —— bind 是原子的，第二个实例必然失败。
    `data/app.lock` 只写来给人看（pid / started_at），**不参与判定**：硬退
    （`os._exit`）留下的残留锁文件不会挡住下一次启动。
    """

    def __init__(self, data_dir, port: int = INSTANCE_PORT) -> None:
        self.path = Path(data_dir) / LOCK_FILE
        self.port = port
        self._sock: socket.socket | None = None

    def acquire(self) -> bool:
        """``True`` = 抢到；``False`` = 已有实例在跑，别启动。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", self.port))
        except OSError:
            sock.close()
            return False
        self._sock = sock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"pid": os.getpid(), "started_at": _stamp(), "port": self.port},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return True

    def release(self) -> None:
        """正常退出：放掉端口 + 删锁。"""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def describe(self) -> str:
        """把现有锁文件读成人话，给拒绝启动时的那行错误用。"""
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""


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
            f"[M0] {_stamp()} recv id={inbound.message_id} chat={inbound.chat_id} "
            f"from={inbound.sender_open_id} type={inbound.message_type} "
            f"text={inbound.text[:40]}"
        )
        if self._is_duplicate(inbound.message_id):
            # 去重命中也要留痕，否则看不出到底有没有重复投递（P1-G / P0-A）
            print(f"[M0] {_stamp()} dup 跳过 id={inbound.message_id}")
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
            assignments=self.store.load_assignments(),
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

    def _send(self, message: Reply):
        """发一条并打一行轨迹（P1-J）。返回 ``None`` = 成功，否则返回异常。"""
        try:
            self.sender.send(message)
        except Exception as exc:
            print(
                f"[M0] {_stamp()} -> {message.receive_id_type}:{message.chat_id} "
                f"失败({type(exc).__name__}: {exc}) | {message.text[:40]}"
            )
            return exc
        print(
            f"[M0] {_stamp()} -> {message.receive_id_type}:{message.chat_id} "
            f"ok | {message.text[:40]}"
        )
        return None

    def _deliver(self, outcome: Outcome) -> tuple[Reply, ...]:
        """发出所有回复，返回**发失败的**那些（P0-C）。每条都打一行轨迹（P1-J）。"""
        failed: list[Reply] = []
        for message in outcome.replies:
            # 一条发失败不能吃掉后面几条：M4 开窗口要连发"清单发群 + 每人私聊"，
            # 某个人的私聊发不出去，群里的清单必须照发（方案 §7：不能静默失败）。
            if self._send(message) is not None:
                failed.append(message)
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
        if outcome.save_direction is not None:
            self._save_direction(outcome.save_direction)
        if outcome.save_complete is not None:
            self._save_complete(outcome.save_complete)
        for image in outcome.images:                 # M7 甘特图：文本先发、图后发
            if self._send_image(image) is not None:  # 图发失败要在群里说（必修 D）
                self._send(Reply(chat_id=image.chat_id, text=replies.IMAGE_SEND_FAILED))
        return tuple(failed)

    def _report_dm_failures(self, failures, state) -> None:
        """私聊发不出去就在群里补一句（P0-C）。只统计 ``open_id`` 目标 —— 那才是"人"。"""
        dm_failed = [r for r in failures if r.receive_id_type == "open_id"]
        # 群取**志愿窗口自己记的** chat_id（P1-H），取不到再退回 state.group_chat_id ——
        # 同 settle()：窗口开着时只要有别的群来一条消息，group_chat_id 就会被刷成那个群
        pref = (state or {}).get("preference") or {}
        group = pref.get("chat_id") or (state or {}).get("group_chat_id") or ""
        if not dm_failed or not group:
            return
        self._send(
            Reply(
                chat_id=group,
                text=replies.PREFERENCE_DM_FAILED.format(count=len(dm_failed)),
            )
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
        """整份分配结果一次性覆盖（M4 结算，§6.4）。

        ``completed_at`` 是执行期的证据，不能因为重开一次志愿窗口就归零（D-67）——
        覆盖前按 ``task_id`` 把旧的完成时间合并回来，**但只在负责人没变时**：
        卡换了人，新负责人的"完成"不该继承前任的。
        """
        previous = {r.task_id: r for r in (self.store.load_assignments() or ())}
        merged = []
        for payload in payloads:
            record = AssignmentRecord.from_dict(payload)
            old = previous.get(record.task_id)
            if not record.completed_at and old is not None and old.assignee == record.assignee:
                record = replace(record, completed_at=old.completed_at)
            merged.append(record)
        self.store.save_assignments(merged)

    def _save_proposal(self, payload: dict) -> None:
        """追加一条提议 —— **含真实 ``user_id``**，这是防滥用留痕（§6.5）。

        ``proposals.json`` 的字段级定义在 requirements 里没有（§6.5 只规定了语义），
        所以走裸 JSON 的原子读-改-写，不硬造数据类。
        """
        self.store.mutate_raw(PROPOSALS, lambda items: [*(items or []), payload], default=[])

    def _save_direction(self, payload: dict) -> None:
        """整份方向结果一次性覆盖（M2 落定，§2.6）—— 裸 JSON，口径同 proposals.json。"""
        self.store.save_direction(payload)

    def _save_complete(self, payload: dict) -> None:
        """M6 的完成标记（§2.1）：走 ``mutate_many`` **只改那一条**，其余原样。

        两件事都写进 ``assignments.json`` 的同一行，所以必须走那个"类型化列表的原子
        读-改-写"原语 —— 直接整份覆盖会把别人刚标的完成擦掉。
        """
        task_id = payload.get("task_id")
        completed_at = payload.get("completed_at")
        self.store.mutate_many(
            ASSIGNMENTS,
            AssignmentRecord,
            lambda items: [
                replace(record, completed_at=completed_at)
                if record.task_id == task_id
                else record
                for record in items
            ],
        )

    def _send_image(self, image: ImageOut):
        """发一张图并打一行轨迹（P1-J）。返回 ``None`` = 成功，否则返回异常。"""
        try:
            self.sender.send_image(image.chat_id, image.path, image.receive_id_type)
        except Exception as exc:
            print(
                f"[M0] {_stamp()} -> {image.receive_id_type}:{image.chat_id} "
                f"图片失败({type(exc).__name__}: {exc}) | {image.path}"
            )
            return exc
        print(
            f"[M0] {_stamp()} -> {image.receive_id_type}:{image.chat_id} "
            f"图片 ok | {image.path}"
        )
        return None

    # ---------- 慢路径：M1 / M2 / M3 ----------

    def run_pipeline(self, kind: str, inbound: Inbound, state: dict) -> None:
        """后台线程里跑。异常一律转成一句人话回群里（方案 §7：不能静默失败）。

        回话一律走 ``_send()``（P1-J）：它不抛异常、失败也留轨迹 —— 否则
        "except 里再发一次、再失败"会让整条后台线程静默死掉，屏幕上什么都看不到。
        """
        try:
            if kind == "assignment":
                self._run_assignment(inbound, state)
            elif kind == "decompose":
                self._run_decompose(inbound)
            elif kind == "direction":
                self._run_direction(inbound)
            elif kind == "report":
                self._run_report(inbound)
        except ExtractError as exc:
            self._send(reply(inbound, replies.EXTRACT_REJECTED.format(reason=exc)))
        except LLMError:
            self._send(reply(inbound, replies.PARSE_FAILED))
        except Exception as exc:                      # 兜底也要说话
            self._send(reply(inbound, f"{replies.PARSE_FAILED}（{type(exc).__name__}）"))
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
            self._send(reply(inbound, replies.NO_RUBRIC_FOUND))
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
        self._send(reply(inbound, report))

    def _run_decompose(self, inbound: Inbound) -> None:
        """「拆解」：用现有评分点重跑 M3，再出一份核对清单。"""
        points = self.store.load_rubric()
        result = decompose(points, self._llm())
        self.store.save_cards(list(result.cards))

        meta = self.store.load_assignment()
        if meta is None:
            coverage = coverage_loop(result.cards, points)
            self._send(
                reply(
                    inbound,
                    f"拆解完成：{len(result.cards)} 张任务卡，"
                    f"覆盖率 {len(coverage.covered)}/{len(coverage.eligible)}。",
                )
            )
            return
        self._send(
            reply(inbound, render_checklist(meta, points, result.cards, result))
        )

    def _run_direction(self, inbound: Inbound) -> None:
        """「方向」：评分点 → 2–3 个候选（M2 唯一的 LLM 点）→ 开投票窗口发群（§2.2）。

        前置缺哪个就回哪句、不发候选：与 router 的判定口径一致（必修 4）。
        开窗时**重新读一次 state**（生成要花十几秒），别拿十几秒前的快照覆盖回盘 ——
        不然这期间别人刚建的花名册 / 窗口会被一起写没。
        """
        points = self.store.load_rubric()
        if not points:
            # D-48 口径：没有评分点就不生成，不烧 token
            self._send(reply(inbound, replies.NEEDS_RUBRIC))
            return
        roster = self.store.load_members()
        if roster is None or not roster.members:
            self._send(reply(inbound, replies.VOTE_NEED_ROSTER))
            return
        try:
            result = generate_directions(points, self.store.load_assignment(), self._llm())
        except LLMError:
            # 生成不出来就直说，别让群里干等（也不套用「作业书解析失败」那句不对路的兜底）
            self._send(reply(inbound, replies.VOTE_GENERATE_FAILED))
            return
        if not result.ok:
            self._send(reply(inbound, replies.VOTE_GENERATE_FAILED))
            return
        outcome = vote.open_window(
            inbound,
            self.store.load_state(),
            [direction.to_dict() for direction in result.directions],
        )
        failures = self._deliver(outcome)
        self._report_dm_failures(failures, outcome.state or {})

    def _run_report(self, inbound: Inbound) -> None:
        """M7 执行报告（D-64 / D-65）：分配总表 + 核对清单 + 甘特图，发群。

        报告是**从盘上重读的快照**：自检项按现状重算（``generations=0`` —— 报告不是拆解，
        没有"这一版拆了几轮"这回事）。文本落 ``data/report.md``、图落 ``data/gantt.png``。
        """
        meta = self.store.load_assignment()
        points = self.store.load_rubric()
        cards = self.store.load_cards()
        assignments = self.store.load_assignments()
        roster = self.store.load_members()
        if meta is None or not points or not cards or not assignments:
            self._send(reply(inbound, replies.REPORT_NEED_ASSIGNMENTS))
            return
        result = DecomposeResult(
            cards=tuple(cards), failures=tuple(check(cards, points)), generations=0
        )
        try:
            gantt = render_gantt(cards, assignments, meta, self.store.path(GANTT), roster)
        except Exception as exc:                     # 渲染崩了也要说话（方案 §7）
            print(f"[M0] {_stamp()} 甘特图渲染失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            self._send(reply(inbound, replies.REPORT_FAILED))
            return

        # 拼成一条长文本客户端会折叠 → 拆成「总表」「核对清单」两条（必修 F）；
        # data/report.md 仍是两份拼起来的完整版。
        board_text = allocation.render_board(
            assignments,
            cards,
            roster,
            self.store.load_preferences(),
            show_completion=True,
        )
        checklist_text = render_checklist(
            meta, points, cards, result, assignments=assignments, roster=roster
        )
        self.store.path(REPORT).write_text(
            board_text + "\n\n" + checklist_text + "\n", encoding="utf-8"
        )
        self._deliver(
            Outcome(
                replies=(
                    Reply(chat_id=inbound.chat_id, text=board_text),
                    Reply(chat_id=inbound.chat_id, text=checklist_text),
                ),
                images=(ImageOut(chat_id=inbound.chat_id, path=str(gantt)),),
            )
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

    # ---------- 慢路径的定时器：M6 催办 ----------

    def scan_reminders(self, now: datetime | None = None) -> list:
        """M6 临期扫描一轮（§2.2）：发群 @负责人，成败都记 ``data/reminders.json``。

        群取 ``state.group_chat_id``（单群假设 D-57）；取不到就跳过并打一行日志 ——
        **宁可漏催，不可把 @ 发到错误的群**。去重靠 ``(task_id, tier)``（见 ``reminder.scan``）。
        """
        group = (self.store.load_state() or {}).get("group_chat_id") or ""
        if not group:
            print(f"[M6] {_stamp()} 催办跳过：还不知道群是哪个（先让群里有人说句话）")
            return []
        _, tier1, tier2 = reminder.settings()
        due = reminder.scan(
            self.store.load_cards(),
            self.store.load_assignments(),
            self.store.load_assignment(),
            self.store.read_raw(REMINDERS, []) or [],
            now,
            tier1_hours=tier1,
            tier2_hours=tier2,
        )
        for item in due:
            ok = self._send(Reply(chat_id=group, text=item.text)) is None
            record = item.to_record(group, _stamp(), ok)
            self.store.mutate_raw(
                REMINDERS, lambda items, row=record: [*(items or []), row], default=[]
            )
        return due

    def start_reminder_loop(self) -> None:
        """后台线程：**启动先扫一次**（演示不必干等一个钟头），之后按间隔循环（§2.2）。"""
        interval, _, _ = reminder.settings()

        def _loop() -> None:
            while True:
                try:
                    self.scan_reminders()
                except Exception as exc:             # 扫一轮出错不能把线程搞死
                    print(f"[M6] {_stamp()} 催办扫描出错：{type(exc).__name__}: {exc}")

                time.sleep(interval)

        threading.Thread(target=_loop, daemon=True).start()

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

    # P0-E：单实例保护。真机故障是两个进程跑同一套凭据 → 每条消息被处理两次。
    guard = SingleInstance(store.root)
    if not guard.acquire():
        print("[M0] 启动被拒：已有一个网关实例在跑（单实例保护，P0-E）。", file=sys.stderr)
        existing = guard.describe()
        if existing:
            print(f"     现有 app.lock：{existing}", file=sys.stderr)
        print(
            "     同一套凭据跑两个进程 = 每条群消息被处理两次。请先关掉另一个窗口。",
            file=sys.stderr,
        )
        return 3

    try:
        client_kwargs = {}
        if args.quiet:
            import lark_oapi as lark

            client_kwargs["log_level"] = lark.LogLevel.WARNING
        client = FeishuClient(config, **client_kwargs)
        gateway = Gateway(config, store, sender=client, downloader=client)
        gateway.start_reminder_loop()

        print("[M0] 正在建立长连接（首次加载 SDK 约 20 秒，属正常）", file=sys.stderr)
        print("     测试群里 @机器人 发「拆解」即可开始；Ctrl+C 退出", file=sys.stderr)
        if args.seconds > 0:
            def _stop() -> None:
                guard.release()
                os._exit(0)

            threading.Timer(args.seconds, _stop).start()

        try:
            client.start(gateway.on_event)
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
