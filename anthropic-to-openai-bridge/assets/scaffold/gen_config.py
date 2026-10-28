"""根据 .env 生成 LiteLLM 的 config.yaml。

不直接手改 YAML 的原因：模型列表需要与上游地址、别名保持同步，脚本生成不会写错缩进。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from bridge_env import (
    CONFIG_FILE,
    as_bool,
    ensure_env,
    normalize_base,
    openai_native_base,
    proxy_model_pairs,
)


def _positive_int(env: dict, key: str, default: int) -> int:
    """读取正整数配置项。缺失用默认值，非法值直接报错。

    token 上限、超时这类参数写成 0 或负数时，下游只会表现为"上游莫名 400"，
    在配置阶段拦住能省掉一轮排查。
    """
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key}={raw!r} 不是整数，请填正整数。")
    if value < 1:
        raise ValueError(f"{key}={value} 必须是正整数（大于 0）。")
    return value


def _parse_caps(raw: str | None) -> dict[str, int]:
    """解析 MAX_OUTPUT_TOKEN_CAPS：{"模型名": 上诉上限}。"""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"MAX_OUTPUT_TOKEN_CAPS 不是合法 JSON: {exc}")
    if not isinstance(data, dict):
        raise ValueError('MAX_OUTPUT_TOKEN_CAPS 必须是 JSON 对象，形如 {"模型名": 131072}')
    caps: dict[str, int] = {}
    for key, value in data.items():
        try:
            cap = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"MAX_OUTPUT_TOKEN_CAPS 中 {key} 的上限不是整数。")
        if cap < 1:
            raise ValueError(f"MAX_OUTPUT_TOKEN_CAPS 中 {key} 的上限 {cap} 必须是正整数。")
        caps[str(key)] = cap
    return caps


def build_yaml(env: dict) -> str:
    pairs = proxy_model_pairs(env)
    if not pairs:
        raise ValueError("UPSTREAM_MODELS 为空，至少填写一个上游模型名。")

    is_openai_native = as_bool(env.get("UPSTREAM_IS_OPENAI_NATIVE"), False)
    provider = "openai" if is_openai_native else "anthropic"

    if is_openai_native:
        # LiteLLM openai provider 直接在 api_base 后拼 /chat/completions（实测 1.106.4），
        # 绝大多数 OpenAI 兼容网关只认 /v1/chat/completions，base 必须带 /v1 尾巴。
        # 网关把 OpenAI 路由挂在非标准路径时，用 UPSTREAM_BASE_URL_OPENAI 显式覆盖。
        base_url = openai_native_base(env)
    else:
        base_url = normalize_base(env.get("UPSTREAM_BASE_URL", ""))
    if not base_url:
        raise ValueError("UPSTREAM_BASE_URL 未填写。")

    timeout = _positive_int(env, "REQUEST_TIMEOUT", 600)
    pad = max(int(timeout * 1.1), timeout + 30)

    extra_headers_raw = (env.get("EXTRA_HEADERS") or "").strip()
    extra_headers = {}
    if extra_headers_raw:
        try:
            extra_headers = json.loads(extra_headers_raw)
            if not isinstance(extra_headers, dict):
                raise ValueError
        except Exception:
            raise ValueError("EXTRA_HEADERS 不是合法的 JSON 对象，例如 {\"X-Foo\":\"bar\"}")

    thinking_enabled = as_bool(env.get("THINKING_ENABLED"), False)
    thinking_budget = _positive_int(env, "THINKING_BUDGET_TOKENS", 2000)
    allow_any = as_bool(env.get("ALLOW_ANY_MODEL"), True)
    guard_enabled = as_bool(env.get("GUARD_ENABLED"), True)
    min_tokens = _positive_int(env, "MIN_OUTPUT_TOKENS", 1600)
    output_caps = _parse_caps(env.get("MAX_OUTPUT_TOKEN_CAPS"))

    if thinking_enabled and provider == "anthropic" and min_tokens <= thinking_budget:
        # Anthropic 要求 max_tokens 严格大于思考预算 budget_tokens，否则整轮 400。
        # guard 会把偏小的 max_tokens 兜到 MIN_OUTPUT_TOKENS，若兜底值本身不大于
        # 预算，客户端不传 max_tokens 时每轮必失败。
        raise ValueError(
            f"MIN_OUTPUT_TOKENS({min_tokens}) 必须大于 THINKING_BUDGET_TOKENS({thinking_budget})："
            f"Anthropic 要求 max_tokens 严格大于思考预算，否则上游 400。"
            f"请把 MIN_OUTPUT_TOKENS 提到 {thinking_budget + 1} 以上。"
        )

    # 输出上限与下限/思考预算的冲突：guard 的执行顺序是「先兜下限、再夹上限」，
    # 上限小于下限时最终值会被夹回上限之下，兜底等于白做，思考块仍会把正文挤空。
    # 单条判断即可覆盖：min_tokens > thinking_budget 已由上一条保证，因此
    # cap >= min_tokens 时必然也大于预算，无需再单独判 cap 与预算的关系。
    for model, cap in output_caps.items():
        if cap < min_tokens:
            extra = ""
            if thinking_enabled and provider == "anthropic":
                extra = f"，且上限必须大于思考预算 {thinking_budget}，否则上游必 400"
            raise ValueError(
                f"MAX_OUTPUT_TOKEN_CAPS 中 {model} 的上限 {cap} 小于 MIN_OUTPUT_TOKENS({min_tokens})："
                f"max_tokens 会被先兜到 {min_tokens} 再夹回 {cap}，兜底失效{extra}。"
                f"请把该模型上限提到 {min_tokens} 以上，或下调 MIN_OUTPUT_TOKENS。"
            )

    lines = [
        "# 本文件由 gen_config.py 自动生成，不要手工修改。改 .env 后重新运行 start_proxy.py。",
        "model_list:",
    ]

    def emit_entry(alias: str, real: str) -> None:
        lines.append(f"  - model_name: {alias}")
        lines.extend([
            "    litellm_params:",
            f"      model: {provider}/{real}",
            f'      api_base: "{base_url}"',
            '      api_key: "os.environ/UPSTREAM_API_KEY"',
            f"      timeout: {pad}",
            f"      stream_timeout: {pad}",
            "      max_retries: 2",
        ])
        if extra_headers:
            lines.append(
                f"      extra_headers: {json.dumps(extra_headers, ensure_ascii=False)}"
            )
        if thinking_enabled and provider == "anthropic":
            lines.extend([
                "      thinking:",
                '        type: "enabled"',
                f"        budget_tokens: {thinking_budget}",
            ])

    for alias, real in pairs:
        emit_entry(alias, real)

    # 兜底通配：客户端请求的模型名不在列表里时也能转发，避免直接报 400
    if allow_any:
        emit_entry('"*"', "*")

    lines += ["", "litellm_settings:", "  telemetry: false", "  drop_params: true"]

    # 请求纠偏：修正模型虚构工具、挖 photocache 输出截断等经转发后暴露的行为偏差
    if guard_enabled:
        lines.append("  callbacks:")
        lines.append("    - bridge_guard.proxy_handler_instance")
    else:
        print("[警告] GUARD_ENABLED=false：compat_patch 的 422 修复依赖 bridge_guard")
        print("       模块导入，关掉后严格网关（如 deepseek 系列带 tools）可能重新 422。")

    lines += [
        f"  request_timeout: {pad}",
        "",
        "general_settings:",
        '  master_key: "os.environ/PROXY_MASTER_KEY"',
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        env = ensure_env()
        content = build_yaml(env)
    except Exception as exc:  # noqa: BLE001
        print(f"[配置错误] {exc}")
        return 1

    Path(CONFIG_FILE).write_text(content, encoding="utf-8")
    pairs = proxy_model_pairs(env)
    print(f"[已生成] {CONFIG_FILE}")
    for alias, real in pairs:
        arrow = "" if alias == real else f"  ->  上游 {real}"
        print(f"         对外模型名: {alias}{arrow}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
