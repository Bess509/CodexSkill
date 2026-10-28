"""后台守护模式：无窗口常驻运行 LiteLLM 代理。

由 start_background.bat 经 pythonw 拉起（计划任务或手动），行为要点：

- 无控制台：LiteLLM 子进程用 CREATE_NO_WINDOW 拉起，stdout/stderr 落盘
  logs/proxy.log；daemon 自身日志写 logs/daemon.log
- pidfile：logs/daemon.pid 记录 daemon 进程号，stop.bat 依赖它定位进程树
- watchdog：litellm 退出后 5 秒重启，指数退避封顶 60 秒；连续崩溃 10 次放弃，
  避免 .env 配错时无限拉起
- 孤儿检测：端口被一个「健康的残留代理」占用时（父进程被杀、uvicorn 孤儿
  仍在监听），健康检查会被孤儿代答，重启永远不可能成功——此时直接退出并
  留日志指导清理，不做无限重启（见 pitfalls 第 14 条）
- 单实例：端口已占用时不抢，直接退出并留日志（与前台模式行为一致）

用法：
    pythonw daemon.py            # 后台启动（由 start_background.bat 调用）
    python daemon.py --foreground  # 前台调试：能看日志，行为相同
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import boot_common
import bridge_env
import daemon_ctl
import gen_config

ROOT = bridge_env.ROOT
LOG_DIR = ROOT / "logs"
PID_FILE = LOG_DIR / "daemon.pid"
DAEMON_LOG = LOG_DIR / "daemon.log"
PROXY_LOG = LOG_DIR / "proxy.log"

# 日志按日期轮转：写入时按当天解析目标文件（daemon-YYYY-MM-DD.log / proxy-YYYY-MM-DD.log），
# 跨天后下一条写入自然切换文件。daemon 是长驻进程，不能只在启动时算一次日期。
LOG_ROTATE = os.environ.get("BRIDGE_LOG_ROTATE", "true").strip().lower() not in ("0", "false", "no", "off")


def _rotated(path: Path) -> Path:
    if not LOG_ROTATE:
        return path
    stamp = datetime.now().strftime("%Y-%m-%d")
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")

CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# proxy.log 的常驻句柄：每次重启 litellm 复用同一句柄，避免句柄泄漏
_proxy_log_fh = None

# watchdog 参数：首延 5 秒，指数退避 x2，封顶 60 秒，连续崩溃 10 次放弃
RETRY_FIRST_DELAY = 5
RETRY_MAX_DELAY = 60
MAX_CONSECUTIVE_CRASHES = 10
# 健康宽限期：litellm 绑定失败即退（秒级），若本进程的 litellm 真正起服务，
# GRACE 秒内不会退出；宽限期后子进程仍活着且健康，才认定健康响应来自本进程。
HEALTH_GRACE_SECONDS = 15


_REDIRECTED = False


def log(message: str) -> None:
    """写日志到 logs/daemon.log。

    stdout 是真实控制台（前台/手动）时同时打印；pythonw 模式下
    stdout 已被 ensure_streams 重定向到同一文件，只写文件避免双写。
    """
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    LOG_DIR.mkdir(exist_ok=True)
    with _rotated(DAEMON_LOG).open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if not _REDIRECTED:
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001  # stdout 异常时静默，日志已落盘
            pass


def ensure_streams() -> None:
    """pythonw 启动时 sys.stdout / sys.stderr 为 None，任何 print（包括
    gen_config 里的）都会崩溃。重定向到当天的轮转日志文件（文本模式），
    保证整条链路可打印。进程跨天存活时重定向句柄停留在启动日，可接受——
    daemon 自身的关键日志走 log() 会按天切换。
    """
    global _REDIRECTED
    LOG_DIR.mkdir(exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(_rotated(DAEMON_LOG), "a", buffering=1, encoding="utf-8")  # type: ignore[assignment]
        _REDIRECTED = True
    if sys.stderr is None:
        sys.stderr = open(_rotated(DAEMON_LOG), "a", buffering=1, encoding="utf-8")  # type: ignore[assignment]


def write_pid() -> None:
    """记录自身身份，供 daemon_ctl 在终止前核对。

    只存 pid 不够：pid 会被系统复用，daemon 崩溃后残留的 pidfile 可能指向一个
    毫不相干的进程，stop.bat 照着它 taskkill 就是误杀。这里连同启动时间、
    解释器路径、脚本绝对路径、脚手架根目录一起存，终止前用四要素确认
    "还是不是原来那个进程"。

    编码必须是 UTF-8：身份里带绝对路径，安装目录含中文时用 ASCII 写会直接
    UnicodeEncodeError，后台启动在写 pidfile 这一步就崩，且崩在 try 之外没人
    兜底。读侧（daemon_ctl.read_identity）同样按 UTF-8 解码，两侧必须一致。
    """
    LOG_DIR.mkdir(exist_ok=True)
    pid = os.getpid()
    chain = daemon_ctl.process_chain(pid)
    own = chain[0] if chain and chain[0].get("pid") == pid else {}
    tokens = daemon_ctl._path_tokens(own.get("cmd"))
    if not tokens or not own.get("exe") or daemon_ctl._parse_time(own.get("created")) is None:
        raise RuntimeError("无法读取当前进程的解释器路径与启动时间，不能安全创建 daemon 身份文件")
    # Windows 的 venv 启动器可能把命令行也改成基础解释器路径。记录 OS
    # 实际报告的同一份证据，不能用 sys.executable 猜测复核时会看到什么。
    identity = {
        "schema": 2,
        "pid": pid,
        "created": own["created"],
        "exe": tokens[0],
        "script": str(Path(__file__).resolve()),
        "root": str(ROOT),
    }
    PID_FILE.write_text(json.dumps(identity, ensure_ascii=False), encoding="utf-8")


def clear_pid() -> None:
    if PID_FILE.exists():
        PID_FILE.unlink()


def start_proxy_process(cmd: list[str]) -> subprocess.Popen:
    """以无窗口方式拉起 LiteLLM，输出落盘当天轮转的 proxy 日志（追加，句柄常驻复用）。

    句柄在拉起子进程时解析一次：litellm 的 stdout 重定向不能中途换文件，
    进程跨天时长输出会继续写启动日文件，直到下一次重启才切新文件——
    可接受的折衷，换来句柄不泄漏。
    """
    global _proxy_log_fh
    LOG_DIR.mkdir(exist_ok=True)
    if _proxy_log_fh is None or _proxy_log_fh.closed:
        _proxy_log_fh = open(_rotated(PROXY_LOG), "ab")
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=_proxy_log_fh,
        stderr=subprocess.STDOUT,
        creationflags=CREATION_FLAGS,
    )


def alive(port: int) -> bool:
    try:
        return boot_common.port_in_use(port)
    except Exception:  # noqa: BLE001
        return False


def wait_healthy(port: int, timeout: float = 120.0) -> bool:
    """LiteLLM 打印 Uvicorn running 后仍需数秒才可接请求，轮询健康检查。"""
    import urllib.request

    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health/liveliness"
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    ensure_streams()
    ap = argparse.ArgumentParser(description="后台守护模式运行代理")
    ap.add_argument("--foreground", action="store_true",
                    help="前台运行便于调试，不写 pidfile")
    args = ap.parse_args()

    try:
        env = bridge_env.ensure_env()
    except FileNotFoundError as exc:
        log(f"[错误] {exc}")
        return 1

    code = gen_config.main()
    if code != 0:
        return code

    try:
        port, cmd, info = boot_common.prepare(env)
    except RuntimeError as exc:
        log(f"[错误] {exc}")
        return 1

    if boot_common.port_in_use(port):
        log(f"[退出] 端口 {port} 已被占用。若上一个代理还在运行，直接照常使用即可；"
            f"若要重启，先跑 stop.bat，或在 .env 中改 PROXY_PORT。")
        return 1

    if not args.foreground:
        write_pid()
    log(f"启动代理: {' '.join(cmd)}")
    log(f"OpenAI 兼容地址: {info['openai_url']}  API Key: {info['api_key']}  "
        f"上游: {info['upstream']}  模型: {info['models']}")

    consecutive = 0
    _current_proc: subprocess.Popen | None = None

    def _on_term(signum, frame):  # noqa: ANN001
        """POSIX 下 stop 依赖 SIGTERM：收到即终止 litellm 子进程再退出，
        否则会留下占着端口的孤儿 uvicorn（见 pitfalls 第 14 条）。"""
        log(f"收到信号 {signum}，终止 litellm 子进程后退出")
        if _current_proc is not None and _current_proc.poll() is None:
            try:
                _current_proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        clear_pid()
        sys.exit(0)

    if os.name != "nt" and not args.foreground:
        # 后台模式才注册；前台调试走 KeyboardInterrupt 分支即可
        import signal as _signal
        _signal.signal(_signal.SIGTERM, _on_term)

    try:
        while True:
            proc = start_proxy_process(cmd)
            _current_proc = proc
            log(f"litellm 已拉起 (pid={proc.pid})，等待健康检查...")
            healthy = wait_healthy(port)
            if healthy:
                # 健康检查只证明「端口上有人响应」，不证明响应者是我们拉起的进程：
                # 端口若被一个健康的残留代理（孤儿 uvicorn）占着，它会代答健康检查，
                # 我们自己这个 litellm 因绑定失败早已退出。等一个宽限期再核对子进程，
                # 避免把孤儿的健康误记为自愈成功、进而无限重启。
                deadline = time.monotonic() + HEALTH_GRACE_SECONDS
                while time.monotonic() < deadline and proc.poll() is None:
                    time.sleep(1.0)
                if proc.poll() is not None:
                    # 子进程已退：健康响应来自别人（端口被占的孤儿），本进程没起来
                    log(f"litellm pid={proc.pid} 已退出 (rc={proc.returncode})，"
                        f"但健康检查仍有响应——端口 {port} 被一个残留的旧代理占用。")
                    log(f"[退出] 停止重启：先清理占用端口的旧进程再启动。")
                    log(f"  排查: netstat -ano | findstr :{port}")
                    log(f"  清理: taskkill /F /PID <旧进程pid>  （或运行 stop.bat 后重试）")
                    return 1
                log(f"健康检查通过 (port={port})，进入守护循环")
                consecutive = 0
                rc = proc.wait()
                log(f"litellm 退出 (rc={rc})")
            else:
                # 健康检查超时：进程可能还活着但卡死，也可能已死，统一按崩溃处理
                proc.kill()
                log("健康检查超时（120 秒），已终止 litellm，按崩溃处理")
                rc = -1

            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_CRASHES:
                log(f"连续崩溃 {consecutive} 次，放弃重启。请检查 logs/proxy.log 排查根因。")
                clear_pid()
                return 1

            delay = min(RETRY_FIRST_DELAY * (2 ** (consecutive - 1)), RETRY_MAX_DELAY)
            log(f"将在 {delay} 秒后第 {consecutive} 次重启（连续崩溃计数 {consecutive}）")
            time.sleep(delay)

            if boot_common.port_in_use(port):
                log(f"端口 {port} 仍被占用（可能是 TIME_WAIT 或残留进程），跳过本轮重启")
                # 端口被占时不无限等待，继续下一轮退避
                continue
    except KeyboardInterrupt:
        log("收到停止信号，退出守护")
        return 0
    finally:
        clear_pid()


if __name__ == "__main__":
    sys.exit(main())
