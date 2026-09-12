"""M0 飞书 I/O —— **全项目唯一 import lark_oapi 的地方**（方案 §2）。

依据：M0 网关方案 §2 / §6 / §7、`tools/probe_feishu.py`（字段解析、发消息、下载 file_key
的用法都已实测过，这里直接沿用）。

连接层只干三件笨事：收事件、发消息、下载资源。任何业务判断都不许写进本文件 ——
那些在 ``router.py`` / ``register.py``（纯函数，可离线全量单测）。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.gateway.events import Reply

__all__ = ["FeishuError", "FeishuClient"]

_BAD_NAME_CHARS = ':*/?"<>|\\'


class FeishuError(RuntimeError):
    """飞书接口调用失败（发消息 / 下载资源）。"""


class FeishuClient:
    """飞书 I/O 适配器。同时充当 app 层的 sender 与 downloader。"""

    def __init__(self, config, log_level=None) -> None:
        self.config = config
        self.log_level = log_level
        self._sdk = None
        self._api_client = None

    # ---------- SDK 懒加载（import lark_oapi 首次约 20 秒，属正常）----------

    def _lark(self):
        if self._sdk is None:
            import lark_oapi as lark

            self._sdk = lark
        return self._sdk

    def _level(self, lark):
        """默认 INFO（与 tools/probe_feishu.py 一致）：连接过程可见；
        ``main(--quiet)`` 把它降到 WARNING，手工验收时屏掉 SDK 唱片。
        """
        return self.log_level if self.log_level is not None else lark.LogLevel.INFO

    def _api(self):
        if self._api_client is None:
            lark = self._lark()
            self._api_client = (
                lark.Client.builder()
                .app_id(self.config.feishu_app_id)
                .app_secret(self.config.feishu_app_secret)
                .log_level(self._level(lark))
                .build()
            )
        return self._api_client

    # ---------- 发消息 ----------

    def send(self, message: Reply) -> bool:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(message.chat_id)
            .msg_type("text")
            .content(json.dumps({"text": message.text}, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response = self._api().im.v1.message.create(request)
        if not response.success():
            raise FeishuError(f"发消息失败 code={response.code} msg={response.msg}")
        return True

    # ---------- 下载资源 ----------

    def download(self, pending: dict, target_dir) -> Path:
        """把 ``state.pending_file`` 里的 file_key 落成字节流。

        下载类型由缓存里的 ``resource_type`` 决定（image 走 image_key）—— 见 §7.5 的
        「文件下载权限」实测；权限已开齐（AGENTS.md §4）。
        """
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = (
            GetMessageResourceRequest.builder()
            .message_id(pending.get("message_id", ""))
            .file_key(pending.get("file_key", ""))
            .type(pending.get("resource_type", "file"))
            .build()
        )
        response = self._api().im.v1.message_resource.get(request)
        if not response.success():
            raise FeishuError(f"下载失败 code={response.code} msg={response.msg}")

        name = _safe_name(pending.get("file_name") or f"{pending.get('file_key', 'file')}.bin")
        target = Path(target_dir) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        body = response.file.read() if response.file is not None else b""
        target.write_bytes(body)
        return target

    # ---------- 长连接（阻塞）----------

    def start(self, on_event) -> None:
        lark = self._lark()
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_event)
            .build()
        )
        client = lark.ws.Client(
            self.config.feishu_app_id,
            self.config.feishu_app_secret,
            event_handler=handler,
            log_level=self._level(lark),
        )
        client.start()


def _safe_name(name: str) -> str:
    cleaned = "".join(ch for ch in str(name) if ch not in _BAD_NAME_CHARS).strip()
    return cleaned or "upload.bin"