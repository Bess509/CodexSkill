"""探测上游真实可用的模型名与可用路径。

用法：
    .venv/Scripts/python.exe detect_models.py
    或直接双击 detect.bat

探测顺序：
1. GET /v1/models 看能否直接列出；
2. 不行则用 .env 里配置的模型名逐个发送极短请求，按响应判定是否存在；
3. 顺带确认网关是否同时提供 OpenAI 原生路由（若可用则无需本地代理）。
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from http.client import HTTPException
from pathlib import Path

from bridge_env import (
    ensure_env,
    is_placeholder,
    message_urls,
    models_urls,
    normalize_base,
    resolve_token,
    upstream_models,
)

# 仅在 .env 未配置模型名时的兜底候选
FALLBACK_MODELS = [
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-1-20250805",
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "glm-4.6",
    "glm-4.5",
]

# 记录遇到过的网关自身过载，用于最终结论区分「配置错」与「上游暂时不可用」
_overload_hits: list[str] = []

_TLS_HINT_SHOWN = False


def build_ssl_context(insecure: bool = False, cafile: str | None = None) -> ssl.SSLContext:
    """构造 TLS 上下文：默认校验证书链与主机名。

    - 默认走系统信任库，证书过期、域名不符、自签名都会直接失败
    - cafile（--ca 或 .env 的 UPSTREAM_CA_BUNDLE）加载私有 CA，供自建中转使用
    - insecure（--insecure）才关闭校验，属临时排查手段
    """
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def build_opener(insecure: bool = False, cafile: str | None = None) -> urllib.request.OpenerDirector:
    """忽略系统代理变量，避免本地回环探测被 HTTP_PROXY 劫持。"""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=build_ssl_context(insecure, cafile)),
    )


_OPENER: urllib.request.OpenerDirector | None = None


def configure_tls(insecure: bool = False, cafile: str | None = None) -> None:
    """建立探测用 opener。必须在发起任何请求前调用一次。"""
    global _OPENER
    if cafile and not insecure and not Path(cafile).is_file():
        raise FileNotFoundError(f"CA 证书文件不存在: {cafile}")
    _OPENER = build_opener(insecure, cafile)


def _opener() -> urllib.request.OpenerDirector:
    global _OPENER
    if _OPENER is None:
        _OPENER = build_opener()
    return _OPENER


def _tls_hint() -> None:
    """证书失败时给一次明确指引，避免被当成"网关不可达"反复重试。"""
    global _TLS_HINT_SHOWN
    if _TLS_HINT_SHOWN:
        return
    _TLS_HINT_SHOWN = True
    print("\n[TLS] 证书校验失败。按下列顺序处理：")
    print("      1. 私有 CA：用 --ca <证书.pem>，或在 .env 里写 UPSTREAM_CA_BUNDLE=<证书.pem>")
    print("      2. 证书链不完整或已过期：修服务端证书，这是唯一长期正确的做法")
    print("      3. 仅临时排查：--insecure（关闭校验，不要用于生产）\n")


def request(url: str, api_key: str, method: str = "GET", body: dict | None = None,
            timeout: int = 20, auth_mode: str = "anthropic") -> tuple[int, str]:
    """发起一次探测请求。

    auth_mode:
        anthropic -> x-api-key + anthropic-version 请求头
        openai    -> Authorization: Bearer 请求头
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"content-type": "application/json", "accept": "application/json"}
    if auth_mode == "openai":
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, HTTPException, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
            _tls_hint()
        return -1, str(exc)


def looks_overloaded(text: str) -> bool:
    """识别网关自身过载或限流，这类响应容易被误判成配置错误。"""
    low = text.lower()
    return any(k in low for k in ("overload", "cpu", "too many requests", "busy"))


def error_summary(code: int, text: str) -> str:
    try:
        payload = json.loads(text)
        err = payload.get("error", payload)
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return str(msg)[:200]
    except json.JSONDecodeError:
        return text[:200]


def record_overload(reason: str) -> None:
    _overload_hits.append(reason)


def probe_models_endpoint(base: str, api_key: str) -> list[str] | None:
    """尝试直接列举模型。返回 None 表示该方式不可用。"""
    for url in models_urls(base):
        code, text = request(url, api_key, timeout=15)
        if code == 200:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            items = payload.get("data", payload) if isinstance(payload, dict) else payload
            names = []
            for item in items or []:
                name = item.get("id") or item.get("name") if isinstance(item, dict) else str(item)
                if name:
                    names.append(name)
            if names:
                print(f"[模型列表可用] {url}")
                return names
        elif code in (401, 403):
            if looks_overloaded(text):
                record_overload(url)
                print(f"[上游过载] {url} -> {error_summary(code, text)}")
                continue
            print(f"[鉴权失败] {url} -> {error_summary(code, text)}")
            print("            请确认 UPSTREAM_API_KEY 正确，且该令牌允许访问模型列表。")
            return []
        elif code == 404:
            print(f"[路径不存在] {url} -> 404")
        elif code > 0:
            print(f"[不可用] {url} -> HTTP {code} {error_summary(code, text)}")
        else:
            print(f"[连接失败] {url} -> {text[:120]}")
    return None


