"""配置 —— 环境变量与 .env。

依据 docs/ARCHITECTURE.md §12.2。密钥只从环境读；永远不写进代码、不进仓库。

`.env` 必须是 NAME=VALUE 形式。出现没有变量名的裸值会被直接拒掉 ——
否则密钥会被悄悄忽略，程序拿一个空 key 去调 API，报错还找不到原因。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

ENV_FEISHU_APP_ID = "FEISHU_APP_ID"
ENV_FEISHU_APP_SECRET = "FEISHU_APP_SECRET"
ENV_LLM_API_KEY = "LLM_API_KEY"
ENV_LLM_BASE_URL = "LLM_BASE_URL"
ENV_LLM_MODEL = "LLM_MODEL"
ENV_DATA_DIR = "DATA_DIR"

_LLM_REQUIRED = (ENV_LLM_API_KEY, ENV_LLM_BASE_URL, ENV_LLM_MODEL)
_FEISHU_REQUIRED = (ENV_FEISHU_APP_ID, ENV_FEISHU_APP_SECRET)


class ConfigError(RuntimeError):
    """缺少必需配置，或 .env 格式不合法。"""


def parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：只认 NAME=VALUE，支持 # 注释。刻意不留裸值后门。"""
    path = Path(path)
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(
                f"{path.name} 第 {lineno} 行没有变量名。"
                f"密钥请写成 {ENV_LLM_API_KEY}=<你的 key> 的形式，不要只写裸值。"
            )
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def _mask(value: str) -> str:
    return "''" if not value else f"'<已设置,{len(value)}位>'"


def _attr_of(env_name: str) -> str:
    return env_name.lower()


@dataclass(frozen=True, repr=False)
class Config:
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    data_dir: Path = REPO_ROOT / "data"

    def __repr__(self) -> str:
        # 手写 repr：密钥绝不能经 logging / traceback 漏出去
        return (
            "Config("
            f"feishu_app_id={_mask(self.feishu_app_id)}, "
            f"feishu_app_secret={_mask(self.feishu_app_secret)}, "
            f"llm_api_key={_mask(self.llm_api_key)}, "
            f"llm_base_url={self.llm_base_url!r}, "
            f"llm_model={self.llm_model!r}, "
            f"data_dir={str(self.data_dir)!r})"
        )

    __str__ = __repr__

    def _missing(self, names: tuple[str, ...]) -> list[str]:
        return [n for n in names if not getattr(self, _attr_of(n))]

    def check_llm(self) -> None:
        missing = self._missing(_LLM_REQUIRED)
        if missing:
            raise ConfigError(f"缺少 LLM 配置：{missing}（填进 .env）")

    def check_feishu(self) -> None:
        missing = self._missing(_FEISHU_REQUIRED)
        if missing:
            raise ConfigError(f"缺少飞书配置：{missing}（填进 .env）")


def load_config(env_file: Path | None = None, *, data_dir: Path | None = None) -> Config:
    """读 .env + 环境变量（环境变量优先）。不在此处抛错，缺失项由 check_* 报。"""
    file_values = parse_env_file(env_file or DEFAULT_ENV_FILE)

    def get(name: str, default: str = "") -> str:
        return os.environ.get(name) or file_values.get(name) or default

    raw_dir = Path(data_dir) if data_dir is not None else Path(get(ENV_DATA_DIR, "data"))
    if not raw_dir.is_absolute():
        raw_dir = REPO_ROOT / raw_dir        # 落盘唯一位置 = 仓库根 data/（D-30）

    return Config(
        feishu_app_id=get(ENV_FEISHU_APP_ID),
        feishu_app_secret=get(ENV_FEISHU_APP_SECRET),
        llm_api_key=get(ENV_LLM_API_KEY),
        llm_base_url=get(ENV_LLM_BASE_URL),
        llm_model=get(ENV_LLM_MODEL),
        data_dir=raw_dir,
    )
