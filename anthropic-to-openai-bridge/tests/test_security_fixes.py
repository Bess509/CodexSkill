"""六项安全修复的定向回归测试。

覆盖：
    1. TLS 默认校验 / 自定义 CA / --insecure 显式绕过
    2. 进程身份核对（陈旧 pid、pid 复用、归属校验）
    3. 空工具声明拒绝、别名只映射到显式声明
    4. tool_choice 同步（含 auto/none/required 保持不变）
    5. 流式工具移除（含后续参数分片）
    6. 完整转储默认关闭、脱敏
    7. token 下限/上限顺序与配置冲突校验

用法（需在装有 litellm 的 venv 内跑，bridge_guard 依赖 litellm）：
    .venv/Scripts/python.exe tests/test_security_fixes.py
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCAFFOLD = SKILL_ROOT / "assets" / "scaffold"
sys.path.insert(0, str(SCAFFOLD))
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import daemon_ctl  # noqa: E402
import detect_models  # noqa: E402
import gen_config  # noqa: E402

try:
    import bridge_guard as bg  # noqa: E402
    HAS_GUARD = True
except Exception as exc:  # noqa: BLE001  # 无 litellm 时跳过 guard 相关用例
    bg = None
    HAS_GUARD = False
    GUARD_IMPORT_ERROR = exc


requires_guard = unittest.skipUnless(HAS_GUARD, "bridge_guard 需要 litellm")


# ---------------------------------------------------------------- 1. TLS


class TLSContextTests(unittest.TestCase):
    def test_default_verifies(self):
        ctx = detect_models.build_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_insecure_disables(self):
        ctx = detect_models.build_ssl_context(insecure=True)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)

    def test_missing_ca_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope.pem")
            with self.assertRaises(FileNotFoundError):
                detect_models.configure_tls(False, missing)

    def test_configure_sets_opener(self):
        detect_models.configure_tls(False, None)
        self.assertIsNotNone(detect_models._opener())

    def test_probe_scripts_accept_ca_flag(self):
        """两个探测脚本的 --insecure 必须是显式开关，且支持 --ca。"""
        for script in ("probe_upstream.py", "probe_model_caps.py"):
            path = SKILL_ROOT / "scripts" / script
            text = path.read_text(encoding="utf-8")
            self.assertIn('"--insecure"', text, script)
            self.assertIn('"--ca"', text, script)
            self.assertIn("cafile", text, script)


class TLSHandshakeTests(unittest.TestCase):
    """起一个自签 HTTPS 服务，验证三种 TLS 模式的实际行为。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.cert = base / "cert.pem"
        cls.key = base / "key.pem"
        try:
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048",
                    "-keyout", str(cls.key), "-out", str(cls.cert),
                    "-days", "1", "-nodes", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1",
                ],
                check=True, capture_output=True, timeout=60,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            raise unittest.SkipTest("openssl 不可用，跳过 TLS 握手测试")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b'{"data":[]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cls.cert), str(cls.key))
        cls.server.socket = ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"https://127.0.0.1:{cls.port}/v1/models"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def tearDown(self):
        detect_models.configure_tls(False, None)

    def test_self_signed_rejected_by_default(self):
        detect_models.configure_tls(False, None)
        code, text = detect_models.request(self.url, "k", timeout=10)
        self.assertEqual(code, -1)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", text.upper().replace(" ", "_"))

    def test_self_signed_accepted_with_custom_ca(self):
        detect_models.configure_tls(False, str(self.cert))
        code, _ = detect_models.request(self.url, "k", timeout=10)
        self.assertEqual(code, 200)

    def test_self_signed_accepted_with_insecure(self):
        detect_models.configure_tls(True, None)
        code, _ = detect_models.request(self.url, "k", timeout=10)
        self.assertEqual(code, 200)


# ---------------------------------------------------------------- 2. 进程身份


def _row(pid=1000, name="pythonw.exe", cmd="", exe="", created=""):
    return {"pid": pid, "ppid": 1, "name": name, "cmd": cmd,
            "exe": exe, "created": created}


