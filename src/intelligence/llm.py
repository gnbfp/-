"""LLM 客户端 —— M1 / M2 / M3 三处调用点**共用**的唯一出口。

依据：docs/ARCHITECTURE.md §5（统一契约）、requirements.md §1(B2) / §5(B8)、D-16。

三处都遵守同一套契约，别在各自的模块里另写一份：
  1. 强制 JSON 输出（``response_format: json_object``）
  2. 出参过 schema 校验（由调用方传进来的 ``parse`` 负责）
  3. 校验失败重试，**最多 2 次**（``max_retries=2`` ⇒ 最多 3 次请求）
  4. 仍失败 → ``LLMError`` 抛出，由调用方**降级不猜**（M1/M3 报错给用户）

本模块只负责"要一次合法 JSON"；它**不**判定业务对错（覆盖率高不高、
评分点是否可拆）——那属于 ``coverage.py`` / ``decompose.py``（B8）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

import httpx

__all__ = ["LLMError", "LLMOutputError", "LLMClient", "as_number"]

T = TypeVar("T")


def as_number(value):
    """LLM 偶尔把数字写成字符串；只做这一种规范化，不猜别的。"""
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


class LLMError(RuntimeError):
    """请求失败，或重试用尽仍未拿到合法输出。"""


class LLMOutputError(LLMError):
    """拿到了响应，但不符合调用方的 schema —— 触发重试的那一类错误。"""


@dataclass
class LLMClient:
    """OpenAI 兼容的 ``/chat/completions`` 客户端（DeepSeek / GLM 同构）。"""

    api_key: str
    base_url: str
    model: str
    timeout: float = 60.0
    # 测试注入用；为 None 时按 timeout 新建一个 httpx.Client
    http: httpx.Client | None = field(default=None, repr=False)

    @classmethod
    def from_config(cls, cfg) -> "LLMClient":
        cfg.check_llm()
        return cls(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url, model=cfg.llm_model)

    def __repr__(self) -> str:                 # 密钥绝不进日志 / traceback
        return f"LLMClient(model={self.model!r}, base_url={self.base_url!r})"

    __str__ = __repr__

    def chat_json(
        self,
        system: str,
        user: str,
        parse: Callable[[dict], T],
        *,
        temperature: float = 0.2,
        max_retries: int = 2,
    ) -> T:
        """要一次合法 JSON 并交给 ``parse`` 转成领域对象。

        ``parse`` 抛 ``LLMOutputError``（或 ValueError）即视为校验失败，
        把失败原因作为反馈追加进对话后重试 —— 而不是把坏数据往下游传。
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        reason = ""
        for attempt in range(max_retries + 1):
            content: str | None = None
            try:
                content = self._complete(messages, temperature)
                return parse(json.loads(content))
            except LLMOutputError as exc:
                reason = str(exc)
            except json.JSONDecodeError as exc:
                reason = f"不是合法 JSON：{exc}"
            except ValueError as exc:          # parse 内部抛的普通校验错也要重试
                reason = str(exc)
            except LLMError as exc:
                reason = str(exc)
            if attempt < max_retries:
                if content is not None:
                    messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": f"上一次输出不合要求：{reason}。请只输出修正后的 JSON，不要解释。",
                    }
                )
                time.sleep(0.5 * (attempt + 1))
        raise LLMError(f"LLM 连续 {max_retries + 1} 次未通过校验或请求失败：{reason}")

    # ---------- 底层 HTTP ----------

    def _client(self) -> httpx.Client:
        return self.http or httpx.Client(timeout=self.timeout)

    def _complete(self, messages: list[dict], temperature: float) -> str:
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._client().post(
                url, json=payload, headers={"Authorization": f"Bearer {self.api_key}"}
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败：{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise LLMError(f"LLM HTTP {response.status_code}：{response.text[:200]}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"LLM 返回结构异常：{response.text[:200]}") from exc
        # ``content`` 可能是 ``null``（上游偶发）：以前会原样返回 None，然后 ``json.loads(None)``
        # 抛 TypeError —— 而下面 ``chat_json`` 的四个 except 里**没有 TypeError**，
        # 于是"契约内的自动重试"当场失效：实测只发 1 次请求就整条链路报错（F1，2026-09-16）。
        # 归到 LLMError 才会走既有的重试路径（最多 3 次请求，仍失败才降级不猜）。
        if not isinstance(content, str) or not content.strip():
            raise LLMError(
                f"LLM 返回的 content 不是非空字符串（得到 {type(content).__name__}）："
                f"{response.text[:200]}"
            )
        return content
