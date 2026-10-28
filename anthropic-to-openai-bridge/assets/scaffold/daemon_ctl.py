"""后台模式控制工具：查状态、停进程。

用法：
    python daemon_ctl.py status   # 查运行状态与连接信息（URL / Key / 模型）
    python daemon_ctl.py stop     # 停止 daemon 及其全部子进程

孤儿识别：daemon 被强杀或崩溃后，litellm 子进程可能仍然存活并占着端口，
此时 pidfile 指向的 pid 已死，旧逻辑会误判"无需停止"，之后新实例又因端口
冲突起不来，用户陷入"停不掉也起不来"的死角。stop/status 都会探测端口：
     若被占，打印占用者、进程链与清理命令；stop 交互确认后直接清掉。
"""
from __future__ import annotations

import argparse
import base64
import json
import ntpath
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import bridge_env

ROOT = bridge_env.ROOT
LOG_DIR = ROOT / "logs"
PID_FILE = LOG_DIR / "daemon.pid"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 父链回溯到这些系统关键进程就停手，绝不给它们发 taskkill
NEVER_KILL = {
    "system",
    "system idle process",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "explorer.exe",
    "sihost.exe",
    "taskhostw.exe",
    "dwm.exe",
}


# ---------------------------------------------------------------- 基础工具


def _run(cmd: list[str], *, timeout: int = 20) -> tuple[int, str]:
    """跑外部命令，统一按系统代码页解码（中文 Windows 是 GBK）。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:  # noqa: BLE001  # 探测类命令失败一律当作"查不到"
        return -1, ""
    enc = "gbk" if os.name == "nt" else "utf-8"
    return proc.returncode, (proc.stdout or b"").decode(enc, "replace")


TIME_TOLERANCE_SECONDS = 5


def read_identity() -> dict | None:
    """读 pidfile 里的进程身份。兼容旧版单行 pid 格式。

    新版是 JSON：pid / created / exe / script / root / schema。
    旧版只有一行数字，没有身份信息，核对时只能退化为命令行匹配。

    解码统一用 UTF-8（utf-8-sig 顺带吃掉记事本加的 BOM）：身份里是绝对路径，
    安装目录含中文时按 ASCII 读会得到乱码路径，核对必然失败——比误杀好，
    但会把能正常停的进程变成停不掉。旧版纯数字文件是 ASCII 子集，照旧可读。
    """
    try:
        text = PID_FILE.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    if text.isdigit():
        return {"schema": 1, "pid": int(text)}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        return None
    data["pid"] = pid
    return data


def read_pid() -> int | None:
    identity = read_identity()
    return identity["pid"] if identity else None


def _norm_path(value: object) -> str:
    """规范化路径，先解析 .. 与本机链接，再比较目录边界；不要传整条命令行。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    windows_path = os.name == "nt" or bool(ntpath.splitdrive(raw)[0])
    try:
        if Path(raw).is_absolute():
            raw = str(Path(raw).resolve())
        elif windows_path:
            raw = ntpath.normpath(raw)
        else:
            raw = os.path.normpath(raw)
    except (OSError, ValueError, RuntimeError):
        return ""  # 无法解析的路径不能成为归属证据
    normalized = raw.replace("\\", "/").rstrip("/") or "/"
    return normalized.casefold() if windows_path else normalized


_PATH_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _path_tokens(text: object) -> list[str]:
    """把命令行拆成参数 token，去掉引号。

    归属判断必须按 token 边界比路径。直接 `root in cmd` 是子串匹配，
    `D:/work/anthropic-openai-bridge-backup/litellm.exe` 会被判成
    `D:/work/anthropic-openai-bridge` 的成员，清理时就杀错了进程。
    """
    out: list[str] = []
    for match in _PATH_TOKEN_RE.findall(str(text or "")):
        token = match.strip().strip('"').strip("'")
        if token:
            out.append(token)
    return out


