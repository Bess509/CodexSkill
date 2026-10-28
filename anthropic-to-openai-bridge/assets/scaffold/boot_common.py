"""启动公共逻辑：装配环境变量、构建 LiteLLM 启动命令。

从 start_proxy.py 抽出，供前台（start_proxy）与后台（daemon）两种模式复用。
零第三方依赖。
"""
from __future__ import annotations

import os
import secrets
import socket
import sys
from pathlib import Path

import bridge_env


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _generate_master_key() -> str:
    """生成随机 master key。本地自用密钥，不追求密码学强度前缀，只保证唯一随机。"""
    return "sk-bridge-" + secrets.token_hex(16)


def _persist_master_key(key: str) -> None:
    """把自动生成的 key 写回 .env，保证下次启动不变。

    若 .env 里已有被清空的 PROXY_MASTER_KEY 行则原位补值；没有则追加到文件末尾。
    """
    path = bridge_env.ENV_FILE
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    new_line = f"PROXY_MASTER_KEY={key}"
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("PROXY_MASTER_KEY="):
            lines[i] = new_line
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def resolve_launcher(python_exe: Path) -> list[str]:
    exe = python_exe.parent / ("litellm.exe" if os.name == "nt" else "litellm")
    if exe.exists():
        return [str(exe)]
    return [str(python_exe), "-m", "litellm"]


DUMP_MAX_CHARS_DEFAULT = 200000


def resolve_dump_settings(env: dict) -> tuple[bool, int]:
    """解析诊断转储开关与字符上限，返回 (是否开启, 字符上限)。

    默认关闭。上限非法（0、负数、非数字）时显式回落默认值并打印提示——
    转储只是诊断手段，为它拦停整个代理不划算，但也不能静默接受非法值。
    """
    enabled = bridge_env.as_bool(env.get("BRIDGE_DUMP_ENABLED"), False)
    raw = (env.get("BRIDGE_DUMP_MAX_CHARS") or "").strip()
    limit = DUMP_MAX_CHARS_DEFAULT
    if raw:
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            print(f"[配置] BRIDGE_DUMP_MAX_CHARS={raw!r} 不是整数，按默认值 {DUMP_MAX_CHARS_DEFAULT} 处理")
        else:
            if parsed <= 0:
                print(f"[配置] BRIDGE_DUMP_MAX_CHARS={parsed} 必须为正整数，按默认值 {DUMP_MAX_CHARS_DEFAULT} 处理")
            else:
                limit = parsed
    return enabled, limit


def apply_dump_settings(env: dict) -> tuple[bool, int]:
    """把转储配置导出到进程环境，LiteLLM 子进程由此继承。

    bridge_guard 在代理进程内读环境变量决定是否落盘，它看不到 .env 的原始内容；
    前台（start_proxy）与后台（daemon）都经过 prepare()，所以导出必须放在这里，
    否则 .env 里写了开关也不生效。

    关闭时显式写 false：如果只是"不覆盖"，子进程会继承父进程环境里残留的
    BRIDGE_DUMP_ENABLED=true（同一个终端反复启动就是这种局面），用户以为关了、
    实际一直在落盘完整对话。
    """
    enabled, limit = resolve_dump_settings(env)
    os.environ["BRIDGE_DUMP_ENABLED"] = "true" if enabled else "false"
    os.environ["BRIDGE_DUMP_MAX_CHARS"] = str(limit)
    dump_file = (env.get("BRIDGE_DUMP_FILE") or "").strip()
    if dump_file:
        os.environ["BRIDGE_DUMP_FILE"] = dump_file
    return enabled, limit


