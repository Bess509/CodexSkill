"""对本地代理做五项能力验证。

用法（代理需已启动）：
    .venv/Scripts/python.exe test_bridge.py
"""
from __future__ import annotations

import base64
import json
import struct
import sys
import time
import zlib
from pathlib import Path

import bridge_env


def make_test_png(width: int = 64, height: int = 64) -> bytes:
    """零依赖生成一张蓝底红方块的 PNG，用于多模态测试。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            # 中心方块用纯红、背景用纯蓝，避免颜色不纯导致判定产生歧义
            if 16 <= x < 48 and 16 <= y < 48:
                raw += bytes((255, 0, 0))
            else:
                raw += bytes((0, 0, 220))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        head = struct.pack(">I", len(payload)) + tag + payload
        return head + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def hr(title: str) -> None:
    print("\n" + "-" * 60)
    print(title)
    print("-" * 60)


def describe_error(exc: Exception) -> str:
    """把 SDK 异常压成一行可读信息。"""
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    detail = ""
    if isinstance(body, dict):
        detail = json.dumps(body, ensure_ascii=False)[:400]
    elif body:
        detail = str(body)[:400]
    return f"{type(exc).__name__}" + (f" HTTP {status}: {detail}" if status else f": {exc}")


def proxy_alive(port: int) -> tuple[bool, str]:
    """测试开始前先确认本地代理是否在监听，避免五项全部的连接错误堆叠。

    APIConnectionError 只会说连不上，区分不了「代理没启动」和「代理启动后崩溃」，
    这一步把两类情况在开头就讲清楚。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return False, f"端口 127.0.0.1:{port} 无监听，本地代理未在运行"
    try:
        import urllib.request

        req = urllib.request.Request(f"http://127.0.0.1:{port}/health/liveliness")
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
        with urllib.request.urlopen(req, timeout=3) as resp:
            return True, f"代理存活，健康检查返回 {resp.status}"
    except Exception as exc:  # 端口在监听但服务未就绪
        return False, f"端口已监听但健康检查失败：{exc}"


