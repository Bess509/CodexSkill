"""第二批复盘的定向回归测试（P1/P1/P2/P2/P2）。

覆盖：
    1. 中文目录下 pidfile 身份记录可写出、可读回（UTF-8 一致，旧数字格式仍兼容）
    2. 孤儿清理只在"归属明确"的进程里选终止根节点，终止前复核；无关 uvicorn 父进程
       及其其他子进程不得被波及
    3. 诊断脱敏不改动live请求（Anthropic image 块 / OpenAI image_url 块两种形态）
    4. 身份证据不全（exe/created/root 缺失或非法）一律拒绝自动终止
    5. .env 里的转储开关与字符上限能到达 LiteLLM 子进程，关闭时不留陈旧 true

用法（需在装有 litellm 的 venv 内跑，bridge_guard 依赖 litellm）：
    .venv/Scripts/python.exe tests/test_regression_fixes.py
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCAFFOLD = SKILL_ROOT / "assets" / "scaffold"
sys.path.insert(0, str(SCAFFOLD))

import boot_common  # noqa: E402
import bridge_env  # noqa: E402
import daemon  # noqa: E402
import daemon_ctl  # noqa: E402

try:
    import bridge_guard as bg  # noqa: E402
    HAS_GUARD = True
    GUARD_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001  # 无 litellm 时跳过 guard 相关用例
    bg = None
    HAS_GUARD = False
    GUARD_IMPORT_ERROR = exc

requires_guard = unittest.skipUnless(HAS_GUARD, "bridge_guard 需要 litellm")


# ------------------------------------------------------- 1. 身份文件编码


class IdentityEncodingTests(unittest.TestCase):
    """P1：daemon.py 用 ensure_ascii=False 序列化却按 ASCII 写盘。"""

    def _patched(self, pid_file: Path, log_dir: Path, root: Path | None = None):
        class _Ctx:
            def __enter__(self):
                self.orig = (daemon.PID_FILE, daemon.LOG_DIR, daemon_ctl.PID_FILE, daemon.ROOT)
                daemon.PID_FILE = pid_file
                daemon.LOG_DIR = log_dir
                daemon_ctl.PID_FILE = pid_file
                if root is not None:
                    daemon.ROOT = root
                return self

            def __exit__(self, *exc):
                (daemon.PID_FILE, daemon.LOG_DIR,
                 daemon_ctl.PID_FILE, daemon.ROOT) = self.orig
                return False

        return _Ctx()

    def test_write_and_read_under_chinese_path(self):
        """中文目录下写身份记录不能抛 UnicodeEncodeError，读回路径不能损坏。"""
        with tempfile.TemporaryDirectory() as tmp:
            cn_dir = Path(tmp) / "中文目录" / "桥接脚手架"
            cn_dir.mkdir(parents=True)
            pid_file = cn_dir / "daemon.pid"
            with self._patched(pid_file, cn_dir / "logs", root=cn_dir):
                daemon.write_pid()
                identity = daemon_ctl.read_identity()

            self.assertIsNotNone(identity)
            self.assertEqual(identity["schema"], 2)
            self.assertEqual(identity["pid"], os.getpid())
            # 落盘内容必须是合法 UTF-8（旧实现用 ASCII 写会直接抛异常）
            raw = pid_file.read_bytes()
            decoded = json.loads(raw.decode("utf-8"))
            # 记录里的中文绝对路径必须原样保留，不能变成问号或替换符
            self.assertEqual(decoded["root"], str(cn_dir))
            self.assertIn("中文目录", decoded["root"])
            self.assertNotIn("\ufffd", decoded["root"])
            self.assertEqual(identity["root"], decoded["root"])
            self.assertTrue(Path(identity["script"]).exists())

    def test_bom_tolerated(self):
        """记事本另存会加 BOM，读侧必须能吃掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            cn_dir = Path(tmp) / "中文目录"
            cn_dir.mkdir()
            pid_file = cn_dir / "daemon.pid"
            payload = {"schema": 2, "pid": 4321, "script": str(cn_dir / "daemon.py")}
            pid_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8-sig")
            with self._patched(pid_file, cn_dir):
                identity = daemon_ctl.read_identity()
            self.assertEqual(identity["pid"], 4321)
            self.assertIn("中文目录", identity["script"])

    def test_legacy_numeric_pidfile_still_read(self):
        """旧版单行数字 pidfile 必须继续可读，不能被编码改动破坏。"""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "daemon.pid"
            pid_file.write_text("2468", encoding="ascii")
            with self._patched(pid_file, Path(tmp)):
                identity = daemon_ctl.read_identity()
            self.assertEqual(identity, {"schema": 1, "pid": 2468})


