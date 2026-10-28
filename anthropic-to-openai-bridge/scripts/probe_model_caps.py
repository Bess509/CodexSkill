#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测单个模型在 OpenAI 兼容端点上的真实能力，用于填写 WorkBuddy 的 models.json。

六项都靠实测，不靠模型介绍页：
  1. 文本      —— 基本可用性
  2. 工具调用  —— 决定 supportsToolCall（默认重复 3 次看命中率）
  3. 视觉      —— 用三张纯色图判定，决定 supportsImages
  4. 输出上限  —— 用超大 max_tokens 触发校验，从报错里读上限，决定 maxOutputTokens
  5. 输入下限  —— 发 400k 字符看是否通过，决定 maxInputTokens（实测拿到的只是下限）
  6. 推理      —— 看响应里有无 reasoning_content 字段，决定 supportsReasoning

只用标准库，不装依赖。

用法：
    python probe_model_caps.py --base http://127.0.0.1:4000/v1 --key sk-xxx \
        --model glm-5.3 --model MiniMax-M3 --insecure
"""
from __future__ import annotations

import argparse
import base64
import json
import ssl
import struct
import sys
import urllib.error
import urllib.request
import zlib


_CAFILE: str | None = None


def _opener(insecure: bool) -> urllib.request.OpenerDirector:
    """显式不走系统代理：对 127.0.0.1 的探测会被本机 HTTP_PROXY 劫持，
    这是 diagnostics.md 对自检脚本的硬性要求。

    默认校验证书链与主机名；_CAFILE（--ca）追加私有 CA；insecure 才关闭校验。
    """
    ctx = ssl.create_default_context(cafile=_CAFILE) if _CAFILE else ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx),
    )


def post(url: str, key: str, body: dict, insecure: bool, timeout: int = 300):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with _opener(insecure).open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def png_b64(rgb, size: int = 64) -> str:
    """生成 size×size 纯色 PNG 的 base64，避免依赖 PIL。"""
    r, g, b = rgb
    raw = b"".join(b"\x00" + bytes([r, g, b]) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a local file and return its content.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string",
                                               "description": "absolute path"}},
                       "required": ["path"]},
    },
}]

COLORS = [("红", (255, 0, 0)), ("蓝", (0, 0, 255)), ("绿", (0, 255, 0))]


def call(base: str, key: str, model: str, body: dict, insecure: bool):
    url = base.rstrip("/") + "/chat/completions"
    status, text = post(url, key, {**body, "model": model}, insecure)
    if status != 200:
        return None, f"HTTP {status} {text[:220]}"
    try:
        return json.loads(text), None
    except Exception:  # noqa: BLE001
        return None, f"响应非 JSON: {text[:200]}"


def probe_text(base, key, model, insecure):
    d, err = call(base, key, model,
                  {"messages": [{"role": "user", "content": "只回答两个字：收到"}],
                   "max_tokens": 64}, insecure)
    if err:
        return err
    return f"OK {(d['choices'][0]['message'].get('content') or '').strip()[:20]!r}"


def probe_tools(base, key, model, insecure, repeats=3):
    """重复探测工具调用，输出命中率。单次结果受采样随机性影响，不可作为结论。"""
    hits, detail = 0, []
    for _ in range(repeats):
        d, err = call(base, key, model,
                      {"messages": [{"role": "user", "content": "读一下 /tmp/demo.txt"}],
                       "tools": TOOLS, "tool_choice": "auto", "max_tokens": 400}, insecure)
        if err:
            detail.append(f"失败:{err[:60]}")
            continue
        msg = d["choices"][0]["message"]
        names = [t["function"]["name"] for t in (msg.get("tool_calls") or [])]
        if names:
            hits += 1
            detail.append(f"调用={names}")
        else:
            detail.append(f"未调 finish={d['choices'][0].get('finish_reason')}")
    rate = f"{hits}/{repeats}"
    verdict = "支持" if hits == repeats else ("不支持" if hits == 0 else "不稳定")
    return f"{verdict} [{rate}] [{' | '.join(detail)}]"


def probe_vision(base, key, model, insecure):
    prompt = ("这张纯色图是什么颜色？只回答颜色的中文名。"
              "如果你没有收到图片或看不到图片，只回答四个字：看不到图。")
    hits = 0
    detail = []
    for name, rgb in COLORS:
        d, err = call(base, key, model, {
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{png_b64(rgb)}"}},
            ]}],
            "max_tokens": 60,
        }, insecure)
        if err:
            detail.append(f"{name}:失败")
            continue
        ans = (d["choices"][0]["message"].get("content") or "").strip().replace("\n", " ")
        ok = name in ans
        hits += 1 if ok else 0
        detail.append(f"{name}:{ans[:12]!r}")
    verdict = "支持" if hits == 3 else ("不支持" if hits == 0 else f"部分({hits}/3)")
    return f"{verdict} [{' | '.join(detail)}]"


def probe_max_tokens(base, key, model, insecure):
    """超大 max_tokens 会被上游校验拦下，报错里通常直接写明上限。"""
    d, err = call(base, key, model,
                  {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2000000},
                  insecure)
    if d is not None:
        return "上游不校验 max_tokens，上限未知，取保守值"
    return err


def probe_input_limit(base, key, model, insecure, chars=400000):
    """发超长输入拿实测下限：400k 字符（约 100k-200k token）通过则至少支持该长度。

    拿到的只是下限：通过 400k 不代表上限就是 400k。配置值取不高于实测下限的保守数。
    """
    filler = "数据" * (chars // 2)  # 重复无害内容，避免内容审查干扰长度校验
    d, err = call(base, key, model,
                  {"messages": [{"role": "user",
                                  "content": f"忽略以下内容，只回答两个字：收到\n\n{filler}"}],
                    "max_tokens": 32}, insecure)
    if err:
        return f"约 {chars // 1000}k 字符被拦：{err[:180]}"
    ans = (d["choices"][0]["message"].get("content") or "").strip()[:10]
    return f"通过（约 {chars // 1000}k 字符）回复={ans!r}，maxInputTokens 取不高于此的保守值"


def probe_reasoning(base, key, model, insecure):
    """看响应里有无 reasoning_content 字段。没有就关 supportsReasoning，
    开了只会显示一块空白思考区。"""
    d, err = call(base, key, model,
                  {"messages": [{"role": "user", "content": "1+1 等于几？"}],
                    "max_tokens": 200}, insecure)
    if err:
        return err
    msg = d["choices"][0]["message"]
    rc = msg.get("reasoning_content") or msg.get("reasoning")
    if rc:
        return f"有 reasoning_content（长度 {len(str(rc))}），supportsReasoning=true"
    return "无 reasoning_content，supportsReasoning=false（开了只显示空白思考区）"


def main() -> int:
    global _CAFILE
    ap = argparse.ArgumentParser(description="探测模型能力，供填写 models.json")
    ap.add_argument("--base", required=True, help="OpenAI 兼容根地址，例如 http://127.0.0.1:4000/v1")
    ap.add_argument("--key", required=True, help="API Key；连本地代理时填 PROXY_MASTER_KEY")
    ap.add_argument("--model", action="append", required=True, help="模型名，可重复")
    ap.add_argument("--insecure", action="store_true", help="跳过证书校验（自签网关）")
    ap.add_argument("--ca", default=None, metavar="PEM", help="自定义 CA 证书文件（私有 CA 网关）")
    ap.add_argument("--repeats", type=int, default=3, help="工具探测重复次数，默认 3；下结论建议 5")
    args = ap.parse_args()
    _CAFILE = args.ca

    for model in args.model:
        print(f"\n===== {model} =====")
        print(f"  文本     : {probe_text(args.base, args.key, model, args.insecure)}")
        print(f"  工具调用 : {probe_tools(args.base, args.key, model, args.insecure, args.repeats)}")
        print(f"  视觉     : {probe_vision(args.base, args.key, model, args.insecure)}")
        print(f"  输出上限 : {probe_max_tokens(args.base, args.key, model, args.insecure)}")
        print(f"  输入下限 : {probe_input_limit(args.base, args.key, model, args.insecure)}")
        print(f"  推理     : {probe_reasoning(args.base, args.key, model, args.insecure)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