class IdentityTests(unittest.TestCase):
    ROOT = str(daemon_ctl.ROOT)

    def test_missing_process_info_refused(self):
        ok, why = daemon_ctl.verify_identity({"schema": 2, "pid": 1}, None)
        self.assertFalse(ok)
        self.assertIn("无法读取", why)

    def test_pid_mismatch_refused(self):
        ident = {"schema": 2, "pid": 1000, "script": "x", "root": self.ROOT}
        ok, why = daemon_ctl.verify_identity(ident, _row(pid=2000))
        self.assertFalse(ok)
        self.assertIn("进程号不符", why)

    def test_legacy_pidfile_uses_command_line(self):
        ident = {"schema": 1, "pid": 1000}
        cmd = f'{self.ROOT}\\.venv\\Scripts\\pythonw.exe daemon.py'
        ok, _ = daemon_ctl.verify_identity(ident, _row(cmd=cmd, exe=f"{self.ROOT}\\.venv\\Scripts\\pythonw.exe"))
        self.assertTrue(ok)

    def test_legacy_pidfile_rejects_unrelated(self):
        ok, why = daemon_ctl.verify_identity(
            {"schema": 1, "pid": 1000},
            _row(cmd="C:\\other\\app.exe --serve", exe="C:\\other\\app.exe"),
        )
        self.assertFalse(ok)

    def test_full_identity_match(self):
        script = f"{self.ROOT}\\daemon.py"
        exe = f"{self.ROOT}\\.venv\\Scripts\\pythonw.exe"
        ident = {"schema": 2, "pid": 1000, "script": script, "exe": exe,
                 "root": self.ROOT, "created": "2026-10-28T13:52:11+08:00"}
        ok, why = daemon_ctl.verify_identity(
            ident,
            _row(cmd=f'"{exe}" "{script}"', exe=exe, created="2026-10-28T13:52:11.9876540+08:00"),
        )
        self.assertTrue(ok, why)

    def test_relative_script_path_still_matches(self):
        """start_background.bat 用的是相对路径 daemon.py。"""
        exe = f"{self.ROOT}\\.venv\\Scripts\\pythonw.exe"
        ident = {"schema": 2, "pid": 1000, "script": f"{self.ROOT}\\daemon.py",
                 "exe": exe, "root": self.ROOT, "created": "2026-10-28T13:52:11+08:00"}
        ok, why = daemon_ctl.verify_identity(
            ident,
            _row(cmd=f'"{exe}" daemon.py', exe=exe, created="2026-10-28T13:52:11.9876540+08:00"),
        )
        self.assertTrue(ok, why)

    def test_reused_pid_by_created_time(self):
        """pid 复用：脚本路径碰巧相似，但启动时间差得远。"""
        script = f"{self.ROOT}\\daemon.py"
        exe = f"{self.ROOT}\\.venv\\Scripts\\pythonw.exe"
        ident = {"schema": 2, "pid": 1000, "script": script, "exe": exe,
                 "root": self.ROOT, "created": "2026-10-28T13:00:00+08:00"}
        ok, why = daemon_ctl.verify_identity(
            ident,
            _row(cmd=f'"{exe}" "{script}"', exe=exe, created="2026-10-28T15:00:00+08:00"),
        )
        self.assertFalse(ok)
        self.assertIn("启动时间", why)

    def test_exe_mismatch_refused(self):
        script = f"{self.ROOT}\\daemon.py"
        ident = {"schema": 2, "pid": 1000, "script": script,
                 "exe": f"{self.ROOT}\\.venv\\Scripts\\pythonw.exe", "root": self.ROOT,
                 "created": "2026-10-28T13:52:11+08:00"}
        ok, why = daemon_ctl.verify_identity(
            ident,
            _row(cmd=f'"C:\\other\\python.exe" "{script}"', exe="C:\\other\\python.exe",
                 created="2026-10-28T13:52:11+08:00"),
        )
        self.assertFalse(ok)
        self.assertIn("解释器路径", why)

    def test_unrelated_script_refused(self):
        """解释器是本脚手架的，但命令行指向别处的 daemon.py：仍要拒绝。"""
        exe = f"{self.ROOT}\\.venv\\Scripts\\pythonw.exe"
        ident = {"schema": 2, "pid": 1000, "script": f"{self.ROOT}\\daemon.py",
                 "exe": exe, "root": self.ROOT,
                 "created": "2026-10-28T13:52:11+08:00"}
        ok, why = daemon_ctl.verify_identity(
            ident,
            _row(cmd=f'"{exe}" "C:\\other\\daemon.py"', exe=exe,
                 created="2026-10-28T13:52:11+08:00"),
        )
        self.assertFalse(ok)
        self.assertIn("脚本路径", why)

    def test_read_identity_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "daemon.pid"
            original = daemon_ctl.PID_FILE
            daemon_ctl.PID_FILE = pid_file
            try:
                pid_file.write_text("4321", encoding="ascii")
                self.assertEqual(daemon_ctl.read_identity()["pid"], 4321)

                pid_file.write_text(json.dumps({"schema": 2, "pid": 777, "script": "s"}), encoding="ascii")
                self.assertEqual(daemon_ctl.read_identity()["schema"], 2)

                pid_file.write_text("not-json-at-all", encoding="ascii")
                self.assertIsNone(daemon_ctl.read_identity())

                pid_file.write_text("", encoding="ascii")
                self.assertIsNone(daemon_ctl.read_identity())

                pid_file.unlink()
                self.assertIsNone(daemon_ctl.read_identity())
            finally:
                daemon_ctl.PID_FILE = original

    def test_belongs_to_scaffold(self):
        inside, _ = daemon_ctl.belongs_to_scaffold(
            _row(cmd=f"{self.ROOT}\\daemon.py", exe=f"{self.ROOT}\\.venv\\Scripts\\python.exe")
        )
        self.assertTrue(inside)
        outside, why = daemon_ctl.belongs_to_scaffold(_row(cmd="C:\\other\\svc.exe", exe="C:\\other\\svc.exe"))
        self.assertFalse(outside)
        self.assertIn("不在", why)

    def test_live_process_identity(self):
        """拿当前真实进程自检：写 pidfile 后应核对通过。"""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "daemon.pid"
            original = daemon_ctl.PID_FILE
            daemon_ctl.PID_FILE = pid_file
            try:
                identity = {
                    "schema": 2,
                    "pid": os.getpid(),
                    "created": "",
                    "exe": str(Path(sys.executable).resolve()),
                    "script": str(Path(__file__).resolve()),
                    "root": str(daemon_ctl.ROOT),
                }
                pid_file.write_text(json.dumps(identity), encoding="ascii")
                chain = daemon_ctl.process_chain(os.getpid())
                # 脚本路径挂在命令行里这一条对本测试进程不成立（跑的是测试文件），
                # 因此这里只验证"读回一致 + 真实进程信息可取到"
                self.assertEqual(daemon_ctl.read_identity()["pid"], os.getpid())
                self.assertTrue(chain)
                self.assertEqual(chain[0]["pid"], os.getpid())
            finally:
                daemon_ctl.PID_FILE = original