def _is_under(value: object, root: str) -> bool:
    """value 是否等于 root 或位于 root 之下（按路径分隔符边界判断）。"""
    v = _norm_path(value)
    root = _norm_path(root)
    if not v or not root or not _is_abs_path(v) or not _is_abs_path(root):
        return False
    return v == root or v.startswith(root.rstrip("/") + "/")


def _cmd_has_root(cmd: object, root: str) -> bool:
    return any(_is_under(token, root) for token in _path_tokens(cmd))


def _is_abs_path(normalized: str) -> bool:
    """归一化后的路径是否为绝对路径（Windows 盘符或 POSIX 根）。"""
    return normalized.startswith("/") or bool(re.match(r"^[a-z]:/", normalized))


def _python_script_arg(tokens: list[str]) -> str | None:
    """只取 Python 实际执行的脚本，不能把脚本后面的数据参数当成执行目标。"""
    index = 1
    while index < len(tokens):
        arg = tokens[index]
        if arg == "--":
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if arg in ("-c", "-m", "-"):
            return None
        if arg in ("-W", "-X", "--check-hash-based-pycs"):
            index += 2
            continue
        if arg in ("-u", "-B", "-E", "-I", "-s", "-S", "-q", "-O", "-OO", "-b", "-bb", "-P", "-v", "-x") or arg.startswith(("-W", "-X")):
            index += 1
            continue
        return None if arg.startswith("-") else arg
    return None


def _script_matches(token: str | None, script: str, info: dict) -> bool:
    """按完整脚本路径匹配；无 cwd 时仅兼容根目录 daemon.py 的简单相对写法。"""
    if not token:
        return False
    # 判断是否相对必须在规范化之前，否则 ../daemon.py 会丢掉原始上下文。
    raw = token.replace("\\", "/")
    if _is_abs_path(raw.casefold()):
        return _norm_path(token) == script
    cwd = str(info.get("cwd") or "").strip()
    if cwd:
        return _norm_path(Path(cwd) / token) == script
    simple = raw.removeprefix("./")
    if os.name == "nt":
        simple = simple.casefold()
    return (
        simple == script.rsplit("/", 1)[-1]
        and "/" not in simple
        and Path(script).is_file()
    )


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.utcoffset() is not None else None
    except ValueError:
        return None


def _schema_of(identity: dict) -> int | None:
    """读取身份格式版本；缺省按旧版 1 处理，非法值返回 None。"""
    raw = identity.get("schema", 1)
    if raw is None:
        return 1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _verify_legacy(info: dict) -> tuple[bool, str]:
    """旧版单行 pidfile 的受限校验路径。

    旧格式没有 exe/script/created，做不了完整核对，只能要求「命令行里确实有
    daemon.py，且解释器或命令行落在本脚手架目录下」。这是刻意保留的降级路径，
    仅对 schema<2 生效；新格式绝不走这里，避免用弱校验冒充强校验。
    """
    root = _norm_path(ROOT)
    raw_cmd = str(info.get("cmd") or "")
    exe = _norm_path(info.get("exe"))
    if not raw_cmd.strip():
        return False, "旧格式 pidfile 且取不到进程命令行，无法确认身份"
    tokens = _path_tokens(raw_cmd)
    if not _script_matches(_python_script_arg(tokens), _norm_path(ROOT / "daemon.py"), info):
        return False, "旧格式 pidfile 且命令行里没有 daemon.py，拒绝自动终止"
    if _cmd_has_root(raw_cmd, root) or _is_under(exe, root):
        return True, "旧格式 pidfile（受限校验）：命令行指向本脚手架的 daemon.py"
    return False, "旧格式 pidfile 且命令行/解释器都不在本脚手架下"