# ------------------------------------------------- 2. 孤儿清理的终止边界


ROOT = str(daemon_ctl.ROOT)


def _proc(pid, ppid, name, cmd, exe, created="2026-10-28T10:00:00+08:00"):
    return {"pid": pid, "ppid": ppid, "name": name, "cmd": cmd, "exe": exe, "created": created}


def _owned_litellm(pid=500, ppid=400):
    return _proc(
        pid, ppid, "litellm.exe",
        f'"{ROOT}\\.venv\\Scripts\\litellm.exe" --config {ROOT}\\config.yaml '
        f'--host 127.0.0.1 --port 4000',
        f"{ROOT}\\.venv\\Scripts\\litellm.exe",
    )


def _unrelated_uvicorn(pid=400, ppid=300):
    return _proc(
        pid, ppid, "uvicorn.exe",
        "C:\\dev\\venv\\Scripts\\uvicorn.exe main:app --reload --port 8100",
        "C:\\dev\\venv\\Scripts\\uvicorn.exe",
    )


class OrphanCleanupTests(unittest.TestCase):
    """P1：只校验监听者归属，_pick_kill_root 却可能选中无关的 uvicorn 祖先。

    所有终止相关入口（_kill_tree / os.kill）都换成记录器，测试不真杀进程。
    """

    def _run(self, chain, *, answer=True, allow_prompt=True, later_chain=None):
        killed: list[int] = []
        calls = {"chain": 0}
        orig = (
            daemon_ctl.port_owner, daemon_ctl.process_chain, daemon_ctl._kill_tree,
            daemon_ctl._ask, daemon_ctl.pid_alive,
        )

        def fake_chain(pid):
            calls["chain"] += 1
            if later_chain is not None and calls["chain"] > 1:
                return later_chain
            for row in chain:
                if row["pid"] == pid:
                    return [row] + [r for r in chain if r["ppid"] == row["pid"]]
            return []

        daemon_ctl.port_owner = lambda port: chain[0]["pid"] if chain else None
        daemon_ctl.process_chain = fake_chain
        daemon_ctl._kill_tree = lambda pid: (killed.append(pid) or True)
        daemon_ctl._ask = lambda prompt: answer
        daemon_ctl.pid_alive = lambda pid: False
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = daemon_ctl.handle_port_holder(4000, allow_prompt=allow_prompt)
        finally:
            (daemon_ctl.port_owner, daemon_ctl.process_chain, daemon_ctl._kill_tree,
             daemon_ctl._ask, daemon_ctl.pid_alive) = orig
        return rc, killed, buf.getvalue()

    def test_unrelated_uvicorn_parent_never_killed(self):
        """归属明确的监听者挂在无关 uvicorn 下：只清监听者，不动父进程与其子进程。"""
        chain = [_owned_litellm(), _unrelated_uvicorn(), _proc(300, 1, "python.exe",
                                                               "C:\\dev\\python.exe manage.py",
                                                               "C:\\dev\\python.exe")]
        rc, killed, out = self._run(chain)
        self.assertEqual(rc, 0, out)
        self.assertEqual(killed, [500])
        self.assertNotIn(400, killed)
        self.assertNotIn(300, killed)

    def test_unowned_listener_refused_without_killing(self):
        """监听者不归属本脚手架：整条链都不许动。"""
        chain = [_proc(500, 400, "litellm.exe", "C:\\other\\litellm.exe --port 4000",
                       "C:\\other\\litellm.exe"),
                 _unrelated_uvicorn()]
        rc, killed, out = self._run(chain)
        self.assertEqual(rc, 1)
        self.assertEqual(killed, [])
        self.assertIn("拒绝自动清理", out)

    def test_sibling_directory_is_not_owned(self):
        """路径边界：`...-old` 与 `...-backup` 不是本实例。"""
        sibling = ROOT.rstrip("\\/") + "-old"
        ok, why = daemon_ctl.belongs_to_scaffold(
            _proc(500, 1, "litellm.exe", f"{sibling}\\litellm.exe", f"{sibling}\\litellm.exe"))
        self.assertFalse(ok, why)
        ok2, _ = daemon_ctl.belongs_to_scaffold(_owned_litellm())
        self.assertTrue(ok2)

    def test_revalidate_blocks_pid_reuse(self):
        """选中根节点后 pid 被复用（命令行变了）：复核失败，一个都不杀。"""
        chain = [_owned_litellm()]
        reused = [_proc(500, 1, "notepad.exe", "C:\\Windows\\notepad.exe", "C:\\Windows\\notepad.exe")]
        rc, killed, out = self._run(chain, later_chain=reused)
        self.assertEqual(rc, 1)
        self.assertEqual(killed, [])
        self.assertIn("复核", out)

    def test_non_interactive_does_not_kill(self):
        chain = [_owned_litellm()]
        rc, killed, _ = self._run(chain, allow_prompt=False)
        self.assertEqual(rc, 1)
        self.assertEqual(killed, [])

    def test_declined_prompt_does_not_kill(self):
        chain = [_owned_litellm()]
        rc, killed, _ = self._run(chain, answer=False)
        self.assertEqual(rc, 1)
        self.assertEqual(killed, [])

    def test_no_kill_root_when_only_unknown_ancestors(self):
        """监听者归属明确但不像代理、且自身不可作根时，返回 None 而不是祖先。"""
        chain = [_proc(500, 400, "python.exe", f'"{ROOT}\\.venv\\Scripts\\python.exe" tool.py',
                       f"{ROOT}\\.venv\\Scripts\\python.exe"),
                 _unrelated_uvicorn()]
        # 监听者本人属于本脚手架 → 退化为它自身，仍然不会碰 400
        self.assertEqual(daemon_ctl._pick_kill_root(chain, 500), 500)
        # 监听者不归属时，链上没有任何合法候选
        chain[0] = _proc(500, 400, "python.exe", "C:\\other\\tool.py", "C:\\other\\python.exe")
        self.assertIsNone(daemon_ctl._pick_kill_root(chain, 500))


