"""前台启动本地 OpenAI 兼容代理（原启动方式，行为不变）。

流程：读取 .env -> 生成 config.yaml -> 注入环境变量 -> 拉起 LiteLLM。

无窗口后台模式请改用 start_background.bat / daemon.py；
公共装配逻辑见 boot_common.py。
"""
from __future__ import annotations

import subprocess
import sys

import boot_common
import bridge_env
import gen_config


def main() -> int:
    try:
        env = bridge_env.ensure_env()
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 1

    code = gen_config.main()
    if code != 0:
        return code

    try:
        port, cmd, info = boot_common.prepare(env)
    except RuntimeError as exc:
        print(f"[错误] {exc}")
        return 1

    if boot_common.port_in_use(port):
        print(f"\n[提示] 端口 {port} 已被占用。")
        print("      若上一个代理还在运行，直接照常使用即可；")
        print("      若要重启，请先关闭占用该端口的进程，或在 .env 中改 PROXY_PORT。")
        return 1

    print("\n" + "=" * 56)
    print(f"OpenAI 兼容地址 : {info['openai_url']}")
    print(f"API Key         : {info['api_key']}")
    print(f"上游地址        : {info['upstream']}")
    print(f"上游令牌来源    : {info['token_source']}")
    print(f"对外模型        : {info['models']}")
    print("按 Ctrl+C 停止")
    print("=" * 56 + "\n")

    try:
        return subprocess.run(cmd, cwd=str(bridge_env.ROOT)).returncode
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