def _verify_current(identity: dict, info: dict) -> tuple[bool, str]:
    """新版身份记录的完整核对：缺任何一项证据都拒绝，不静默降级。

    pid 复用是最难识别的一类误杀——命令行、解释器路径碰巧都能对上时，
    唯一的硬证据是启动时间。取不到启动时间就必须拒绝，不能"没证据就放行"。
    """
    missing = [
        field for field in ("script", "root", "exe", "created")
        if not str(identity.get(field) or "").strip()
    ]
    if missing:
        return False, (
            f"pidfile 身份记录不完整，缺少 {'/'.join(missing)}，"
            f"无法确认是否为原进程，已拒绝自动终止"
        )

    want_script = _norm_path(identity.get("script"))
    want_root = _norm_path(identity.get("root"))
    want_exe = _norm_path(identity.get("exe"))
    want_created = _parse_time(identity.get("created"))
    if want_created is None:
        return False, f"pidfile 的 created 字段无法解析：{identity.get('created')!r}，已拒绝自动终止"

    our_root = _norm_path(ROOT)
    if our_root and want_root != our_root:
        return False, (
            f"pidfile 记录的是另一个安装目录（{identity.get('root')}），"
            f"当前是 {ROOT}，已拒绝自动终止"
        )
    if not _is_under(want_script, want_root):
        return False, f"记录的脚本路径不在记录的根目录之下（{want_script}），已拒绝自动终止"

    raw_cmd = str(info.get("cmd") or "")
    got_exe = _norm_path(info.get("exe"))
    created_raw = str(info.get("created") or "").strip()
    got_created = _parse_time(info.get("created"))

    if not raw_cmd.strip():
        return False, "取不到进程命令行，无法确认跑的是本脚手架的 daemon.py"
    if not got_exe:
        return False, "取不到进程可执行路径，无法完成身份核对（已拒绝自动终止）"
    if not created_raw:
        return False, "取不到进程启动时间，无法排除 pid 复用（已拒绝自动终止）"
    if got_created is None:
        return False, f"进程启动时间无法解析：{created_raw!r}（已拒绝自动终止）"

    # 解释器路径的比对基准是「命令行里的第一个 token」，不是进程的镜像路径：
    # venv 的 python.exe 是转发器，启动后 Win32_Process.ExecutablePath 报的是
    # 基础解释器（如 .../binaries/python/versions/3.13.12/python.exe），Linux 上
    # /proc/<pid>/exe 也会解析到 venv 之外的真实二进制。拿 sys.executable 与之
    # 等值比较，会把所有 venv 安装全判成"解释器路径不符"（实测踩到过）。
    # got_exe 仍然要求存在：取不到镜像路径说明进程信息残缺，不应放行。
    tokens = _path_tokens(raw_cmd)
    cmd_exe = _norm_path(tokens[0]) if tokens else ""
    if not cmd_exe:
        return False, "取不到命令行里的解释器路径，无法确认身份（已拒绝自动终止）"
    if want_exe != cmd_exe:
        return False, f"解释器路径不符（记录 {want_exe}，命令行实际 {cmd_exe}）"

    # 脚本路径出现在命令行里是硬证据；start_background.bat 用的是相对路径
    # `daemon.py`，此时没有目录信息可比。分两级处理：
    #   1. 能拿到进程工作目录（POSIX /proc/<pid>/cwd）时，要求它就是脚手架根目录；
    #   2. 拿不到时，要求解释器与本安装一致（前面已核对）+ 记录的脚本文件确实存在。
    # 绝对路径写法（C:\other\daemon.py）永远不会被这条退让接受。
    if not _script_matches(_python_script_arg(tokens), want_script, info):
        return False, f"命令行与记录的脚本路径不符，疑似 pid 被复用（记录 {want_script}）"

    drift = abs((got_created - want_created).total_seconds())
    if drift > TIME_TOLERANCE_SECONDS:
        return False, f"进程启动时间与记录相差 {drift:.0f} 秒，pid 已被复用"

    return True, "身份核对通过（命令行 + 解释器路径 + 启动时间）"


def verify_identity(identity: dict, info: dict | None) -> tuple[bool, str]:
    """核对 pidfile 里记录的进程是否仍是它自称的那个进程。

    返回 (是否确认, 理由)。任何一项无法确认都返回 False——误杀一个无关进程的
    代价，远高于让用户手动执行一条 taskkill。
    """
    if not info:
        return False, "无法读取该 pid 的进程信息（权限不足或进程已退出）"
    pid = identity.get("pid")
    if info.get("pid") != pid:
        return False, f"进程号不符（pidfile={pid}，实际={info.get('pid')}）"

    schema = _schema_of(identity)
    if schema is None:
        return False, f"pidfile 的 schema 字段非法（{identity.get('schema')!r}），已拒绝自动终止"
    if schema < 2:
        return _verify_legacy(info)
    return _verify_current(identity, info)


