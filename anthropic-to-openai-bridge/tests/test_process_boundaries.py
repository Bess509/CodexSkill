"""进程身份边界回归：仅用临时文件和 mock，不终止真实进程。"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets" / "scaffold"))
import daemon_ctl as ctl
import daemon


class ProcessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "中文 bridge"
        self.root.mkdir()
        self.script = self.root / "daemon.py"
        self.script.write_text("# isolated fixture\n", encoding="utf-8")
        self.exe = str(self.root / ".venv" / "Scripts" / "python.exe")
        self.root_patch = patch.object(ctl, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def identity(self):
        return {"schema": 2, "pid": 424242, "root": str(self.root),
                "script": str(self.script), "exe": self.exe,
                "created": "2026-10-28T10:00:00+08:00"}

    def row(self, **extra):
        row = {"pid": 424242, "ppid": 1, "name": "python.exe",
               "cmd": f'"{self.exe}" "{self.script}"', "exe": self.exe,
               "created": "2026-10-28T10:00:00.000000+08:00"}
        row.update(extra)
        return row

    def revalidate(self, snapshot, current):
        with patch.object(ctl, "process_chain", return_value=[current]):
            return ctl.revalidate_target(424242, snapshot)

    def test_same_pid_command_and_exe_with_new_start_is_rejected(self):
        old = self.row()
        for created in ("2026-10-28T12:00:00+08:00", "2026-10-28T10:00:00.000001+08:00"):
            with self.subTest(created=created):
                ok, reason = self.revalidate(old, self.row(created=created))
                self.assertFalse(ok)
                self.assertIn("启动时间", reason)

    def test_same_creation_in_different_timezone_passes(self):
        self.assertTrue(self.revalidate(self.row(), self.row(created="2026-10-28T02:00:00Z"))[0])

    def test_missing_or_invalid_birth_evidence_is_rejected(self):
        for bad in ("", "invalid", "2026-10-28T10:00:00"):
            for missing_snapshot in (False, True):
                with self.subTest(bad=bad, snapshot=missing_snapshot):
                    old, current = self.row(), self.row()
                    (old if missing_snapshot else current)["created"] = bad
                    self.assertFalse(self.revalidate(old, current)[0])

    def test_missing_snapshot_or_wrong_pid_is_rejected(self):
        self.assertFalse(self.revalidate(None, self.row())[0])
        self.assertFalse(self.revalidate(self.row(pid=1), self.row())[0])
        self.assertFalse(self.revalidate(self.row(), self.row(pid=1))[0])

    def test_missing_exe_or_command_is_rejected(self):
        for field in ("cmd", "exe"):
            for which in ("old", "new"):
                with self.subTest(field=field, which=which):
                    old, current = self.row(), self.row()
                    (old if which == "old" else current)[field] = ""
                    self.assertFalse(self.revalidate(old, current)[0])

    def test_reused_pid_during_cleanup_never_reaches_kill(self):
        old, new = self.row(), self.row(created="2026-10-28T12:00:00+08:00")
        with patch.object(ctl, "port_owner", return_value=424242), \
             patch.object(ctl, "process_chain", side_effect=[[old], [new]]), \
             patch.object(ctl, "_ask", return_value=True), \
             patch.object(ctl, "_kill_tree") as kill, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ctl.handle_port_holder(41000, allow_prompt=True), 1)
            kill.assert_not_called()

    def test_traversal_outside_root_is_not_owned_or_selected(self):
        raw = self.root / ".." / "other" / "uvicorn.exe"
        row = self.row(name="uvicorn.exe", exe=str(raw.resolve()), cmd=f'"{raw}" main:app')
        self.assertFalse(ctl.belongs_to_scaffold(row)[0])
        self.assertIsNone(ctl._pick_kill_root([row], row["pid"]))

    def test_normalized_path_still_inside_root_passes(self):
        raw = self.root / "sub" / ".." / "daemon.py"
        self.assertTrue(ctl._is_under(raw, str(self.root)))
        self.assertTrue(ctl.verify_identity(self.identity(), self.row(cmd=f'"{self.exe}" "{raw}"'))[0])

    def test_sibling_and_relative_paths_are_not_owned(self):
        for raw in (str(self.root) + "-backup/uvicorn.exe", "../uvicorn.exe", "daemon.py"):
            self.assertFalse(ctl._is_under(raw, str(self.root)), raw)

    def test_directory_link_outside_root_is_not_owned(self):
        # Windows junctions and POSIX symlinks are both resolved by Path.resolve.
        outside = Path(self.tmp.name) / "other"
        outside.mkdir()
        link = self.root / "linked"
        if sys.platform == "win32":
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                           check=True, capture_output=True, creationflags=ctl.NO_WINDOW)
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            self.assertFalse(ctl._is_under(link / "uvicorn.exe", str(self.root)))
        finally:
            link.rmdir() if sys.platform == "win32" else link.unlink()

    def test_other_script_suffix_is_rejected(self):
        for suffix in (".backup", "x", ".old"):
            with self.subTest(suffix=suffix):
                row = self.row(cmd=f'"{self.exe}" "{self.script}{suffix}"')
                self.assertFalse(ctl.verify_identity(self.identity(), row)[0])

    def test_daemon_path_as_data_argument_is_rejected(self):
        row = self.row(cmd=f'"{self.exe}" other.py "{self.script}"')
        self.assertFalse(ctl.verify_identity(self.identity(), row)[0])

    def test_module_and_code_commands_are_rejected(self):
        for mode in ("-m other", '-c "pass"'):
            row = self.row(cmd=f'"{self.exe}" {mode} "{self.script}"')
            self.assertFalse(ctl.verify_identity(self.identity(), row)[0])

    def test_absolute_script_in_subdirectory_is_rejected(self):
        row = self.row(cmd=f'"{self.exe}" "{self.root / "sub" / "daemon.py"}"')
        self.assertFalse(ctl.verify_identity(self.identity(), row)[0])

    def test_relative_script_resolved_against_cwd(self):
        for token, cwd, allowed in (
            ("daemon.py", self.root, True),
            ("./daemon.py", self.root, True),
            ("sub/../daemon.py", self.root, True),
            ("daemon.py", self.root / "sub", False),
            ("../daemon.py", self.root / "sub", True),
            ("sub/daemon.py", self.root, False),
        ):
            with self.subTest(token=token, cwd=cwd):
                row = self.row(cmd=f'"{self.exe}" {token}', cwd=str(cwd))
                self.assertEqual(ctl.verify_identity(self.identity(), row)[0], allowed)

    def test_only_simple_relative_form_allowed_without_cwd(self):
        for token, allowed in (("daemon.py", True), ("./daemon.py", True),
                               ("sub/daemon.py", False), ("../daemon.py", False)):
            row = self.row(cmd=f'"{self.exe}" {token}')
            self.assertEqual(ctl.verify_identity(self.identity(), row)[0], allowed, token)

    def test_python_flags_before_script_remain_supported(self):
        for flags in ("-u", "-X utf8 -W ignore", "--"):
            row = self.row(cmd=f'"{self.exe}" {flags} "{self.script}"')
            self.assertTrue(ctl.verify_identity(self.identity(), row)[0], flags)

    def test_legacy_rejects_daemon_as_data_or_wrong_script(self):
        for args in (f'other.py "{self.script}"', f'"{self.script}.backup"'):
            row = self.row(cmd=f'"{self.exe}" {args}')
            self.assertFalse(ctl.verify_identity({"schema": 1, "pid": 424242}, row)[0])

    def test_pidfile_records_os_interpreter_and_birth_evidence(self):
        base_exe = str(Path(self.tmp.name) / "base-python.exe")
        own = self.row(cmd=f'"{base_exe}" "{self.script}"', exe=base_exe)
        target = self.root / "daemon.pid"
        with patch.object(daemon, "ROOT", self.root), \
             patch.object(daemon, "__file__", str(self.script)), \
             patch.object(daemon, "PID_FILE", target), \
             patch.object(daemon, "LOG_DIR", self.root / "logs"), \
             patch.object(daemon.os, "getpid", return_value=own["pid"]), \
             patch.object(ctl, "process_chain", return_value=[own]), \
             patch.object(ctl, "PID_FILE", target):
            daemon.write_pid()
            identity = ctl.read_identity()
        self.assertEqual(identity["exe"], base_exe)
        self.assertEqual(identity["created"], own["created"])
        self.assertTrue(ctl.verify_identity(identity, own)[0])

    def test_pidfile_refuses_missing_os_identity(self):
        target = self.root / "daemon.pid"
        with patch.object(daemon, "PID_FILE", target), \
             patch.object(daemon, "LOG_DIR", self.root / "logs"), \
             patch.object(ctl, "process_chain", return_value=[]):
            with self.assertRaises(RuntimeError):
                daemon.write_pid()
        self.assertFalse(target.exists())


class PosixTimestampTests(unittest.TestCase):
    def test_birth_timestamp_is_stable_across_wall_clock_changes(self):
        stat = "4242 (name with spaces) " + " ".join(["S"] + ["0"] * 18 + ["12345"])

        def proc_file(path, *args, **kwargs):
            if path.name == "stat" and path.parent.name == "4242":
                return stat
            return "cpu 0 0 0 0\nbtime 1700000000\n"

        with patch.object(Path, "read_text", proc_file), \
             patch.object(ctl.os, "sysconf", return_value=100, create=True), \
             patch.object(ctl.time, "time", side_effect=[1700000000, 1800000000]):
            first = ctl._posix_created(4242)
            second = ctl._posix_created(4242)
        self.assertEqual(first, second)
        self.assertEqual(int(datetime.fromisoformat(first).timestamp()), 1700000123)

    def test_unavailable_proc_evidence_returns_empty(self):
        with patch.object(Path, "read_text", side_effect=OSError):
            self.assertEqual(ctl._posix_created(4242), "")


if __name__ == "__main__":
    unittest.main()