# ---------------------------------------------------------------- 3/4. 工具声明与改名


class ToolDeclarationTests(unittest.TestCase):
    @requires_guard
    def test_empty_declarations_reject_everything(self):
        for name in ("Read", "webReader", "ToolSearch", "whatever"):
            self.assertIsNone(bg._resolve_name(name, set()), name)

    @requires_guard
    def test_declared_tool_passes(self):
        self.assertEqual(bg._resolve_name("Read", {"Read"}), "Read")
        self.assertEqual(bg._resolve_name("SearchDeferredMcpTools", {"ToolSearch"}), "ToolSearch")

    @requires_guard
    def test_alias_only_maps_to_declared_target(self):
        self.assertEqual(bg._resolve_name("webReader", {"WebFetch"}), "WebFetch")
        self.assertIsNone(bg._resolve_name("webReader", {"Read"}))
        self.assertIsNone(bg._resolve_name("webReader", set()))

    @requires_guard
    def test_client_declared_excludes_own_alias(self):
        payload = {"tools": [
            {"type": "function", "function": {"name": "WebFetch", "description": "读网页"}},
            bg._builtin_schema("webReader", "WebFetch"),
        ]}
        self.assertEqual(bg._client_declared(payload), {"WebFetch"})

    @requires_guard
    def test_client_declared_keeps_same_name_tool(self):
        """客户端自己声明的 webReader 不带代理标记，必须保留。"""
        payload = {"tools": [
            {"type": "function", "function": {"name": "webReader", "description": "客户端自有实现"}},
        ]}
        self.assertEqual(bg._client_declared(payload), {"webReader"})

    @requires_guard
    def test_client_declared_restores_renamed_tools(self):
        """request_data 里的工具已被改名，响应侧要还原成客户端原名。"""
        payload = {"tools": [
            {"type": "function", "function": {"name": "SearchDeferredMcpTools", "description": "x"}},
            {"type": "function", "function": {"name": "InvokeDeferredMcpTool", "description": "y"}},
        ]}
        self.assertEqual(bg._client_declared(payload), {"ToolSearch", "DeferExecuteTool"})

    @requires_guard
    def test_empty_tools_field(self):
        self.assertEqual(bg._client_declared({"tools": []}), set())
        self.assertEqual(bg._client_declared({}), set())

    @requires_guard
    def test_tool_choice_sync_openai_shape(self):
        payload = {"tool_choice": {"type": "function", "function": {"name": "ToolSearch"}}}
        changed = bg._sync_tool_choice(payload)
        self.assertIsNotNone(changed)
        self.assertEqual(payload["tool_choice"]["function"]["name"], "SearchDeferredMcpTools")

    @requires_guard
    def test_tool_choice_sync_anthropic_shape(self):
        payload = {"tool_choice": {"type": "tool", "name": "DeferExecuteTool"}}
        bg._sync_tool_choice(payload)
        self.assertEqual(payload["tool_choice"]["name"], "InvokeDeferredMcpTool")

    @requires_guard
    def test_tool_choice_modes_preserved(self):
        for mode in ("auto", "none", "required"):
            payload = {"tool_choice": mode}
            self.assertIsNone(bg._sync_tool_choice(payload))
            self.assertEqual(payload["tool_choice"], mode)

    @requires_guard
    def test_tool_choice_unknown_name_untouched(self):
        payload = {"tool_choice": {"type": "function", "function": {"name": "Read"}}}
        self.assertIsNone(bg._sync_tool_choice(payload))
        self.assertEqual(payload["tool_choice"]["function"]["name"], "Read")

    @requires_guard
    def test_rename_forward_covers_tools_history_and_choice(self):
        payload = {
            "tools": [{"type": "function", "function": {"name": "ToolSearch", "description": "old"}}],
            "tool_choice": {"type": "function", "function": {"name": "ToolSearch"}},
            "messages": [
                {"role": "assistant", "tool_calls": [
                    {"id": "c1", "function": {"name": "ToolSearch", "arguments": "{}"}}]},
                {"role": "tool", "name": "ToolSearch", "content": "r"},
            ],
        }
        changed = bg._rename_forward(payload)
        self.assertEqual(payload["tools"][0]["function"]["name"], "SearchDeferredMcpTools")
        self.assertEqual(payload["tool_choice"]["function"]["name"], "SearchDeferredMcpTools")
        self.assertEqual(payload["messages"][0]["tool_calls"][0]["function"]["name"],
                         "SearchDeferredMcpTools")
        self.assertEqual(payload["messages"][1]["name"], "SearchDeferredMcpTools")
        self.assertTrue(any("tool_choice" in item for item in changed))