def belongs_to_scaffold(row: dict | None) -> tuple[bool, str]:
    """判断某进程是否属于本脚手架，用于决定能不能自动清理孤儿。

    按路径边界匹配可执行文件与命令行参数，不做子串匹配：同级的
    `anthropic-openai-bridge-old` 之类的目录不属于本实例。
    """
    if not row:
        return False, "取不到该进程信息"
    root = _norm_path(ROOT)
    if not root:
        return False, "无法确定本脚手架目录"
    if _is_under(row.get("exe"), root):
        return True, ""
    if _cmd_has_root(row.get("cmd"), root):
        return True, ""
    return False, f"可执行路径与命令行参数都不在 {ROOT} 之下"


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, timeout=10, creationflags=NO_WINDOW,
            ).stdout
            return str(pid) in out.decode("gbk", "replace")
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_port() -> int:
    try:
        env = bridge_env.ensure_env()
    except Exception:  # noqa: BLE001  # .env 缺失/损坏时退回默认端口
        env = {}
    return bridge_env.as_int(env.get("PROXY_PORT"), 4000)


# ------------------------------------------------------------ 端口占用探测


def port_owner(port: int) -> int | None:
    """返回监听指定端口的进程 pid；端口空闲或探测失败返回 None。"""
    if os.name == "nt":
        netstat = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "netstat.exe")
        if not os.path.exists(netstat):
            netstat = "netstat"
        _, text = _run([netstat, "-ano"], timeout=20)
        for line in text.splitlines():
            # TCP    127.0.0.1:4000    0.0.0.0:0    LISTENING    1234
            parts = line.split()
            if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
                continue
            if parts[1].rsplit(":", 1)[-1] != str(port):
                continue
            try:
                return int(parts[4])
            except ValueError:
                continue
        return None

    # POSIX：优先 ss（无需 root 也能看到自己的进程），退回 lsof
    code, text = _run(["ss", "-lptnH"], timeout=15)
    if code == 0:
        for line in text.splitlines():
            if f":{port}" not in line:
                continue
            match = re.search(r"pid=(\d+)", line)
            if match:
                return int(match.group(1))
    code, text = _run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], timeout=15)
    if code == 0:
        for token in text.split():
            if token.isdigit():
                return int(token)
    return None


