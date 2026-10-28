"""本地 Mock Anthropic 上游，用于端到端验证协议转换是否正确。

用途：在还没有真实第三方地址时，先验证「OpenAI 请求 -> LiteLLM -> Anthropic 请求」
这条链路上 system 提升、max_tokens 兜底、工具调用、图片转换是否被正确处理。

用法：
    .venv/Scripts/python.exe mock_anthropic.py
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8899
EXPECTED_KEY = "mock-upstream-key"

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

_requests: list[dict] = []
_lock = threading.Lock()

# 测试钩子：消息里出现 [mock-force-tool:工具名] 时，无论 tools 字段如何都返回
# 该名字的 tool_use。用于验证「代理是否正确改写/丢弃工具名」这条链路，
# 正常对话不会包含该标记，行为不受影响。
FORCE_TOOL_RE = re.compile(r"\[mock-force-tool:([A-Za-z0-9_.\-]+)\]")


def _forced_tool(payload: dict) -> str | None:
    for msg in payload.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            found = FORCE_TOOL_RE.search(content)
            if found:
                return found.group(1)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    found = FORCE_TOOL_RE.search(block.get("text") or "")
                    if found:
                        return found.group(1)
    return None


def _has_image(payload: dict) -> bool:
    for msg in payload.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    return True
    return False


def _has_tool_result(payload: dict) -> bool:
    for msg in payload.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
    return False


def _decide(payload: dict) -> tuple[str, str]:
    """根据请求内容决定回复策略，返回 (类别, 文本)。"""
    if _has_image(payload):
        return "text", "红色"
    tools = payload.get("tools") or []
    if tools and not _has_tool_result(payload):
        return "tool_use", ""
    if _has_tool_result(payload):
        return "text", "杭州今天晴，气温 24 度。"
    text_blobs = [
        b.get("text", "")
        for m in payload.get("messages", [])
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    joined = " ".join(text_blobs)
    if "1+1" in joined:
        return "text", "2"
    if "SSE" in joined:
        return "text", "SSE 是一种服务端向浏览器单向推送数据的长连接机制。"
    return "text", "这是来自 Mock Anthropic 上游的回复。"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockAnthropic/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        pass

    def _write_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self) -> None:
        self._write_json(401, {
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        })

    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/models"):
            self._write_json(200, {
                "data": [
                    {"id": "claude-sonnet-4-5-20250929", "display_name": "Claude Sonnet 4.5"},
                    {"id": "claude-opus-4-1-20250805", "display_name": "Claude Opus 4.1"},
                ]
            })
            return
        self._write_json(404, {"type": "error", "error": {"type": "not_found", "message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.endswith("/messages"):
            self._write_json(404, {"type": "error", "error": {"type": "not_found", "message": "not found"}})
            return
        if self.headers.get("x-api-key") != EXPECTED_KEY:
            self._unauthorized()
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._write_json(400, {"type": "error", "error": {"type": "invalid_request", "message": "bad json"}})
            return

        with _lock:
            _requests.append(payload)
            (LOG_DIR / "last_upstream_request.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        forced = _forced_tool(payload)
        if forced:
            category, text = "tool_use", ""
        else:
            category, text = _decide(payload)
        model = payload.get("model", "mock-model")

        if category == "tool_use":
            tool_name = forced or (payload.get("tools") or [{}])[0].get("name", "get_weather")
            tool_id = "toolu_mock_1"
            if payload.get("stream"):
                self._stream_tool_use(model, tool_name, tool_id)
            else:
                self._write_json(200, {
                    "id": "msg_mock_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [
                        {"type": "text", "text": "我来查一下。"},
                        {"type": "tool_use", "id": tool_id, "name": tool_name,
                         "input": {"city": "杭州"}},
                    ],
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 42, "output_tokens": 28},
                })
            return

        if payload.get("stream"):
            self._stream_text(model, text)
        else:
            self._write_json(200, {
                "id": "msg_mock_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 42, "output_tokens": len(text)},
            })

    def _sse(self, event: str, data: dict) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
        self.wfile.flush()

    def _begin_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _end_stream(self) -> None:
        # 终止分块：0 长度块 + 空行
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _stream_text(self, model: str, text: str) -> None:
        self._begin_stream()
        self._sse("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_mock_1", "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 42, "output_tokens": 1},
            },
        })
        self._sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        pieces = [text[i:i + 4] for i in range(0, len(text), 4)] or [""]
        for piece in pieces:
            time.sleep(0.02)
            self._sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": piece},
            })
        self._sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": len(text)},
        })
        self._sse("message_stop", {"type": "message_stop"})
        self._end_stream()

    def _stream_tool_use(self, model: str, tool_name: str, tool_id: str) -> None:
        self._begin_stream()
        self._sse("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_mock_1", "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 42, "output_tokens": 1},
            },
        })
        self._sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        self._sse("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "我来查一下。"},
        })
        self._sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        self._sse("content_block_start", {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}},
        })
        # 参数按片段下发，用于验证 arguments 的增量拼接
        for piece in ['{"city":', ' "杭州"}']:
            time.sleep(0.02)
            self._sse("content_block_delta", {
                "type": "content_block_delta", "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": piece},
            })
        self._sse("content_block_stop", {"type": "content_block_stop", "index": 1})
        self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 28},
        })
        self._sse("message_stop", {"type": "message_stop"})
        self._end_stream()


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Mock Anthropic 上游已启动: http://{HOST}:{PORT}")
    print(f"期望的 x-api-key: {EXPECTED_KEY}")
    print("请求记录写入: logs/last_upstream_request.json")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
