#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测 Anthropic 协议上游是否可用，以及是否支持工具调用与流式。

接任何新上游之前先跑这个脚本，把「端点路径 / 模型名 / 工具支持」三件事一次确认完，
避免配好代理才发现模型名写错或端点路径不对。

用法：
    python probe_upstream.py --base https://host:port --key sk-xxx --model glm-5.3
    python probe_upstream.py --base https://host:port --key sk-xxx --insecure

只用标准库，不依赖 httpx / requests。
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request

# 各家中转的路径约定不一致，按命中概率排序试探
MESSAGE_PATHS = ("/v1/messages", "/messages")
MODELS_PATHS = ("/v1/models", "/models")
PATH_TAILS = ("/v1/messages", "/messages", "/v1/chat/completions", "/chat/completions", "/v1", "/")


def normalize_base(url: str) -> str:
    """把用户填写的地址收敛为不带请求路径的形式。"""
    base = (url or "").strip().rstrip("/")
    changed = True
    while changed:
        changed = False
        for tail in PATH_TAILS:
            if base.endswith(tail) and len(base) > len(tail):
                base = base[: -len(tail)].rstrip("/")
                changed = True
                break
    return base


def _opener(insecure: bool, cafile: str | None = None) -> urllib.request.OpenerDirector:
    """默认校验证书链与主机名；cafile 追加私有 CA；insecure 才关闭校验。"""
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # 显式不走系统代理，避免本机 HTTP_PROXY 劫持回环或内网地址
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx),
    )


def post(opener, url: str, key: str, body: dict, timeout: int) -> tuple[int, str]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"x-api-key": key, "authorization": "Bearer " + key,
                 "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


def get(opener, url: str, key: str, timeout: int) -> tuple[int, str]:
    req = urllib.request.Request(
        url, method="GET",
        headers={"x-api-key": key, "authorization": "Bearer " + key,
                 "anthropic-version": "2023-06-01"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:2000]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


TOOLS = [{
    "name": "read_file",
    "description": "Read a local file from disk and return its content.",
    "input_schema": {"type": "object",
                     "properties": {"path": {"type": "string", "description": "absolute path"}},
                     "required": ["path"]},
}]


def probe_messages(opener, base: str, key: str, model: str, timeout: int) -> tuple[str, str]:
    """依次试探消息端点，返回 (可用端点, 工具支持结论)。"""
    good = ""
    for path in MESSAGE_PATHS:
        status, text = post(opener, base + path, key, {
            "model": model, "max_tokens": 512, "tools": TOOLS,
            "messages": [{"role": "user", "content": "用 read_file 读取 C:/tmp/demo.txt"}],
        }, timeout)
        if status == 200:
            good = path
            try:
                blocks = json.loads(text).get("content") or []
            except json.JSONDecodeError:
                return path, "响应不是合法 JSON"
            kinds = [b.get("type") for b in blocks]
            called = [b.get("name") for b in blocks if b.get("type") == "tool_use"]
            if called:
                return path, "支持工具调用（模型发起 %s）【单次结果，仅供初筛；下结论需重复 5 次看命中率】" % ", ".join(map(str, called))
            return path, "端点可用但模型未发起工具调用（blocks=%s）【单次结果，仅供初筛；模型采样有随机性，未调≠不支持，重复 5 次再判定】" % kinds
        print("  %s -> HTTP %s %s" % (path, status, text[:160]))
    return good, "无可用消息端点"


def probe_models(opener, base: str, key: str, timeout: int) -> list[str]:
    for path in MODELS_PATHS:
        status, text = get(opener, base + path, key, timeout)
        if status == 200:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            items = data.get("data") if isinstance(data, dict) else data
            names = []
            for it in items or []:
                if isinstance(it, dict):
                    names.append(str(it.get("id") or it.get("name") or it))
            if names:
                print("  模型列表(%s): %s" % (path, ", ".join(names[:20])))
                return names
        else:
            print("  %s -> HTTP %s" % (path, status))
    print("  模型列表接口不可用（不影响接入，模型名按文档填即可）")
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="探测 Anthropic 协议上游")
    ap.add_argument("--base", required=True, help="上游地址，可带路径，脚本会自动归一化")
    ap.add_argument("--key", required=True, help="上游令牌")
    ap.add_argument("--model", default="", help="上游模型名；不填则从模型列表取第一个")
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 校验（内网自签证书时用）")
    ap.add_argument("--ca", default=None, metavar="PEM", help="自定义 CA 证书文件（私有 CA 网关）")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    base = normalize_base(args.base)
    print("归一化后的 base: %s" % base)
    opener = _opener(args.insecure, args.ca)

    print("\n[1/3] 模型列表")
    names = probe_models(opener, base, args.key, args.timeout)

    model = args.model or (names[0] if names else "")
    if not model:
        print("\n未确定模型名，请用 --model 指定后重跑。")
        return 1
    print("\n[2/3] 消息端点 + 工具支持（model=%s）" % model)
    path, verdict = probe_messages(opener, base, args.key, model, args.timeout)

    print("\n[3/3] 结论")
    if not path:
        print("  ✗ 消息端点不可用，检查 base 与 key")
        return 1
    print("  ✓ 端点: %s%s" % (base, path))
    print("  %s 工具: %s" % ("✓" if "支持工具调用" in verdict else "!", verdict))
    print("\n下一步：把 base 填 UPSTREAM_BASE_URL、%s 填 UPSTREAM_MODELS，然后生成 config.yaml。" % model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
