"""飞书探针 —— 一次性诊断工具。**不属于 M0 运行时，不要被 src/ import。**

为什么要有这个文件（依据 requirements.md §7.1 的「作业书 + 文件」行）：

主链路第一环是「群里的作业书文件 → 机器人手里」。这一环压着两件无法凭文档确定
的事实，必须实测：

  1. 飞书客户端把「文字 + 挂一个附件」发出去时，是一条消息还是两条？若拆成两条，
     那条 file 消息没有 @ 机器人。原假设：应用只有 im:message.group_at_msg:readonly
     （只收被 @ 到的群消息）—— 那条文件消息可能压根不会推给机器人。
     后台实际开通的是 im:message.group_msg.include_bot:read + im:message:readonly
     （见 requirements.md §7.1），群里不带 @ 也收得到 —— 此处旧假设已被实测推翻。
  2. 收到 file 消息后，file_key 能不能换成字节流？这取决于「获取与上传图片或文件
     资源」权限是否已开通。

跑法（在源码根目录）::

    python -m tools.probe_feishu                  # 一直跑，Ctrl+C 退出
    python -m tools.probe_feishu --seconds 180    # 180 秒后自动退出
    python -m tools.probe_feishu --echo           # 额外回一句，顺带验发送权限
    python -m tools.probe_feishu --quiet          # 关掉 SDK 的连接日志

下载到的文件落在 data/probe/（data/ 已被 .gitignore 忽略，不会进仓库）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config import ConfigError, load_config

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

# 消息类型 -> (内容里的资源字段名, 下载接口的 type 参数)
RESOURCE_FIELD = {
    "file": ("file_key", "file"),
    "image": ("image_key", "image"),
    "audio": ("file_key", "file"),
    "media": ("file_key", "file"),
    "video": ("file_key", "file"),
    "sticker": ("file_key", "file"),
}


def _print_steps() -> None:
    print()
    print("=" * 68)
    print("请照这个顺序在飞书里做（每做一步，回来看终端有没有打印事件）：")
    print(r"  ①【私聊】给机器人发一句：  你好")
    print(r"  ②【私聊】直接把一个 PDF 拖进去发送（不带文字、不 @）")
    print(r"       例：C:\Users\<你的用户名>\Desktop\作业书\示例作业书.pdf")
    print(r"  ③【私聊】发一句纯文字：  作业书")
    print(r"  ④【群里】@机器人 发：  @机器人 拆解")
    print(r"  ⑤【群里】不带 @，直接发一条 PDF")
    print(r"  ⑥【群里】发一条「文字 + 挂 PDF + 同时也 @机器人」的消息")
    print()
    print("重点看 ②⑤⑥：文件消息到底收到几条？file_key 能不能下载？")
    print("①②③用来验证「私聊不需要 @ 也能全收」这条退路是否成立。")
    print("=" * 68)
    print()


def check_credentials(cfg) -> bool:
    """第 1 步：拿 tenant_access_token，证明这两把钥匙是真的。"""
    import httpx

    print("[1/3] 凭据检查")
    print("      FEISHU_APP_ID     = %s" % cfg.feishu_app_id)
    print("      FEISHU_APP_SECRET = <已设置,%d 位>" % len(cfg.feishu_app_secret))
    try:
        resp = httpx.post(
            TOKEN_URL,
            json={"app_id": cfg.feishu_app_id, "app_secret": cfg.feishu_app_secret},
            timeout=15,
        )
        payload = resp.json()
    except Exception as exc:
        print("      [X] 请求没发出去：%s: %s" % (type(exc).__name__, exc))
        return False
    if payload.get("code") != 0:
        print("      [X] code=%s msg=%s" % (payload.get("code"), payload.get("msg")))
        print("        -> 钥匙不对，或应用被停用。去飞书后台核对 App ID / Secret")
        return False
    print("      [OK] tenant_access_token 已取到（%ss 有效期）" % payload.get("expire"))
    return True


class MessageProbe:
    """把收到的每条消息事件原样打出来；是文件就顺手试下载。"""

    def __init__(self, cfg, download=True, echo=False, save_dir=None):
        self.cfg = cfg
        self.download = download
        self.echo = echo
        self.save_dir = Path(save_dir)
        self.seq = 0
        self.seen = set()
        self.log = []            # (message_type, @个数, 摘要, chat_type)
        self._client = None

    # ---- SDK 客户端（懒加载：import lark_oapi 本身要 ~20 秒）----
    def _lark_client(self):
        if self._client is None:
            import lark_oapi as lark

            self._client = (
                lark.Client.builder()
                .app_id(self.cfg.feishu_app_id)
                .app_secret(self.cfg.feishu_app_secret)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
        return self._client

    # ---- 事件入口：探针绝不能被解析异常搞崩 ----
    def on_message(self, data) -> None:
        try:
            self._handle(data)
        except Exception as exc:
            print("  !! 处理事件时出错（已忽略，不影响继续收）：%s: %s"
                  % (type(exc).__name__, exc))

    def _handle(self, data) -> None:
        msg = data.event.message
        sender = data.event.sender
        message_id = getattr(msg, "message_id", None)

        if message_id and message_id in self.seen:
            print("")
            print("[重复投递] message_id=%s 已处理过，跳过" % message_id)
            return
        if message_id:
            self.seen.add(message_id)

        self.seq += 1
        mtype = getattr(msg, "message_type", None)
        chat_type = getattr(msg, "chat_type", None)
        sender_type = getattr(sender, "sender_type", None)
        sender_open_id = getattr(getattr(sender, "sender_id", None), "open_id", None)
        mentions = getattr(msg, "mentions", None) or []
        raw = getattr(msg, "content", None) or ""
        chat_id = getattr(msg, "chat_id", None)
        payload = self._parse_content(raw)

        print("")
        print("=" * 68)
        print("[事件 %d] %s" % (self.seq, time.strftime("%H:%M:%S")))
        print("  message_type = %r" % (mtype,))
        print("  chat_type    = %r   (p2p = 私聊, group = 群)" % (chat_type,))
        print("  chat_id      = %s" % chat_id)
        print("  message_id   = %s" % message_id)
        print("  sender       = %r  open_id=%s" % (sender_type, sender_open_id))
        print("  @ 到的人     = %d 个" % len(mentions))
        for m in mentions:
            m_open_id = getattr(getattr(m, "id", None), "open_id", None)
            print("      - name=%r mentioned_type=%r open_id=%s"
                  % (getattr(m, "name", None), getattr(m, "mentioned_type", None), m_open_id))
        print("  content      = %s" % json.dumps(payload, ensure_ascii=False))

        note = self._describe(mtype, payload, msg)
        print("  >>> %s" % note)
        self.log.append((mtype or "?", len(mentions), note, chat_type))

        # 机器人自己发的消息不要再回
        if self.echo and sender_type != "app" and chat_id:
            self._echo(chat_id, mtype)

    # ---- 各类消息的解读 ----
    def _describe(self, mtype, payload, msg):
        if mtype == "text":
            return "文本消息：%r" % payload.get("text")

        if mtype in RESOURCE_FIELD:
            field, rtype = RESOURCE_FIELD[mtype]
            key = payload.get(field)
            if not key:
                return "%s 消息，但内容里没有 %s —— 需要看原始 content" % (mtype, field)
            if self.download:
                return self._try_download(msg.message_id, key, rtype, payload)
            return "%s 消息，%s=%s（--no-download，未尝试下载）" % (mtype, field, key)

        if mtype == "post":
            return "富文本消息（post）—— 可能同时含文字和附件，需逐层看 content"
        if mtype == "merge_forward":
            return "合并转发的消息，机器人通常拿不到里面的文件"
        return "%s 消息（探针不解析这类）" % mtype

    # ---- 下载文件资源：这一步就是「文件下载权限」的实测 ----
    def _try_download(self, message_id, file_key, rtype, payload):
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(rtype)
            .build()
        )
        try:
            resp = self._lark_client().im.v1.message_resource.get(request)
        except Exception as exc:
            return "%s 资源下载：[X] 调用抛异常 %s: %s" % (rtype, type(exc).__name__, exc)

        if not resp.success():
            hint = ""
            blob = "%s" % (resp.msg or "")
            if "permission" in blob.lower() or "权限" in blob:
                hint = "  <- 像是权限问题：去开「获取与上传图片或文件资源」"
            return "%s 资源下载：[X] code=%s msg=%s%s" % (rtype, resp.code, resp.msg, hint)

        name = resp.file_name or payload.get("file_name") or ("%s.bin" % file_key)
        safe = "".join(c for c in name if c not in ':*/?"<>|\\') or ("%s.bin" % file_key)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        target = self.save_dir / ("%02d_%s" % (self.seq, safe))
        try:
            body = resp.file.read() if resp.file is not None else b""
            target.write_bytes(body)
        except Exception as exc:
            return "%s 下载成功但落盘失败：%s: %s" % (rtype, type(exc).__name__, exc)
        return ("%s 资源下载：[OK] %d 字节 <- **文件下载权限是通的**  已存 %s"
                % (rtype, len(body), target))

    # ---- 回一句：验发送权限 ----
    def _echo(self, chat_id, seen_type):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        content = json.dumps({"text": "[探针] 已收到一条 %s 消息" % seen_type},
                             ensure_ascii=False)
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(content)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        try:
            resp = self._lark_client().im.v1.message.create(request)
        except Exception as exc:
            print("  [回信] [X] 调用抛异常 %s: %s" % (type(exc).__name__, exc))
            return
        if resp.success():
            print("  [回信] [OK] 已发出 —— 发送权限 OK")
        else:
            print("  [回信] [X] code=%s msg=%s" % (resp.code, resp.msg))

    def _parse_content(self, raw):
        try:
            parsed = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return {"_未解析_": raw}
        return parsed if isinstance(parsed, dict) else {"_非字典_": parsed}

    # ---- 收尾小结：这才是这个脚本存在的意义 ----
    def summary(self) -> None:
        print("")
        print("=" * 68)
        print("小结：共收到 %d 条消息事件" % self.seq)
        if not self.seq:
            print("  一条都没收到。常见的三个原因：")
            print("  ① 事件订阅没配成【长连接】模式（后台改完要发布版本）")
            print("  ② 没订阅 im.message.receive_v1")
            print("  ③ 机器人没被拉进那个群 / 应用可见范围不含你")
            print("=" * 68)
            return

        for i, (mtype, n_mention, _note, chat_type) in enumerate(self.log, 1):
            at = "@%d处" % n_mention if n_mention else "无@"
            print("  %2d. %-10s %-6s %s" % (i, mtype, at, chat_type))

        has_file = any(t in RESOURCE_FIELD for t, _n, _s, _c in self.log)
        p2p_files = [t for t, _n, _s, c in self.log if c == "p2p" and t in RESOURCE_FIELD]
        group_files = [t for t, _n, _s, c in self.log if c == "group" and t in RESOURCE_FIELD]
        print("")
        print("怎么读这份日志（重点）：")
        if p2p_files:
            print("  · 私聊里收到了文件事件 -> 【私聊投递这条路是通的】")
            print("    演示就让组长私聊丢作业书，最稳，不依赖群 @ 行为。")
        if has_file:
            print("  · 收到了文件类事件 -> 机器人拿得到 file_key；")
            print("    再看上面有没有出现「文件下载权限是通的」这一行。")
        if not group_files and p2p_files:
            print("  · 群里没收到文件事件（若你确实发了⑤）-> 证实：机器人收不到没 @ 它的群文件。")
        print("  · 若⑥那条只打印了 text 而没有 file -> 就是「飞书把它拆成两条」的实证，")
        print("    那么群投递需要「读群内全部消息」的权限，或改走私聊投递。")
        print("=" * 68)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="飞书连通性 + 文件投递探针")
    parser.add_argument("--seconds", type=int, default=0,
                        help="到点自动退出；0 = 一直跑（Ctrl+C 停）")
    parser.add_argument("--echo", action="store_true",
                        help="收到消息后回一句，顺带验证发送权限")
    parser.add_argument("--no-download", dest="download", action="store_false",
                        help="不尝试下载文件资源")
    parser.add_argument("--quiet", action="store_true", help="关掉 SDK 的连接日志")
    parser.add_argument("--save-dir", default=None,
                        help="下载落盘目录（默认 data/probe/）")
    parser.set_defaults(download=True)
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        cfg.check_feishu()
    except ConfigError as exc:
        print("[配置错误] %s" % exc, file=sys.stderr)
        print("-> 确认仓库根目录 .env 里 FEISHU_APP_ID / FEISHU_APP_SECRET 都填了",
              file=sys.stderr)
        return 2

    if not check_credentials(cfg):
        return 2

    save_dir = Path(args.save_dir) if args.save_dir else (REPO_ROOT / "data" / "probe")
    probe = MessageProbe(cfg, download=args.download, echo=args.echo, save_dir=save_dir)

    print("[2/3] 正在加载飞书 SDK（import lark_oapi 首次约 20 秒，属正常，别以为卡死）")
    import lark_oapi as lark

    level = lark.LogLevel.WARNING if args.quiet else lark.LogLevel.INFO
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(probe.on_message)
        .build()
    )
    ws_client = lark.ws.Client(
        cfg.feishu_app_id,
        cfg.feishu_app_secret,
        event_handler=handler,
        log_level=level,
    )

    print("[3/3] 建立长连接（不需要公网、不占端口）")
    _print_steps()

    if args.seconds > 0:
        def _stop() -> None:
            probe.summary()
            sys.stdout.flush()
            os._exit(0)

        threading.Timer(args.seconds, _stop).start()
        print("（%d 秒后自动退出并打印小结；想提前停就 Ctrl+C）" % args.seconds)

    try:
        ws_client.start()          # 阻塞
    except KeyboardInterrupt:
        pass
    finally:
        probe.summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())