def _nt_chain(pid: int, depth: int = 6) -> list[dict]:
    """用 PowerShell 取进程自身与祖先链（父进程已退出时自然断链）。"""
    script = f"""$ErrorActionPreference='SilentlyContinue'
$cur = {pid}
$rows = @()
for ($i = 0; $i -lt {depth}; $i++) {{
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$cur"
  if (-not $p) {{ break }}
  $created = ''
  if ($p.CreationDate) {{ $created = $p.CreationDate.ToString('o') }}
  $rows += [pscustomobject]@{{ pid = [int]$p.ProcessId; ppid = [int]$p.ParentProcessId; name = [string]$p.Name; cmd = [string]$p.CommandLine; exe = [string]$p.ExecutablePath; created = $created }}
  if ($p.ParentProcessId -le 4) {{ break }}
  $cur = $p.ParentProcessId
}}
ConvertTo-Json -InputObject @($rows) -Compress -Depth 4
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    powershell = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )
    if not os.path.exists(powershell):
        powershell = "powershell"
    _, text = _run([powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], timeout=25)
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [row for row in data if isinstance(row, dict) and row.get("pid")]


def _posix_created(pid: int) -> str:
    """用 /proc 的 starttime 与内核 btime 得到稳定的进程启动时间。

    daemon 写身份时会记录 created；核对要求这一项必须有证据，不能因为
    POSIX 侧"不好算"就跳过——跳过等于让 pid 复用无从识别。
    """
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        # comm 里可能带空格和括号，从最后一个 ')' 之后开始数字段：
        # 第 3 段 state 是 fields[0]，starttime 是第 22 段，即 fields[19]
        tail = stat_text[stat_text.rfind(")") + 2:]
        fields = tail.split()
        starttime = int(fields[19])
        hz = os.sysconf("SC_CLK_TCK")
        # btime 是稳定的内核启动时刻。time.time() - uptime 的采样抖动会让同一
        # 进程的 created 在两次复核间变化一秒，无法做严格的进程身份比较。
        boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
        return datetime.fromtimestamp(boot + starttime / hz).astimezone().isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001  # 取不到就返回空，核对侧会据此拒绝
        return ""


def _posix_chain(pid: int, depth: int = 6) -> list[dict]:
    rows: list[dict] = []
    cur = pid
    for _ in range(depth):
        try:
            status = Path(f"/proc/{cur}/status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            break
        name = ""
        ppid = ""
        for line in status.splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("PPid:"):
                ppid = line.split(":", 1)[1].strip()
        try:
            cmd = Path(f"/proc/{cur}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            cmd = ""
        try:
            exe = os.readlink(f"/proc/{cur}/exe")
        except OSError:
            exe = ""
        try:
            cwd = os.readlink(f"/proc/{cur}/cwd")
        except OSError:
            cwd = ""
        parent = int(ppid) if ppid.isdigit() else 0
        rows.append({"pid": cur, "ppid": parent, "name": name, "cmd": cmd, "exe": exe,
                     "cwd": cwd, "created": _posix_created(cur)})
        if parent <= 1:
            break
        cur = parent
    return rows


def process_chain(pid: int) -> list[dict]:
    """返回 [自身, 父, 祖父, ...]。"""
    return _nt_chain(pid) if os.name == "nt" else _posix_chain(pid)


def wait_port_free(port: int, timeout: float = 3.0) -> bool:
    """等端口从 LISTENING 消失。

    正常停机时子进程退出到端口释放有毫秒级延迟，直接判定会把正常停机
    误报成孤儿；只有真孤儿才会等满 timeout。
    """
    deadline = time.time() + timeout
    while True:
        if port_owner(port) is None:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.4)


# ------------------------------------------------------------ 孤儿识别与清理


def _looks_like_bridge(row: dict) -> bool:
    """判定进程是否属于本代理的进程链，避免误杀占用同端口的无关程序。"""
    name = (row.get("name") or "").lower()
    cmd = (row.get("cmd") or "").lower()
    if name.startswith("litellm"):
        return True
    if "litellm" in cmd or "uvicorn" in cmd or "proxy_cli" in cmd:
        return True
    return False


def _pick_kill_root(chain: list[dict], owner: int) -> int | None:
    """从链里挑一个可安全终止的根节点；挑不出归属明确的就返回 None。

    只从"确认属于本脚手架"的进程里挑，还要求它像代理进程（litellm / uvicorn /
    本脚手架的脚本）。父进程若是无关的 uvicorn 开发服务器，即便命令行里有
    uvicorn 字样也不会被选中——`/T` 会连带杀掉它的其他子进程，这个代价远高于
    留一条手动清理命令。

    父进程已退出时链断在 litellm 垫片上，那个就是根，`/T` 能带走整棵树；
    都不像代理时退化为监听者本身（前提是监听者归属明确）。
    """
    current = os.getpid()
    fallback: int | None = None
    for row in reversed(chain):
        try:
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid == current:
            continue
        if (row.get("name") or "").lower() in NEVER_KILL:
            continue
        owned, _ = belongs_to_scaffold(row)
        if not owned:
            continue
        if _looks_like_bridge(row):
            return pid
        if pid == owner:
            fallback = pid
    return fallback


def revalidate_target(pid: int, expected: dict | None = None) -> tuple[bool, str]:
    """终止前重新核对目标：归属、系统进程白名单、以及身份是否已变化。

    选中根节点和真正发信号之间隔着用户确认，这期间 pid 可能已被系统复用。
    复核拿不到可比对的证据时同样拒绝——宁可让用户手动 taskkill。
    """
    if pid <= 0:
        return False, "目标 pid 非法"
    if pid == os.getpid():
        return False, "目标是当前进程自身，拒绝终止"
    chain = process_chain(pid)
    row = chain[0] if chain else None
    if not row:
        return False, f"取不到 pid={pid} 的进程信息，无法确认归属"
    if (row.get("name") or "").lower() in NEVER_KILL:
        return False, f"pid={pid} 是系统关键进程，拒绝终止"
    owned, why = belongs_to_scaffold(row)
    if not owned:
        return False, f"pid={pid} 不属于本脚手架（{why}），拒绝终止"

    if not expected or expected.get("pid") != pid or row.get("pid") != pid:
        return False, f"缺少 pid={pid} 的原始身份快照，拒绝自动清理"
    exp_created = _parse_time(expected.get("created"))
    got_created = _parse_time(row.get("created"))
    if exp_created is None or got_created is None:
        return False, f"无法确认 pid={pid} 前后的启动时间，拒绝自动清理"
    # 快照与复核都来自 OS，必须是同一启动时刻；不能使用 pidfile 的秒级容差。
    if exp_created != got_created:
        return False, f"pid={pid} 的启动时间已变化，疑似 pid 复用"
    exp_exe = _norm_path(expected.get("exe"))
    got_exe = _norm_path(row.get("exe"))
    exp_cmd = _path_tokens(expected.get("cmd"))
    got_cmd = _path_tokens(row.get("cmd"))
    if not exp_exe or not got_exe or not exp_cmd or not got_cmd:
        return False, f"无法对比 pid={pid} 前后的完整身份，拒绝自动清理"
    if exp_exe != got_exe:
        return False, f"pid={pid} 的可执行路径已变化，疑似 pid 复用"
    if exp_cmd != got_cmd:
        return False, f"pid={pid} 的命令行已变化，疑似 pid 复用"
    return True, ""


def _ask(prompt: str) -> bool:
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        answer = input(prompt).strip().lower()
    except (EOFError, OSError):
        return False
    return answer in ("y", "yes", "是")


def _kill_tree(pid: int) -> bool:
    print(f"清理 pid={pid} 及其子进程树...")
    if os.name == "nt":
        code, text = _run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=30)
        if text.strip():
            print(text.strip())
        return code == 0
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"已发送 SIGTERM 给 pid={pid}")
        return True
    except OSError as exc:
        print(f"发送信号失败: {exc}")
        return False


def handle_port_holder(port: int, *, allow_prompt: bool) -> int:
    """端口被占但 daemon 已不在时，定位占用者并（可选）清理。

    返回 0 表示端口干净或已清理完毕，1 表示仍有占用未处理。
    清理的门槛是"最终被终止的那个进程确认属于本脚手架"：只校验监听者不够，
    链上更外层的进程可能只是命令行里恰好有 uvicorn/litellm 的无关服务。
    不归属的一律只打印信息，绝不替用户决定要不要杀掉别人的进程。
    """
    owner = port_owner(port)
    if owner is None:
        print(f"端口 {port}: 空闲，无残留监听")
        return 0

    chain = process_chain(owner)
    head = chain[0] if chain else {}
    owned, why_not = belongs_to_scaffold(head)
    root = _pick_kill_root(chain, owner)
    manual = f"taskkill /F /T /PID {owner}" if os.name == "nt" else f"kill -TERM {owner}"

    print()
    if owned:
        print(f"[警告] 端口 {port} 仍被占用，但 daemon 已不在——检测到孤儿代理进程")
    else:
        print(f"[警告] 端口 {port} 被占用，daemon 不在；占用者不是本脚手架的进程")
    print(f"  占用者 : pid={owner}  {head.get('name') or '未知'}")
    if chain:
        print("  进程链 :")
        for index, row in enumerate(chain):
            tag = "监听端口" if index == 0 else f"父级 {index}"
            print(f"           {str(row.get('pid')):>7}  {str(row.get('name') or '?'):<18} {tag}")
    else:
        print("  进程链 : 查询失败（可能权限不足），仅知监听 pid")

    command = (head.get("cmd") or "").strip()
    if command:
        print(f"  命令行 : {command[:150]}{'...' if len(command) > 150 else ''}")
    print(f"  影响   : 代理可能仍在响应，但已无 watchdog；stop 找不到它，")
    print(f"           直接启动新实例会因端口冲突退出。")

    if not owned:
        print(f"  归属   : {why_not}")
        print("  处置   : 已拒绝自动清理，避免误杀无关进程。")
        print(f"           确认该进程确实是本代理残留后，手动执行: {manual}")
        return 1

    if root is None:
        print("  归属   : 监听者属于本脚手架，但进程链里没有可安全终止的根节点")
        print("  处置   : 已拒绝自动清理（外层进程归属不明，收树可能波及无关进程）。")
        print(f"           确认后手动执行: {manual}")
        return 1

    cleanup_hint = f"taskkill /F /T /PID {root}" if os.name == "nt" else f"kill -TERM {root}"
    print(f"  清理   : {cleanup_hint}" + ("" if root == owner else f"（根节点，含监听者 {owner}）"))
    if not allow_prompt:
        return 1
    if not _ask("是否立即清理？[y/N]: "):
        print("已跳过。手动清理完成后再启动 start_background.bat。")
        return 1

    snapshot = next((row for row in chain if int(row.get("pid") or 0) == root), None)
    safe, why = revalidate_target(root, snapshot)
    if not safe:
        print(f"  复核   : {why}")
        print(f"  处置   : 已拒绝自动清理。确认无误后手动执行: {manual}")
        return 1

    ok = _kill_tree(root)
    if ok and root != owner and pid_alive(owner):
        # 根节点已带 /T 收树；再兜一次监听者，防垫片退出后内层存活。
        # 兜底前同样复核，避免端口已被别的进程重新占上时打错目标。
        safe_owner, why_owner = revalidate_target(owner, head)
        if safe_owner:
            _kill_tree(owner)
        else:
            print(f"  [跳过] 未再清理监听者: {why_owner}")
    if ok:
        print("清理完成。重新启动: 双击 start_background.bat")
        return 0
    print(f"[警告] 清理失败，请手动执行: {cleanup_hint}")
    return 1


# ---------------------------------------------------------------- 命令实现


def cmd_status() -> int:
    port = _read_port()
    identity = read_identity()
    if identity is None:
        print("状态: 未运行（无 pidfile）")
        handle_port_holder(port, allow_prompt=False)
        print("启动方式: 双击 start_background.bat")
        return 1
    pid = identity["pid"]
    if not pid_alive(pid):
        print(f"状态: 已停止（pidfile 残留 pid={pid}，daemon 进程不存在）")
        PID_FILE.unlink(missing_ok=True)
        left = handle_port_holder(port, allow_prompt=False)
        if left:
            print("清理: 先执行 stop.bat 清掉孤儿进程，再启动")
        else:
            print("清理: 直接启动即可，pidfile 会自动覆盖")
        return 1

    chain = process_chain(pid)
    confirmed, reason = verify_identity(identity, chain[0] if chain else None)
    env: dict = {}
    try:
        env = bridge_env.ensure_env()
    except FileNotFoundError:
        pass
    print(f"状态: 运行中  daemon pid={pid}  端口 {port}")
    if not confirmed:
        print(f"[注意] pidfile 身份核对未通过：{reason}")
        print("       该 pid 可能已被系统复用，stop.bat 会拒绝自动终止，请人工核实。")
    print(f"日志: {LOG_DIR / 'daemon-YYYY-MM-DD.log'}  {LOG_DIR / 'proxy-YYYY-MM-DD.log'}（按日期轮转）")

    # 连接信息：后台无窗口，status 是事后查 URL / Key 的唯一入口
    try:
        master_key = env.get("PROXY_MASTER_KEY", "")
        pairs = bridge_env.proxy_model_pairs(env) if env else []
        aliases = [a for a, _ in pairs] if pairs else []
        print()
        print("─" * 46)
        print("连接信息（填入客户端 / models.json）")
        print(f"  Base URL : http://127.0.0.1:{port}/v1")
        if master_key:
            print(f"  API Key  : {master_key}")
        else:
            print("  API Key  : (未配置 PROXY_MASTER_KEY；下次启动会自动生成并写回 .env)")
        if aliases:
            print(f"  Model    : {' / '.join(aliases)}")
        else:
            print("  Model    : (未配置 UPSTREAM_MODELS)")
        print(f"  完整 URL : http://127.0.0.1:{port}/v1/chat/completions")
        print("─" * 46)
    except Exception as exc:  # noqa: BLE001  # 连接信息只是附加项，不阻断状态查询
        print(f"连接信息读取失败（.env 异常?）: {exc}")
    return 0


def cmd_stop() -> int:
    port = _read_port()
    identity = read_identity()
    if identity is None:
        print("无需停止：无 pidfile，daemon 未在运行")
        # pidfile 没了，但端口仍可能被上次没收干净的孤儿占着
        return handle_port_holder(port, allow_prompt=True)
    pid = identity["pid"]
    if not pid_alive(pid):
        print(f"无需停止：pid={pid} 已不存在，清理残留 pidfile")
        PID_FILE.unlink(missing_ok=True)
        # 关键补漏：pid 死了不代表代理停了，litellm 子进程常常还活着
        return handle_port_holder(port, allow_prompt=True)

    # 终止前先确认这个 pid 仍是 pidfile 记录的那个进程。pid 会被系统复用，
    # 残留 pidfile 指向一个无关进程时，照着杀就是误杀。
    chain = process_chain(pid)
    confirmed, reason = verify_identity(identity, chain[0] if chain else None)
    if not confirmed:
        print(f"[拒绝] 无法确认 pid={pid} 就是本脚手架的 daemon：{reason}")
        print("       为防止误杀无关进程，已停止操作。人工核实：")
        print(f'       tasklist /FI "PID eq {pid}"')
        print(f"       确认它确实是本代理后，手动执行: taskkill /F /T /PID {pid}")
        print(f"       或删除 {PID_FILE} 后重新启动。")
        return 1
    print(f"身份核对: {reason}")

    print(f"停止 daemon pid={pid} 及其子进程树...")
    if os.name == "nt":
        # /T 带走整棵进程树（litellm 在 daemon 之下）
        # tasklist/taskkill 输出为 GBK（中文系统），text=True 按 utf-8 解码会崩，
        # 用 errors="replace" 容错
        out = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ok = out.returncode == 0
        print((out.stdout or b"").decode("gbk", "replace").strip())
        if not ok:
            print((out.stderr or b"").decode("gbk", "replace").strip())
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            print("已发送 SIGTERM")
            ok = True
        except OSError as exc:
            print(f"发送信号失败: {exc}")
            ok = False
    PID_FILE.unlink(missing_ok=True)
    if ok:
        # 树理论上已随 /T 走，但垫片链偶尔会漏；顺手核一次端口，
        # 避免"以为停了其实还在"
        if wait_port_free(port, timeout=3.0):
            print(f"端口 {port}: 已释放")
            print("完成。重新启动: 双击 start_background.bat")
            return 0
        if handle_port_holder(port, allow_prompt=True) == 0:
            print("完成。重新启动: 双击 start_background.bat")
            return 0
        return 1
    # kill 失败时 pidfile 已在上面清理——此时进程还在跑但 daemon 已失联，
    # 必须把 pid 告诉用户，否则再也停不掉它
    print(f"[警告] 停止失败，daemon pid={pid} 可能仍在运行。")
    print(f"       手动处理: taskkill /F /T /PID {pid}")
    handle_port_holder(port, allow_prompt=False)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="后台模式状态与停止控制")
    ap.add_argument("command", choices=["status", "stop"])
    args = ap.parse_args()
    if args.command == "status":
        return cmd_status()
    return cmd_stop()


if __name__ == "__main__":
    sys.exit(main())
