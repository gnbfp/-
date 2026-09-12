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
from src.gateway.events import Inbound, Outcome, reply, to_inbound
from src.gateway.router import pipeline_kind, route
from src.intelligence.coverage import coverage_loop
from src.intelligence.decompose import decompose
from src.intelligence.extract import ExtractError, check_weight_sum, extract_text
from src.intelligence.llm import LLMClient, LLMError
from src.intelligence.parse import parse_assignment
from src.models import Roster
from src.report.checklist import render_checklist
from src.storage import JsonStore

__all__ = ["Gateway", "main"]


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
        """快路径：路由 → 回话 → 落盘 → 需要时起后台重活。"""
        state = self.store.load_state()
        has_rubric = bool(self.store.load_rubric())
        outcome = route(inbound, state, self.store.load_members(), has_rubric=has_rubric)
        self._deliver(outcome)

        kind = pipeline_kind(inbound, state, has_rubric)
        if kind:
            threading.Thread(
                target=self.run_pipeline, args=(kind, inbound, state), daemon=True
            ).start()
        return outcome

    def _deliver(self, outcome: Outcome) -> None:
        for message in outcome.replies:
            self.sender.send(message)
        if outcome.state is not None:
            self.store.save_state(outcome.state)
        if outcome.save_roster is not None:
            self.store.save_members(Roster.from_dict(outcome.save_roster))

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
                self._forget_pending_file()

    def _run_assignment(self, inbound: Inbound, state: dict) -> None:
        """作业书 → 下载 → 抽文本 → M1 → **必须续跑 M3** → 核对清单发群（方案 §7）。"""
        pending = (state or {}).get("pending_file") or {}
        path = self.downloader.download(pending, self.store.uploads)

        text = extract_text(path)
        parsed = parse_assignment(text, self._llm(), source_file=path.name)
        self.store.save_assignment(parsed.meta)
        self.store.save_rubric(list(parsed.points))

        result = decompose(parsed.points, self._llm())
        self.store.save_cards(list(result.cards))

        report = render_checklist(parsed.meta, parsed.points, result.cards, result)
        warning = check_weight_sum(parsed.points)
        if warning:
            report += f"\n\n[软警告] {warning}"
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

    def _forget_pending_file(self) -> None:
        """消费掉缓存：成败都不留旧文件，免得下次静默复用一份过期作业书。"""
        state = self.store.load_state()
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