def probe_by_calling(base: str, api_key: str, models: list[str]) -> tuple[str, list[str]]:
    """逐个模型名发送极短请求，判断哪些真实存在。"""
    working_url = ""
    alive_models: list[str] = []
    for url in message_urls(base):
        print(f"\n[试探路径] {url}")
        path_alive = False
        found: list[str] = []
        for model in models:
            body = {
                "model": model,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            }
            code, text = request(url, api_key, method="POST", body=body, timeout=30)
            if code == 200:
                path_alive = True
                found.append(model)
                print(f"    可用   {model}")
            elif code == 404:
                print(f"    不存在 {model}")
            elif code == 400:
                # 路径已进入业务层（能被校验），说明路径本身是通的。
                # 部分网关对未知模型名返回 400 而非 404，报错文案里通常带模型名，
                # 不能据此中断——继续探测下一个候选模型。
                path_alive = True
                summary = error_summary(code, text)
                if looks_overloaded(text):
                    record_overload(url)
                    print(f"    上游过载 {summary}")
                else:
                    print(f"    参数/模型 {code}: {summary}")
            elif code in (401, 403):
                # 走到鉴权层说明路径本身是通的
                path_alive = True
                summary = error_summary(code, text)
                if looks_overloaded(text):
                    record_overload(url)
                    print(f"    上游过载 {summary}")
                else:
                    print(f"    鉴权/参数 {code}: {summary}")
                break
            elif code == -1:
                print(f"    连接失败 {text[:120]}")
                break
            else:
                print(f"    HTTP {code}: {error_summary(code, text)}")
        if path_alive:
            working_url, alive_models = url, found
            if found:
                break
    return working_url, alive_models


def probe_openai_native(base: str, api_key: str, models: list[str]) -> None:
    """确认网关是否同时提供 OpenAI 原生路由。可用则不必走本地代理。"""
    if not models:
        return
    url = normalize_base(base) + "/v1/chat/completions"
    body = {
        "model": models[0],
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }
    code, text = request(url, api_key, method="POST", body=body, timeout=30,
                         auth_mode="openai")
    print("\n[附带探测] 网关是否同时提供 OpenAI 原生路由")
    print(f"           {url}")
    if code == 200:
        print("           可用。此时可以直接用它，不必经过本地代理；")
        print("           若仍想统一走代理，把 .env 的 UPSTREAM_IS_OPENAI_NATIVE 改为 true。")
    elif looks_overloaded(text):
        record_overload(url)
        print(f"           上游过载，本次无法判定: {error_summary(code, text)}")
    elif code in (401, 403):
        print(f"           该令牌无权使用 OpenAI 路由: {error_summary(code, text)}")
        print("           维持 Anthropic 协议路线即可。")
    elif code == 404:
        print("           网关不提供 OpenAI 路由，只能走 Anthropic 协议。")
    else:
        print(f"           HTTP {code}: {error_summary(code, text)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="探测上游可用模型与可用路径")
    ap.add_argument("--insecure", action="store_true",
                    help="关闭 TLS 证书校验（仅限临时排查，不要用于生产）")
    ap.add_argument("--ca", default=None, metavar="PEM",
                    help="自定义 CA 证书文件，用于私有 CA 签发的网关")
    args = ap.parse_args(argv)

    try:
        env = ensure_env()
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 1

    # TLS 必须先于任何请求确定：默认严格校验，私有 CA 用 --ca 或 UPSTREAM_CA_BUNDLE
    cafile = args.ca or (env.get("UPSTREAM_CA_BUNDLE") or "").strip() or None
    try:
        configure_tls(args.insecure, cafile)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 1
    if args.insecure:
        print("[警告] 已关闭 TLS 证书校验（--insecure）。本次探测只能证明网络可达，")
        print("       无法证明对面是可信网关。排查完请去掉该参数。")
    elif cafile:
        print(f"TLS: 校验证书链与主机名，信任库附加 {cafile}")
    else:
        print("TLS: 校验证书链与主机名（系统信任库）")

    base = normalize_base(env.get("UPSTREAM_BASE_URL", ""))
    api_key, key_source = resolve_token(env)
    if not base or is_placeholder(base):
        print("[错误] 请先在 .env 中填写 UPSTREAM_BASE_URL（当前为空或占位值）。")
        return 1
    if not api_key:
        print("[错误] 未找到上游令牌。请在 .env 中填写 UPSTREAM_API_KEY，")
        print("      或设置环境变量 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY。")
        return 1

    configured = upstream_models(env)
    models = configured or FALLBACK_MODELS

    print(f"上游地址: {base}")
    print(f"令牌来源: {key_source}")
    print(f"待验证模型: {', '.join(models)}\n")

    names = probe_models_endpoint(base, api_key)
    if names is None:
        print("\n模型列表接口取不到，改用逐个调用的方式探测。")
        url, names = probe_by_calling(base, api_key, models)
        if not url:
            print("\n[结论] 所有候选路径均无法连通。请检查：")
            print("  1. 地址与端口是否正确，本机能否访问")
            print("  2. 令牌是否仍有效")
            probe_openai_native(base, api_key, models)
            return 2
        print(f"\n[可用路径] {url}")

    probe_openai_native(base, api_key, models)

    if names:
        print("\n可用模型（可复制到 .env 的 UPSTREAM_MODELS）：")
        print("UPSTREAM_MODELS=" + ",".join(names))
    else:
        print("\n[结论] 未自动确认到可用模型。")
        if _overload_hits:
            print("       注意：本次探测遇到了网关自身过载，这不代表配置有误，")
            print("       稍等几分钟让服务端负载降下来后重跑本脚本即可。")
        else:
            print("       请核对模型名是否与服务商文档一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