def prepare(env: dict) -> tuple[int, list[str], dict]:
    """装配运行环境。返回 (端口, 启动命令, 横幅信息 dict)。

    在这里统一做五件事：
    1. 校验 .env 必填项（令牌、上游地址、模型名）
    2. 解析令牌并注入 UPSTREAM_API_KEY
    3. 注入回环直连（NO_PROXY）、max_tokens 兜底、遥测关闭等环境变量
    4. 生成 config.yaml
    5. 构建 LiteLLM 启动命令
    任一步失败抛 RuntimeError，调用方决定如何呈现错误。
    """
    token, token_source = bridge_env.resolve_token(env)
    if not token:
        raise RuntimeError(
            "未找到上游令牌。请在 .env 中填写 UPSTREAM_API_KEY，"
            "或设置环境变量 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY。"
        )
    os.environ["UPSTREAM_API_KEY"] = token

    base = bridge_env.normalize_base(env.get("UPSTREAM_BASE_URL", ""))
    if not base or bridge_env.is_placeholder(base):
        raise RuntimeError(
            f"UPSTREAM_BASE_URL 尚未填写或仍是占位值：{env.get('UPSTREAM_BASE_URL', '')!r}"
        )

    models = bridge_env.upstream_models(env)
    if not models or all(bridge_env.is_placeholder(m) for m in models):
        raise RuntimeError(
            f"UPSTREAM_MODELS 尚未填写或仍是占位值：{models}"
        )

    master_key = (env.get("PROXY_MASTER_KEY") or "").strip()
    if not master_key or bridge_env.is_placeholder(master_key):
        # 没有预配置 key 时自动生成并写回 .env：公开仓库里的固定默认值
        # 会在有人开放 0.0.0.0 监听时变成安全隐患
        master_key = _generate_master_key()
        _persist_master_key(master_key)
        env["PROXY_MASTER_KEY"] = master_key
        print(f"[初始化] 已自动生成 PROXY_MASTER_KEY 并写回 .env：{master_key[:12]}...")
    os.environ["PROXY_MASTER_KEY"] = master_key
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 强制子进程（litellm）用 UTF-8 读文件：config.yaml 是 UTF-8 写出的，
    # 中文 Windows 默认 locale 是 GBK，计划任务等干净环境下 PyYAML 会
    # UnicodeDecodeError（Git Bash 会话里恰好有 UTF-8 相关变量兜底，掩盖过）
    os.environ["PYTHONUTF8"] = "1"
    os.environ["DO_NOT_TRACK"] = "True"
    os.environ["LITELLM_TELEMETRY"] = "False"
    os.environ["NO_COLOR"] = "1"
    # 本机常存在 HTTP_PROXY（各类软件的本地代理），会把 127.0.0.1 的请求也发去代理。
    # 让回环地址直连，远端上游仍可走系统代理。
    no_proxy = "127.0.0.1,localhost,::1"
    existing = os.environ.get("NO_PROXY", "").strip()
    if existing and existing.lower() not in no_proxy:
        no_proxy = f"{existing},{no_proxy}"
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy
    # Anthropic 强制要求 max_tokens，OpenAI 客户端常不传，这里给转发请求兜底
    os.environ["DEFAULT_ANTHROPIC_CHAT_MAX_TOKENS"] = str(
        bridge_env.as_int(env.get("DEFAULT_MAX_TOKENS"), 8192)
    )
    # 纠偏所用的输出 token 下限，由 bridge_guard 读取
    os.environ["BRIDGE_MIN_MAX_TOKENS"] = str(
        bridge_env.as_int(env.get("MIN_OUTPUT_TOKENS"), 1600)
    )
    # 诊断转储开关与上限：bridge_guard 在子进程里读环境变量，必须在这里导出
    dump_on, dump_limit = apply_dump_settings(env)
    if dump_on:
        print(f"[诊断] 完整请求转储已开启，上限 {dump_limit} 字符；排查完请把 "
              f"BRIDGE_DUMP_ENABLED 改回 false 并删除 logs/last_request.json")
    # 按模型的输出上限表（JSON 对象，如 {"glm-5.3": 131072}），由 bridge_guard 读取；
    # 留空则只用内置实测表。写 .env 与写系统环境变量 BRIDGE_MAX_OUTPUT_CAPS 等效
    extra_caps = (env.get("MAX_OUTPUT_TOKEN_CAPS") or "").strip()
    if extra_caps:
        os.environ["BRIDGE_MAX_OUTPUT_CAPS"] = extra_caps
    # 跳过启动时联网拉取模型价格表，国内网络下这一步会白等三十秒
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["LITELLM_DONT_INITIALIZE_TELEMETRY"] = "True"

    port = bridge_env.as_int(env.get("PROXY_PORT"), 4000)

    python_exe = Path(sys.executable)
    cmd = resolve_launcher(python_exe) + [
        "--config", str(bridge_env.CONFIG_FILE),
        "--host", "127.0.0.1",
        "--port", str(port),
    ]

    aliases = [a for a, _ in bridge_env.proxy_model_pairs(env)]
    info = {
        "port": port,
        "cmd": cmd,
        "openai_url": f"http://127.0.0.1:{port}/v1",
        "api_key": os.environ["PROXY_MASTER_KEY"],
        "upstream": base,
        "token_source": token_source,
        "models": ", ".join(aliases) if aliases else "(未配置)",
    }
    return port, cmd, info