# ---------------------------------------------------------------- 5. 流式移除


class FakeFunction:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, index, name=None, arguments=None):
        self.index = index
        self.function = FakeFunction(name, arguments)


class FakeDelta:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls
        self.content = content


class FakeChoice:
    def __init__(self, index, tool_calls=None, content=None, finish_reason=None):
        self.index = index
        self.delta = FakeDelta(tool_calls, content)
        self.finish_reason = finish_reason


class FakeChunk:
    def __init__(self, choices):
        self.choices = choices


@requires_guard
class StreamingRemovalTests(unittest.TestCase):
    def test_unknown_tool_and_its_argument_chunks_dropped(self):
        declared = {"Read"}
        dropped_idx: set = set()
        dropped: list[str] = []

        first = FakeChunk([FakeChoice(0, [FakeToolCall(0, "webReader")])])
        dropped += bg._restore_in_chunk(first, declared, dropped_idx)
        self.assertEqual(dropped, ["webReader"])
        self.assertEqual(first.choices[0].delta.tool_calls, [])

        for piece in ('{"url":', ' "https://x"}', '}'):
            chunk = FakeChunk([FakeChoice(0, [FakeToolCall(0, None, piece)])])
            bg._restore_in_chunk(chunk, declared, dropped_idx)
            self.assertEqual(chunk.choices[0].delta.tool_calls, [], piece)

    def test_declared_tool_kept_and_renamed(self):
        declared = {"ToolSearch"}
        dropped_idx: set = set()
        chunk = FakeChunk([FakeChoice(0, [FakeToolCall(0, "SearchDeferredMcpTools")])])
        dropped = bg._restore_in_chunk(chunk, declared, dropped_idx)
        self.assertEqual(dropped, [])
        self.assertEqual(chunk.choices[0].delta.tool_calls[0].function.name, "ToolSearch")

    def test_empty_declaration_drops_all_tools(self):
        dropped_idx: set = set()
        chunk = FakeChunk([FakeChoice(0, [FakeToolCall(0, "Read"), FakeToolCall(1, "Grep")])])
        dropped = bg._restore_in_chunk(chunk, set(), dropped_idx)
        self.assertEqual(sorted(dropped), ["Grep", "Read"])
        self.assertEqual(chunk.choices[0].delta.tool_calls, [])
        self.assertIn((0, 0), dropped_idx)
        self.assertIn((0, 1), dropped_idx)

    def test_choice_index_isolated(self):
        """n>1 时不同 choice 的同号工具不能串味。"""
        declared = {"Read"}
        dropped_idx: set = set()
        good = FakeChunk([FakeChoice(0, [FakeToolCall(0, "Read")])])
        bad = FakeChunk([FakeChoice(1, [FakeToolCall(0, "webReader")])])
        bg._restore_in_chunk(good, declared, dropped_idx)
        bg._restore_in_chunk(bad, declared, dropped_idx)
        self.assertEqual(len(good.choices[0].delta.tool_calls), 1)
        self.assertEqual(bad.choices[0].delta.tool_calls, [])
        self.assertIn((1, 0), dropped_idx)

    def test_non_stream_message_drops_unknown(self):
        class Msg:
            def __init__(self, calls):
                self.tool_calls = calls

        kept = FakeToolCall(0, "Read")
        gone = FakeToolCall(1, "webReader")
        msg = Msg([kept, gone])
        dropped = bg._restore_in_message(msg, {"Read"})
        self.assertEqual(dropped, ["webReader"])
        self.assertEqual(len(msg.tool_calls), 1)