def main() -> int:
    try:
        env = bridge_env.ensure_env()
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 1

    port = bridge_env.as_int(env.get("PROXY_PORT"), 4000)

    # 先确认代理活着。五项全 FAIL 往往只是代理没起，逐个跑完既慢又掩盖了真正的原因。
    ok, reason = proxy_alive(port)
    if not ok:
        print("=" * 60)
        print("本地代理未就绪，已跳过全部用例")
        print("=" * 60)
        print(f"原因：{reason}\n")
        print("请先双击 start.bat 启动代理，等待出现 'Uvicorn running on' 之类的日志，")
        print("再运行本测试。若 start.bat 窗口一闪而过或立即报错，把窗口内容发来即可定位。")
        return 1

    api_key = (env.get("PROXY_MASTER_KEY") or "").strip()
    if not api_key or bridge_env.is_placeholder(api_key):
        print("[错误] .env 中未配置 PROXY_MASTER_KEY。")
        print("       首次启动代理（start.bat / start_background.bat）会自动生成并写回 .env，")
        print("       也可用 status.bat 查询当前 key。")
        return 1
    base_url = f"http://127.0.0.1:{port}/v1"

    try:
        from openai import OpenAI
    except ImportError:
        print("[错误] 缺少 openai SDK，请先执行：")
        print("       .venv\\Scripts\\python.exe -m pip install openai")
        return 1

    models = bridge_env.proxy_model_pairs(env)
    if not models:
        print("[错误] UPSTREAM_MODELS 为空。")
        return 1
    model = models[0][0]

    # trust_env=False：忽略系统代理变量，保证测试请求直连本地代理，
    # 避免被本机 HTTP_PROXY 劫持导致 404 或挂起。
    import httpx

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=120.0,
        max_retries=0,
        http_client=httpx.Client(trust_env=False),
    )
    results: list[tuple[str, bool, str]] = []

    print(f"本地地址: {base_url}")
    print(f"测试模型: {model}")

    hr("[1/5] 模型列表接口 GET /v1/models")
    try:
        listing = client.models.list()
        ids = [m.id for m in listing.data]
        print("返回: " + ", ".join(ids[:10]))
        results.append(("模型列表", True, f"{len(ids)} 个模型"))
    except Exception as exc:  # noqa: BLE001
        print("失败: " + describe_error(exc))
        results.append(("模型列表", False, describe_error(exc)[:80]))

    hr("[2/5] 非流式对话（含 system 提升、max_tokens 缺省）")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你只回答结果，不解释。"},
                {"role": "user", "content": "1+1 等于几？只输出数字。"},
            ],
        )
        content = resp.choices[0].message.content or ""
        usage = resp.usage
        tokens = f"prompt={usage.prompt_tokens} completion={usage.completion_tokens}" if usage else "无 usage"
        print(f"回复: {content.strip()[:120]}")
        print(f"用量: {tokens}  finish_reason={resp.choices[0].finish_reason}")
        results.append(("非流式对话", bool(content.strip()), tokens))
    except Exception as exc:  # noqa: BLE001
        print("失败: " + describe_error(exc))
        results.append(("非流式对话", False, describe_error(exc)[:80]))

    hr("[3/5] 流式 SSE 输出")
    try:
        started = time.time()
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "用一句话说明什么是 SSE。"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks = 0
        text_parts: list[str] = []
        first_at = None
        usage = None
        for chunk in stream:
            chunks += 1
            if first_at is None and chunk.choices and chunk.choices[0].delta.content:
                first_at = time.time() - started
            if chunk.choices and chunk.choices[0].delta.content:
                text_parts.append(chunk.choices[0].delta.content)
            if getattr(chunk, "usage", None):
                usage = chunk.usage
        elapsed = time.time() - started
        text = "".join(text_parts).strip()
        print(f"分片数: {chunks}   首字节: {first_at:.2f}s   总耗时: {elapsed:.2f}s")
        print(f"内容: {text[:120]}")
        print(f"用量: {'有' if usage else '无'}")
        results.append(("流式输出", chunks > 1 and bool(text), f"{chunks} 个分片"))
    except Exception as exc:  # noqa: BLE001
        print("失败: " + describe_error(exc))
        results.append(("流式输出", False, describe_error(exc)[:80]))

    hr("[4/5] 工具调用 + 工具结果回传")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询指定城市的当前天气",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string", "description": "城市名称"}},
                    "required": ["city"],
                },
            },
        }
    ]
    history = [{"role": "user", "content": "杭州现在的天气怎么样？"}]
    ok_tool = False
    try:
        first = client.chat.completions.create(model=model, messages=history, tools=tools)
        msg = first.choices[0].message
        calls = msg.tool_calls or []
        print(f"finish_reason: {first.choices[0].finish_reason}")
        if not calls:
            print("未返回工具调用，模型直接作答: " + (msg.content or "")[:120])
            ok_tool = False
        else:
            call = calls[0]
            args = call.function.arguments
            print(f"调用: {call.function.name}({args})")
            try:
                json.loads(args)
                args_valid = True
            except json.JSONDecodeError:
                args_valid = False
            print(f"参数 JSON 可解析: {args_valid}")

            history.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in calls
            ]})
            history.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps({"city": "杭州", "weather": "晴", "temp_c": 24}, ensure_ascii=False),
            })
            second = client.chat.completions.create(model=model, messages=history, tools=tools)
            final_text = (second.choices[0].message.content or "").strip()
            print(f"工具结果回传后的回复: {final_text[:160]}")
            ok_tool = args_valid and bool(final_text)
        results.append(("工具调用", ok_tool, "含结果回传" if ok_tool else "未完成"))
    except Exception as exc:  # noqa: BLE001
        print("失败: " + describe_error(exc))
        results.append(("工具调用", False, describe_error(exc)[:80]))

    hr("[5/5] 多模态图片输入")
    try:
        png = make_test_png()
        b64 = base64.b64encode(png).decode("ascii")
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "图中位于中心的方块是什么颜色？只回答一个中文颜色词。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            ],
        )
        answer = (resp.choices[0].message.content or "").strip()
        print(f"图片大小: {len(png)} 字节")
        print(f"回复: {answer[:120]}")
        hit = ("红" in answer) or ("red" in answer.lower())
        if hit:
            results.append(("多模态输入", True, "颜色识别正确"))
        elif answer:
            # 图片块已成功送达（否则网关通常直接报错），但模型没看懂内容。
            # 这种情况多发生在中转未转发图像或模型未启用视觉能力，与协议转换无关。
            print("提示: 图片已送达且有回复，但识别结果与实际的红色不符。")
            print("      这通常由中转网关丢弃图像块、或该模型未启用视觉能力导致，")
            print("      与 OpenAI->Anthropic 的转换本身无关（可用 curl 直连上游对比确认）。")
            results.append(("多模态输入", False, f"有回复但识别错: {answer[:24]}"))
        else:
            results.append(("多模态输入", False, "无文字回复"))
    except Exception as exc:  # noqa: BLE001
        print("失败: " + describe_error(exc))
        results.append(("多模态输入", False, describe_error(exc)[:80]))

    hr("验证结果汇总")
    name_width = max(len(n) for n, _, _ in results)
    for name, ok, note in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{name.ljust(name_width)}  {flag}   {note}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} 项通过")

    failures = [n for n, ok, _ in results if not ok]
    if failures:
        print("\n未通过项排查顺序：")
        print("1. 打开代理终端窗口，看 LiteLLM 打印的上游报错原文")
        print("2. 运行 detect_models.py 确认模型名真实存在")
        print("3. 若报错提到 thinking / budget_tokens，把 .env 的 THINKING_ENABLED 改为 false")
        print("4. 若报错提到某个参数不被支持，确认 litellm_settings.drop_params 已开启")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
