"""公共配置读取工具。

所有脚本共用：读取 .env、规范化上游地址、生成候选 URL。
刻意保持零第三方依赖，安装 LiteLLM 之前也能先跑模型探测。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
CONFIG_FILE = ROOT / "config.yaml"

# 完整的 Anthropic 候选谓词，用于剥离用户误填的路径尾巴
_PATH_TAILS = ("/v1/messages", "/messages", "/v1/chat/completions", "/chat/completions", "/v1", "/")


# 未填 UPSTREAM_API_KEY 时的令牌回退来源，按优先级排序。
# 覆盖常见写法：Anthropic 官方 SDK 的两个标准变量名，以及本项目自己的变量名。
_TOKEN_ENV_FALLBACKS = (
    "UPSTREAM_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)


def ensure_env() -> dict:
    """读取 .env 并返回配置字典。系统环境变量优先级高于文件，便于临时覆盖。"""
    if not ENV_FILE.exists():
        raise FileNotFoundError(
            f"未找到配置文件 {ENV_FILE}\n"
            f"请复制一份 env.example 改名为 .env，并填入 UPSTREAM_BASE_URL 与 UPSTREAM_API_KEY。"
        )
    text = ENV_FILE.read_text(encoding="utf-8-sig")
    data: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key:
            data[key] = value
    for key in list(data.keys()):
        override = os.environ.get(key, "").strip()
        if override:
            data[key] = override
    return data


def resolve_token(env: dict) -> tuple[str, str]:
    """确定上游令牌及其来源。

    返回 (令牌, 来源说明)。令牌可能来自 .env 文件，也可能来自系统环境变量，
    后者方便用户沿用 Anthropic 官方 SDK 的习惯写法（ANTHROPIC_AUTH_TOKEN 等）。
    """
    for name in _TOKEN_ENV_FALLBACKS:
        # 系统环境变量优先，其次才是 .env 文件里的同名项
        from_env_var = os.environ.get(name, "").strip()
        if from_env_var and not is_placeholder(from_env_var):
            return from_env_var, f"环境变量 {name}"
    file_value = (env.get("UPSTREAM_API_KEY") or "").strip()
    if file_value and not is_placeholder(file_value):
        return file_value, ".env 中的 UPSTREAM_API_KEY"
    return "", "未找到"


def is_placeholder(value: str) -> bool:
    """识别未填写的占位值，避免把样例文字当成真实令牌发出去。"""
    v = value.strip()
    markers = ("填写", "your-", "请在这里", "xxxx", "xxx", "sk-ant-xxxx", "TODO", "CHANGE_ME")
    return (not v) or any(m.lower() in v.lower() for m in markers)


def normalize_base(url: str, keep_v1: bool = False) -> str:
    """把用户填写的地址统一收敛为不带请求路径的形式。

    例如 https://a.com/v1/messages、https://a.com/v1 都会收敛为 https://a.com。
    keep_v1=True 时保留一个 /v1 尾巴：OpenAI 原生模式下 LiteLLM 直接在
    api_base 后拼 /chat/completions（实测 1.106.4），绝大多数 OpenAI 兼容
    网关只认 /v1/chat/completions，所以 base 必须以 /v1 结尾。
    """
    base = (url or "").strip().rstrip("/")
    changed = True
    while changed:
        changed = False
        for tail in _PATH_TAILS:
            if base.endswith(tail) and len(base) > len(tail):
                if keep_v1 and tail == "/v1":
                    break
                base = base[: -len(tail)].rstrip("/")
                changed = True
                break
    return base


def message_urls(base: str) -> list[str]:
    """Anthropic 消息接口的候选地址。

    不同中转的路径约定不一致，这里按命中概率排序逐个试探。
    """
    b = normalize_base(base)
    return [f"{b}/v1/messages", f"{b}/messages"]


def models_urls(base: str) -> list[str]:
    """模型列表接口的候选地址。"""
    b = normalize_base(base)
    return [f"{b}/v1/models", f"{b}/models"]


def as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def openai_native_base(env: dict) -> str:
    """OpenAI 原生模式的 api_base：优先显式覆盖键，其次在归一化结果上补 /v1。

    UPSTREAM_BASE_URL_OPENAI 供网关把 OpenAI 路由挂在非标准路径时手填
    （如 https://gw.example.com/api/openai/v1），填了就原样使用（仅去尾斜杠）。
    """
    explicit = (env.get("UPSTREAM_BASE_URL_OPENAI") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    base = normalize_base(env.get("UPSTREAM_BASE_URL", ""))
    return f"{base}/v1" if base else base


def upstream_models(env: dict) -> list[str]:
    raw = env.get("UPSTREAM_MODELS", "")
    # 兼容部分用户写成 anthropic/xxx 的形式
    return [m.split("/", 1)[-1] if m.startswith("anthropic/") else m for m in split_list(raw)]


def proxy_model_pairs(env: dict) -> list[tuple[str, str]]:
    """返回 [(对外别名, 上游真实模型名)]。别名未配置时与真实名保持一致。"""
    real = upstream_models(env)
    aliases = split_list(env.get("PROXY_MODEL_ALIASES"))
    if not aliases:
        return [(m, m) for m in real]
    if len(aliases) != len(real):
        raise ValueError(
            f"PROXY_MODEL_ALIASES 的数量({len(aliases)})必须与 UPSTREAM_MODELS({len(real)})一致，"
            f"否则无法确定映射关系。"
        )
    return list(zip(aliases, real))