# ---------------------------------------------------------------- 6. 转储默认值


@requires_guard
class DumpTests(unittest.TestCase):
    def test_dump_disabled_by_default(self):
        self.assertFalse(bg.DUMP_ENABLED)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dump.json"
            original = bg.DUMP_FILE
            bg.DUMP_FILE = target
            try:
                bg._dump({"model": "m", "messages": [{"role": "user", "content": "secret"}]})
                self.assertFalse(target.exists(), "默认不应写完整请求转储")
            finally:
                bg.DUMP_FILE = original

    def test_dump_when_enabled_redacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dump.json"
            original_file, original_flag = bg.DUMP_FILE, bg.DUMP_ENABLED
            bg.DUMP_FILE, bg.DUMP_ENABLED = target, True
            try:
                payload = {
                    "model": "m",
                    "authorization": "Bearer sk-secret",
                    "messages": [
                        {"role": "user", "content": "hello"},
                        {"role": "user", "content": [
                            {"type": "image", "source": {"type": "base64", "data": "A" * 5000}}]},
                    ],
                }
                bg._dump(payload)
                text = target.read_text(encoding="utf-8")
                self.assertIn("<redacted>", text)
                self.assertNotIn("sk-secret", text)
                self.assertNotIn("A" * 100, text)
                self.assertIn("redacted image base64", text)
            finally:
                bg.DUMP_FILE, bg.DUMP_ENABLED = original_file, original_flag

    def test_redact_deep_truncates_long_strings(self):
        out = bg._redact_deep({"k": "x" * 10000})
        self.assertLess(len(out["k"]), 2000)
        self.assertIn("truncated", out["k"])

    def test_routine_log_has_no_content_head(self):
        """常规日志只留长度，不留回复正文。"""
        class Msg:
            content = "这是模型回复正文"
            tool_calls = None

        class Choice:
            message = Msg()
            finish_reason = "stop"

        class Resp:
            choices = [Choice()]
            usage = None

        summary = bg._resp_summary(Resp())
        self.assertIn("content_len", summary)
        self.assertNotIn("content_head", summary)

    def test_dump_off_yields_content_head_when_on(self):
        original = bg.DUMP_ENABLED
        bg.DUMP_ENABLED = True
        try:
            class Msg:
                content = "正文"
                tool_calls = None

            class Choice:
                message = Msg()
                finish_reason = "stop"

            class Resp:
                choices = [Choice()]
                usage = None

            self.assertIn("content_head", bg._resp_summary(Resp()))
        finally:
            bg.DUMP_ENABLED = original