# ------------------------------------------------------- 3. 脱敏不改原对象


@requires_guard
class DumpRedactionTests(unittest.TestCase):
    """P2：_redact_deep 原地改写 image.data，转储一开就把发往上游的请求弄坏。"""

    def _payload(self):
        b64 = "QUJDRA" * 1200  # 8400 字符，超过 4096 折叠阈值
        return {
            "model": "glm-5.3",
            "headers": {"authorization": "Bearer sk-live-secret"},
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "这张图是什么"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": b64}},
                ]},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": f"data:image/png;base64,{b64}"},
                ]},
            ],
        }

    def test_dump_keeps_request_intact(self):
        payload = self._payload()
        snapshot = copy.deepcopy(payload)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dump.json"
            orig_file, orig_flag = bg.DUMP_FILE, bg.DUMP_ENABLED
            bg.DUMP_FILE, bg.DUMP_ENABLED = target, True
            try:
                bg._dump(payload)
                text = target.read_text(encoding="utf-8")
            finally:
                bg.DUMP_FILE, bg.DUMP_ENABLED = orig_file, orig_flag

        # 原请求必须与转储前深等：base64 一个字节都不能被换掉
        self.assertEqual(payload, snapshot)
        self.assertEqual(payload["messages"][0]["content"][1]["source"]["data"],
                         snapshot["messages"][0]["content"][1]["source"]["data"])
        self.assertEqual(payload["messages"][1]["content"][0]["image_url"]["url"],
                         snapshot["messages"][1]["content"][0]["image_url"]["url"])
        # 落盘内容必须已脱敏
        self.assertIn("redacted image base64", text)
        self.assertIn("redacted image url", text)
        self.assertNotIn("QUJDRA" * 100, text)
        self.assertNotIn("sk-live-secret", text)

    def test_redact_returns_fresh_containers(self):
        block = {"type": "image", "source": {"type": "base64", "data": "A" * 6000}}
        out = bg._redact_deep(block)
        self.assertIsNot(out, block)
        self.assertIsNot(out["source"], block["source"])
        self.assertEqual(block["source"]["data"], "A" * 6000)
        self.assertIn("redacted image base64", out["source"]["data"])

    def test_redact_openai_string_image_url(self):
        block = {"type": "image_url", "image_url": "data:image/png;base64," + "B" * 5000}
        out = bg._redact_deep(block)
        self.assertTrue(out["image_url"].startswith("<redacted image,"))
        self.assertTrue(block["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(len(block["image_url"]), 5022)

    def test_redact_text_truncation_keeps_input(self):
        value = {"k": "x" * 10000}
        out = bg._redact_deep(value)
        self.assertIn("truncated", out["k"])
        self.assertEqual(len(value["k"]), 10000)


# --------------------------------------------------- 4. 身份证据必须完整


class IdentityEvidenceTests(unittest.TestCase):
    """P2：exe / created 取不到时旧实现跳过比较，仍返回"核对通过"。"""

    def _identity(self, **over):
        data = {
            "schema": 2,
            "pid": 1000,
            "script": f"{ROOT}\\daemon.py",
            "exe": f"{ROOT}\\.venv\\Scripts\\pythonw.exe",
            "root": ROOT,
            "created": "2026-10-28T13:52:11+08:00",
        }
        data.update(over)
        return {k: v for k, v in data.items() if v is not None}

    def _row(self, **over):
        row = _proc(1000, 1, "pythonw.exe",
                    f'"{ROOT}\\.venv\\Scripts\\pythonw.exe" daemon.py',
                    f"{ROOT}\\.venv\\Scripts\\pythonw.exe",
                    created="2026-10-28T13:52:11.9876540+08:00")
        row.update(over)
        return row

    def test_complete_match_passes(self):
        ok, why = daemon_ctl.verify_identity(self._identity(), self._row())
        self.assertTrue(ok, why)

    def test_venv_launcher_image_path_mismatch_still_passes(self):
        """venv 的 python.exe 是转发器：镜像路径是基础解释器，不能据此判失败。

        实测（Windows）：sys.executable = <venv>/Scripts/python.exe，
        Win32_Process.ExecutablePath = .../binaries/python/versions/x/python.exe。
        硬比对镜像路径会把所有 venv 安装的 stop 全拒掉。
        """
        ident = self._identity()
        row = self._row(exe="C:\\Python\\base\\python.exe")
        ok, why = daemon_ctl.verify_identity(ident, row)
        self.assertTrue(ok, why)

    def test_command_line_interpreter_differs_refused(self):
        """命令行里的解释器与记录不符：这才是"换了进程"的硬证据。"""
        row = self._row(cmd=f'"C:\\other\\python.exe" daemon.py')
        ok, why = daemon_ctl.verify_identity(self._identity(), row)
        self.assertFalse(ok)
        self.assertIn("解释器路径不符", why)

    def test_identity_without_exe_refused(self):
        ok, why = daemon_ctl.verify_identity(self._identity(exe=None), self._row())
        self.assertFalse(ok)
        self.assertIn("exe", why)

    def test_identity_without_created_refused(self):
        ok, why = daemon_ctl.verify_identity(self._identity(created=None), self._row())
        self.assertFalse(ok)
        self.assertIn("created", why)

    def test_identity_without_root_refused(self):
        ok, why = daemon_ctl.verify_identity(self._identity(root=None), self._row())
        self.assertFalse(ok)
        self.assertIn("root", why)

    def test_process_without_exe_refused(self):
        ok, why = daemon_ctl.verify_identity(self._identity(), self._row(exe=""))
        self.assertFalse(ok)
        self.assertIn("可执行路径", why)

    def test_process_without_created_refused(self):
        ok, why = daemon_ctl.verify_identity(self._identity(), self._row(created=""))
        self.assertFalse(ok)
        self.assertIn("启动时间", why)

    def test_process_with_malformed_created_refused(self):
        ok, why = daemon_ctl.verify_identity(self._identity(), self._row(created="not-a-time"))
        self.assertFalse(ok)
        self.assertIn("无法解析", why)

    def test_identity_with_malformed_created_refused(self):
        ok, why = daemon_ctl.verify_identity(self._identity(created="???"), self._row())
        self.assertFalse(ok)
        self.assertIn("无法解析", why)

    def test_process_without_cmd_refused(self):
        ok, why = daemon_ctl.verify_identity(self._identity(), self._row(cmd=""))
        self.assertFalse(ok)
        self.assertIn("命令行", why)

    def test_root_mismatch_refused(self):
        ok, why = daemon_ctl.verify_identity(self._identity(root=f"{ROOT}-old"), self._row())
        self.assertFalse(ok)
        self.assertIn("另一个安装目录", why)

    def test_bad_schema_refused(self):
        ok, why = daemon_ctl.verify_identity(self._identity(schema="two"), self._row())
        self.assertFalse(ok)
        self.assertIn("schema", why)

    def test_legacy_uses_limited_path_not_silent_downgrade(self):
        """旧格式走显式受限校验；新格式缺字段绝不能降级成旧校验。"""
        legacy_ok, _ = daemon_ctl.verify_identity({"schema": 1, "pid": 1000}, self._row())
        self.assertTrue(legacy_ok)
        missing_created = self._identity(created=None)
        ok, why = daemon_ctl.verify_identity(missing_created, self._row())
        self.assertFalse(ok)
        self.assertIn("身份记录不完整", why)


# ----------------------------------------------- 5. 转储配置传递到子进程


class DumpSettingsPropagationTests(unittest.TestCase):
    """P2：.env 里写了 BRIDGE_DUMP_* 也不生效——prepare() 没导出。"""

    MASTER_KEY = "sk-bridge-integrationkey0001"

    def setUp(self):
        self._env_backup = dict(os.environ)
        self._env_file = bridge_env.ENV_FILE

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        bridge_env.ENV_FILE = self._env_file

    def _base_env(self, **extra):
        env = {
            "UPSTREAM_BASE_URL": "https://upstream.example.com",
            "UPSTREAM_MODELS": "glm-5.3",
            "UPSTREAM_API_KEY": "sk-upstream-test-0001",
            "PROXY_MASTER_KEY": self.MASTER_KEY,
            "PROXY_PORT": "4020",
        }
        env.update(extra)
        return env

    def test_env_file_values_reach_child(self):
        env = self._base_env(BRIDGE_DUMP_ENABLED="true", BRIDGE_DUMP_MAX_CHARS="4096")
        boot_common.prepare(env)
        self.assertEqual(os.environ["BRIDGE_DUMP_ENABLED"], "true")
        self.assertEqual(os.environ["BRIDGE_DUMP_MAX_CHARS"], "4096")

    def test_default_off_when_absent(self):
        boot_common.prepare(self._base_env())
        self.assertEqual(os.environ["BRIDGE_DUMP_ENABLED"], "false")
        self.assertEqual(os.environ["BRIDGE_DUMP_MAX_CHARS"], str(boot_common.DUMP_MAX_CHARS_DEFAULT))

    def test_invalid_limit_handled_explicitly(self):
        for raw in ("0", "-1", "abc", ""):
            enabled, limit = boot_common.resolve_dump_settings(
                {"BRIDGE_DUMP_ENABLED": "true", "BRIDGE_DUMP_MAX_CHARS": raw})
            self.assertTrue(enabled)
            self.assertEqual(limit, boot_common.DUMP_MAX_CHARS_DEFAULT, raw)
        _, limit = boot_common.resolve_dump_settings({"BRIDGE_DUMP_MAX_CHARS": "512"})
        self.assertEqual(limit, 512)

    def test_disabling_does_not_leave_stale_true(self):
        os.environ["BRIDGE_DUMP_ENABLED"] = "true"  # 上一个终端残留
        boot_common.apply_dump_settings({})
        self.assertEqual(os.environ["BRIDGE_DUMP_ENABLED"], "false")

    def test_environment_override_wins_over_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "UPSTREAM_BASE_URL=https://upstream.example.com\n"
                "UPSTREAM_MODELS=glm-5.3\n"
                "BRIDGE_DUMP_ENABLED=false\n"
                "BRIDGE_DUMP_MAX_CHARS=8192\n",
                encoding="utf-8",
            )
            bridge_env.ENV_FILE = env_file
            os.environ["BRIDGE_DUMP_ENABLED"] = "true"
            os.environ["BRIDGE_DUMP_MAX_CHARS"] = "2048"
            env = bridge_env.ensure_env()
            enabled, limit = boot_common.apply_dump_settings(env)
        self.assertTrue(enabled)
        self.assertEqual(limit, 2048)
        self.assertEqual(os.environ["BRIDGE_DUMP_ENABLED"], "true")

    def test_dump_file_path_propagated(self):
        boot_common.apply_dump_settings({"BRIDGE_DUMP_FILE": "logs/custom_dump.json"})
        self.assertEqual(os.environ["BRIDGE_DUMP_FILE"], "logs/custom_dump.json")


def main() -> int:
    if not HAS_GUARD:
        print(f"[提示] bridge_guard 无法导入（{GUARD_IMPORT_ERROR}），脱敏相关用例将跳过。")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        IdentityEncodingTests, OrphanCleanupTests, DumpRedactionTests,
        IdentityEvidenceTests, DumpSettingsPropagationTests,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