# ---------------------------------------------------------------- 7. token 限制


@requires_guard
class TokenLimitTests(unittest.TestCase):
    def test_min_then_clamp(self):
        data = {"model": "anthropic/glm-5.3", "max_tokens": 100}
        bg._ensure_min_tokens(data)
        self.assertEqual(data["max_tokens"], bg.MIN_MAX_TOKENS)
        bg._clamp_max_tokens(data)
        # 下限仍低于该模型上限，夹紧不应把值改回去
        self.assertEqual(data["max_tokens"], bg.MIN_MAX_TOKENS)

    def test_over_cap_clamped(self):
        data = {"model": "glm-5.3", "max_tokens": 999999}
        bg._ensure_min_tokens(data)
        bg._clamp_max_tokens(data)
        self.assertEqual(data["max_tokens"], 131072)

    def test_missing_max_tokens_gets_floor(self):
        data = {"model": "glm-5.3"}
        bg._ensure_min_tokens(data)
        bg._clamp_max_tokens(data)
        self.assertEqual(data["max_tokens"], bg.MIN_MAX_TOKENS)

    def test_no_cap_model_untouched_by_clamp(self):
        data = {"model": "unknown-model", "max_tokens": 500000}
        bg._ensure_min_tokens(data)
        bg._clamp_max_tokens(data)
        self.assertEqual(data["max_tokens"], 500000)

    def test_floor_above_cap_wins_by_cap(self):
        """下限高于模型上限时，最终值必须落在上限内（否则整轮 400）。"""
        original_min, original_caps = bg.MIN_MAX_TOKENS, dict(bg.MAX_OUTPUT_CAPS)
        bg.MIN_MAX_TOKENS = 999999
        bg.MAX_OUTPUT_CAPS["tiny-model"] = 8000
        try:
            data = {"model": "tiny-model", "max_tokens": 10}
            bg._ensure_min_tokens(data)
            bg._clamp_max_tokens(data)
            self.assertEqual(data["max_tokens"], 8000)
        finally:
            bg.MIN_MAX_TOKENS = original_min
            bg.MAX_OUTPUT_CAPS.clear()
            bg.MAX_OUTPUT_CAPS.update(original_caps)

    def test_positive_int_env_validation(self):
        cases = [("abc", 1600), ("0", 1600), ("-5", 1600), ("", 1600), ("500", 500)]
        for raw, expect in cases:
            os.environ["BRIDGE_TEST_INT"] = raw
            self.assertEqual(bg._positive_int_env("BRIDGE_TEST_INT", 1600), expect, raw)
        os.environ.pop("BRIDGE_TEST_INT", None)

    def test_extra_caps_rejects_invalid(self):
        for raw in ('{"m": 0}', '{"m": -1}', '{"m": "x"}', "not-json", '["x"]'):
            os.environ["BRIDGE_MAX_OUTPUT_CAPS"] = raw
            self.assertEqual(bg._load_extra_caps(), {}, raw)
        os.environ["BRIDGE_MAX_OUTPUT_CAPS"] = '{"m": 1024}'
        self.assertEqual(bg._load_extra_caps(), {"m": 1024})
        os.environ.pop("BRIDGE_MAX_OUTPUT_CAPS", None)


class GenConfigValidationTests(unittest.TestCase):
    BASE = {"UPSTREAM_BASE_URL": "https://a.com", "UPSTREAM_MODELS": "m1"}

    def _env(self, **extra):
        env = dict(self.BASE)
        env.update(extra)
        return env

    def test_min_output_tokens_must_be_positive(self):
        for value in ("0", "-1", "abc"):
            with self.assertRaises(ValueError):
                gen_config.build_yaml(self._env(MIN_OUTPUT_TOKENS=value))

    def test_thinking_budget_must_be_positive(self):
        for value in ("0", "-100", "x"):
            with self.assertRaises(ValueError):
                gen_config.build_yaml(self._env(THINKING_BUDGET_TOKENS=value))

    def test_request_timeout_must_be_positive(self):
        with self.assertRaises(ValueError):
            gen_config.build_yaml(self._env(REQUEST_TIMEOUT="0"))

    def test_thinking_budget_conflict(self):
        with self.assertRaises(ValueError) as ctx:
            gen_config.build_yaml(self._env(
                THINKING_ENABLED="true", THINKING_BUDGET_TOKENS="2000", MIN_OUTPUT_TOKENS="1600"))
        self.assertIn("THINKING_BUDGET_TOKENS", str(ctx.exception))

    def test_thinking_ok_when_budget_below_floor(self):
        yaml_text = gen_config.build_yaml(self._env(
            THINKING_ENABLED="true", THINKING_BUDGET_TOKENS="2000", MIN_OUTPUT_TOKENS="4000"))
        self.assertIn("budget_tokens: 2000", yaml_text)

    def test_cap_below_floor_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            gen_config.build_yaml(self._env(
                MIN_OUTPUT_TOKENS="1600", MAX_OUTPUT_TOKEN_CAPS='{"m1": 800}'))
        self.assertIn("小于 MIN_OUTPUT_TOKENS", str(ctx.exception))

    def test_cap_conflict_mentions_thinking_budget(self):
        """开启思考时，上限不足的错误要同时点出预算约束。"""
        with self.assertRaises(ValueError) as ctx:
            gen_config.build_yaml(self._env(
                THINKING_ENABLED="true", THINKING_BUDGET_TOKENS="2000",
                MIN_OUTPUT_TOKENS="4000", MAX_OUTPUT_TOKEN_CAPS='{"m1": 2000}'))
        message = str(ctx.exception)
        self.assertIn("小于 MIN_OUTPUT_TOKENS", message)
        self.assertIn("思考预算", message)

    def test_caps_invalid_json(self):
        with self.assertRaises(ValueError):
            gen_config.build_yaml(self._env(MAX_OUTPUT_TOKEN_CAPS="not-json"))

    def test_caps_non_positive_rejected(self):
        with self.assertRaises(ValueError):
            gen_config.build_yaml(self._env(MAX_OUTPUT_TOKEN_CAPS='{"m1": 0}'))

    def test_defaults_produce_valid_yaml(self):
        yaml_text = gen_config.build_yaml(self._env())
        self.assertIn("model_list:", yaml_text)
        self.assertIn("api_base: \"https://a.com\"", yaml_text)


def main() -> int:
    if not HAS_GUARD:
        print(f"[提示] bridge_guard 无法导入（{GUARD_IMPORT_ERROR}），相关用例将跳过。")
        print("       请在装有 litellm 的 venv 里运行以覆盖全部用例。")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TLSContextTests, TLSHandshakeTests, IdentityTests,
        ToolDeclarationTests, StreamingRemovalTests, DumpTests,
        TokenLimitTests, GenConfigValidationTests,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
