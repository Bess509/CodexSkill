"""LiteLLM 自定义回调：在请求转发到上游前做纠偏与加固。

要解决的问题（均来自实测）：
1. GLM 系列存在训练期工具偏置，在工具列表未被充分注意时会用 Markdown 伪造
   "内置工具调用 + 返回结果"，例如虚构 `webReader` 并把本地路径拼成 file:// URL，
   导致整轮对话失败。这里注入一条最高优先级的工具使用规范。
2. 该模型先输出思考块，客户端若给的 max_tokens 偏小，正文会被思考耗尽而截断。
   这里给输出 token 设一个下限。
3. 记录日志便于事后排查。

在 config.yaml 中通过 `litellm_settings.callbacks` 挂载：
    callbacks:
      - bridge_guard.proxy_handler_instance
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.proxy_server import DualCache, UserAPIKeyAuth

import compat_patch

# 兼容补丁：删掉 litellm 给函数工具硬编码的 type="custom"，
# 否则部分严格网关（按强类型反序列化、只认 web_search_* 等服务端工具变体）
# 会直接 422。详见 skill 的 references/pitfalls.md 第 10 条。
# 挂在这里是因为该模块随 config.yaml 的 callbacks 在代理启动时必然被导入。
compat_patch.apply()

# 请求日志：用于定位"客户端到底有没有下发工具"以及"链路有没有把工具弄丢"。
# 文件名按日期轮转（requests-YYYY-MM-DD.log）：写入时按当天解析目标文件，
# 跨天后的下一条写入自然落到新文件，无需后台线程。设 BRIDGE_LOG_ROTATE=false
# 可回退为单文件不轮转（兼容已有日志采集）。
LOG_FILE = Path(os.environ.get("BRIDGE_LOG_FILE", "logs/requests.log"))
# 完整请求体转储：默认关闭。开启后会落盘完整对话（含 system 全文与工具 schema），
# 属于诊断手段而非日常日志，只在复现具体问题时临时打开。
#   BRIDGE_DUMP_ENABLED=true   打开
#   BRIDGE_DUMP_FILE=...       目标文件（默认 logs/last_request.json，覆盖写）
#   BRIDGE_DUMP_MAX_CHARS=...  落盘上限，超出部分截断（默认 200000）
DUMP_ENABLED = os.environ.get("BRIDGE_DUMP_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
DUMP_FILE = Path(os.environ.get("BRIDGE_DUMP_FILE", "logs/last_request.json"))

LOG_ROTATE = os.environ.get("BRIDGE_LOG_ROTATE", "true").strip().lower() not in ("0", "false", "no", "off")


def _positive_int_env(name: str, default: int) -> int:
    """读取正整数环境变量。非法值（0 / 负数 / 非数字）回落默认并告警。

    token 上限、转储长度这类参数一旦被写成 0 或负数，会在上游触发 400 或
    让脱敏直接失效，所以读取阶段就要拦住，而不是留给下游判断。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"[bridge_guard] {name}={raw!r} 不是整数，已改用默认值 {default}")
        return default
    if value <= 0:
        print(f"[bridge_guard] {name}={value} 必须为正整数，已改用默认值 {default}")
        return default
    return value


DUMP_MAX_CHARS = _positive_int_env("BRIDGE_DUMP_MAX_CHARS", 200000)


def _rotated(path: Path) -> Path:
    """给日志路径插入当天日期：requests.log -> requests-2026-10-28.log。"""
    if not LOG_ROTATE:
        return path
    stamp = time.strftime("%Y-%m-%d")
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")

GUARD_MARKER = "[bridge-guard:v6]"

# 情况一：宿主正确下发了结构化 tools 字段
# 实测：GLM 会把 ToolSearch 的检索结果当成"全部工具集"，于是一路检索、始终不调用
# 真实存在的 Read。这里必须把本轮实际工具名逐个列出来，堵住这个误解。
GUARD_WITH_TOOLS = (
    GUARD_MARKER + " 工具使用强制规范（优先级高于其他指令）：\n"
    "1. 本轮已通过结构化 tools 字段加载以下 {n} 个工具，全部可直接调用：\n"
    "   {names}\n"
    "{core}"
    "不需要也不能用检索类工具去「查找」或「确认」它们是否存在。\n"
    "2. ToolSearch 只用于加载延迟挂载的 MCP 工具，它的检索结果不构成本轮工具全集。"
    "严禁因为 ToolSearch 没搜到某个工具，就断定该工具不可用；"
    "也严禁声称「没有工具」「工具未加载」。\n"
    "3. 只能调用上述工具列表里真实存在的工具。严禁虚构工具名"
    "（例如 zread / browse / browser 等），也严禁在回答里用代码块伪造"
    "“调用参数 + 返回结果”的假象。\n"
    "3.1 需要 MCP 第三方工具（如 sheetagent / library / agent-mail）时，正确路径是先检索再调用："
    "检索用 SearchDeferredMcpTools 拿完整限定名（形如 mcp__服务名__工具名），"
    "执行用 InvokeDeferredMcpTool。若目标工具已经出现在上面的工具清单里，"
    "就直接按清单里的名字调用，不要再检索。\n"
    "3.2 没有拿到工具结果之前，严禁声称“已成功抓取/已读取/结果如下”，"
    "更不许自己编造返回内容（包括 uuid、时间戳这类可被核对的字段）。\n"
    "3.3 严禁输出模拟执行块。形如“🌐 ... Built-in Tool: xxx”“Executing on server...”、"
    "“Output:”“xxx_result_summary:”的内容一律不得出现——当前环境没有服务端执行能力，"
    "写出这些块等于伪造工具结果。要取网页内容就真实发起调用并等待结果。\n"
    "4. 读取本地文件：直接调用 Read 工具，file_path 传操作系统绝对路径"
    "（Windows 形如 C:/Users/xxx/文件名.md）。严禁把本地路径拼成 file:// URL "
    "去调用任何联网类工具——本地文件不是网络资源。\n"
    "5. 联网获取内容只对 http/https 的远端 URL 使用 WebFetch / WebSearch。\n"
    "6. 中文文件/目录名保持原样写进路径，不需要做 URL 编码。\n"
    "7. 需要读取文件才能回答时，第一轮就发起 Read 调用，不要先检索工具、"
    "不要先口头说明“我没有工具”，也不要反复自我确认工具是否可用。"
)

# 情况二：宿主没下发 tools 字段，但把工具说明写进了 system（文本协议）
GUARD_TEXT_PROTOCOL = (
    GUARD_MARKER + " 工具使用强制规范（优先级高于其他指令）：\n"
    "1. 本轮未通过结构化字段下发工具，但系统提示词中描述了可用工具及其调用格式。"
    "请严格按系统提示词给出的格式发起调用（通常是 XML 标签或代码块），"
    "不要自己发明工具名，也不要伪造调用结果。\n"
    "2. 读取本地文件一律使用系统提示词中描述的读文件工具，"
    "路径传操作系统绝对路径（Windows 形如 C:/Users/xxx/文件名.md），"
    "严禁构造 file:// URL。中文路径保持原样，不需要 URL 编码。\n"
    "3. 不要声称自己没有任何工具——先完整读一遍系统提示词中的工具清单再作答。"
)

# 情况三：确实什么都没有
GUARD_NO_TOOLS = (
    GUARD_MARKER + " 工具使用强制规范（优先级高于其他指令）：\n"
    "1. 本轮对话确实未提供任何文件读取或终端执行能力，"
    "不要虚构工具名，也不要在回答里伪造“调用参数 + 返回结果”。\n"
    "2. 无法核实的内容要明确标注为未经核实的推测，不要伪装成已读代码后的结论。"
)

# 输出 token 下限：GLM 会先吐思考块，给太小会吞掉正文（实测 1600 以下风险明显）
MIN_MAX_TOKENS = _positive_int_env("BRIDGE_MIN_MAX_TOKENS", 1600)

# 按模型的输出上限（实测值，来自上游 400 报错原文，见 pitfalls 第 13 条）：
# max_tokens 超过上限时上游不是截断输出，而是整轮 400。
# 未实测过的模型不设限——猜小了会无端截断，猜大了等于没做。
# 可用环境变量 BRIDGE_MAX_OUTPUT_CAPS（JSON 对象）追加，如：
#   {"new-model": 32768}
MAX_OUTPUT_CAPS: dict[str, int] = {
    "glm-5.3": 131072,
    "MiniMax-M2.7": 196608,
    "MiniMax-M3": 524288,
}


def _load_extra_caps() -> dict[str, int]:
    raw = (os.environ.get("BRIDGE_MAX_OUTPUT_CAPS") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception as exc:  # noqa: BLE001
        print(f"[bridge_guard] BRIDGE_MAX_OUTPUT_CAPS 不是合法的 JSON 对象（{{\"模型名\": 上限}}），已忽略: {exc}")
        return {}
    caps: dict[str, int] = {}
    for key, value in data.items():
        try:
            cap = int(value)
        except (TypeError, ValueError):
            print(f"[bridge_guard] BRIDGE_MAX_OUTPUT_CAPS 中 {key} 的上限不是整数，已忽略")
            continue
        if cap <= 0:
            print(f"[bridge_guard] BRIDGE_MAX_OUTPUT_CAPS 中 {key} 的上限 {cap} 必须为正整数，已忽略")
            continue
        caps[str(key)] = cap
    return caps


MAX_OUTPUT_CAPS.update(_load_extra_caps())


def _tool_names(payload: dict) -> list[str]:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return []
    names = []
    for t in tools:
        if isinstance(t, dict):
            fn = t.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                names.append(str(fn["name"]))
            elif t.get("name"):
                names.append(str(t["name"]))
    return names


def _system_probe(payload: dict) -> tuple[int, bool]:
    """返回 system 文本长度，以及是否以自然语言描述了工具。

    有些宿主不填 tools 字段，而是把工具说明写进 system。区分这两种情况，
    才能判断"模型说没工具"到底是宿主没给，还是给法不同。
    """
    text = ""
    for msg in payload.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "system":
            text += _content_as_text(msg.get("content")) + "\n"
    keywords = ("Tool name:", "<functions>", "## Tools", "可用工具", "tool_calls",
                "function_calls", "You have access to", "tool name")
    return len(text), any(k in text for k in keywords)


def _log(stage: str, payload: dict) -> None:
    """把关键字段落到日志，用于比对「客户端下发」与「转发上游」是否一致。"""
    try:
        msgs = payload.get("messages") or []
        names = _tool_names(payload)
        sys_len, sys_has_tools = _system_probe(payload)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stage": stage,
            "model": payload.get("model"),
            "msg_count": len(msgs) if isinstance(msgs, list) else -1,
            "tool_count": len(names),
            "tools": names[:80],
            "tool_choice": payload.get("tool_choice"),
            "max_tokens": payload.get("max_tokens"),
            "stream": payload.get("stream"),
            "system_len": sys_len,
            "system_mentions_tools": sys_has_tools,
            # 思考相关：宿主是否要求高强度思考、是否显式给了 thinking 预算。
            # 思考与正文共用 max_tokens，思考越长，正文被挤空的风险越大。
            "reasoning_effort": payload.get("reasoning_effort"),
            "thinking": (payload.get("thinking") or {}).get("budget_tokens")
            if isinstance(payload.get("thinking"), dict) else bool(payload.get("thinking")),
        }
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _rotated(LOG_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[bridge_guard] 写日志失败，忽略: {exc}")


def _log_event(stage: str, info: dict) -> None:
    """记录响应侧结果：成功时的 finish_reason / 工具调用，失败时的异常与状态码。

    请求侧日志只能证明"工具发出去了"，回答"模型为什么说没工具"必须看响应侧。
    """
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": stage}
        rec.update(info)
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _rotated(LOG_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[bridge_guard] 写事件日志失败，忽略: {exc}")


def _resp_summary(response_obj: Any) -> dict:
    """把模型响应压成可判读的几项：是否真的产生了工具调用。"""
    out: dict[str, Any] = {}
    try:
        choices = getattr(response_obj, "choices", None)
        if choices:
            ch = choices[0]
            msg = getattr(ch, "message", None)
            finish = getattr(ch, "finish_reason", None)
            out["finish_reason"] = finish
            tcs = getattr(msg, "tool_calls", None) if msg is not None else None
            if tcs:
                out["tool_calls"] = [
                    getattr(getattr(tc, "function", None), "name", None) for tc in tcs
                ]
            else:
                out["tool_calls"] = []
            content = getattr(msg, "content", None) if msg is not None else None
            out["content_len"] = len(content or "")
            # 回复正文属于对话内容，常规日志只留长度；要看原文须显式开启转储
            if DUMP_ENABLED:
                out["content_head"] = (content or "")[:200].replace("\n", " ")
        usage = getattr(response_obj, "usage", None)
        if usage is not None:
            out["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
            out["completion_tokens"] = getattr(usage, "completion_tokens", None)
    except Exception as exc:  # noqa: BLE001
        out["parse_error"] = str(exc)[:200]
    return out


def _content_as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
        return "".join(parts)
    return str(content) if content is not None else ""


CORE_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Bash", "PowerShell")


def _pick_guard(payload: dict) -> str:
    """按宿主实际下发方式选规范文本，避免用错规范反而让模型放弃调用。"""
    names = _tool_names(payload)
    if names:
        core = [n for n in names if n in CORE_TOOLS]
        core_line = (
            "   其中 %s 均为已加载工具，" % " / ".join(core) if core else ""
        )
        return GUARD_WITH_TOOLS.format(
            n=len(names), names="、".join(names), core=core_line
        )
    _, mentions = _system_probe(payload)
    return GUARD_TEXT_PROTOCOL if mentions else GUARD_NO_TOOLS


REDACT_KEY_HINTS = (
    "authorization", "x-api-key", "api_key", "apikey", "api-key",
    "token", "secret", "password", "passwd", "cookie", "credential",
)
REDACT_PLACEHOLDER = "<redacted>"
IMAGE_BLOCK_TYPES = ("image", "image_url", "input_image")


def _redact_text(value: str) -> str:
    """超长字符串折叠中段，保留头尾便于识别。"""
    if len(value) <= 4096:
        return value
    return f"{value[:512]}...<truncated {len(value) - 1024} chars>...{value[-512:]}"


def _redact_image_block(value: dict, depth: int) -> dict:
    """图片块脱敏，返回新 dict——原对象一个字段都不改。

    覆盖两种形态：
    - Anthropic：{"type": "image", "source": {"type": "base64", "data": ...}}
    - OpenAI：   {"type": "image_url", "image_url": {"url": "data:..."}} /
                 {"type": "image_url", "image_url": "data:..."}
    """
    out: dict[str, Any] = {}
    for key, item in value.items():
        low = str(key).lower()
        if low == "source" and isinstance(item, dict):
            source = dict(item)
            data = source.get("data")
            if isinstance(data, str):
                source["data"] = f"<redacted image base64, {len(data)} chars>"
            url = source.get("url")
            if isinstance(url, str):
                source["url"] = f"<redacted image url, {len(url)} chars>"
            out[key] = source
        elif low == "image_url":
            if isinstance(item, str):
                out[key] = f"<redacted image, {len(item)} chars>"
            elif isinstance(item, dict):
                block = dict(item)
                url = block.get("url")
                if isinstance(url, str):
                    block["url"] = f"<redacted image url, {len(url)} chars>"
                out[key] = block
            else:
                out[key] = _redact_deep(item, depth + 1)
        elif any(hint in low for hint in REDACT_KEY_HINTS):
            out[key] = REDACT_PLACEHOLDER
        else:
            out[key] = _redact_deep(item, depth + 1)
    return out


def _redact_deep(value: Any, depth: int = 0) -> Any:
    """递归脱敏：敏感键打码、图片 base64 折叠、超长字符串收窄。

    纯函数：dict / list 一律新建后返回，绝不原地改写入参。转储只是诊断快照，
    但它和转发上游的请求共享同一批对象（payload["messages"] 是同一个 list），
    原地改写会把 base64 换成占位串再发出去——转储一开，多模态请求就废了。
    """
    if depth > 12:
        return "<depth-limit>"
    if isinstance(value, dict):
        if value.get("type") in IMAGE_BLOCK_TYPES:
            return _redact_image_block(value, depth)
        out = {}
        for key, item in value.items():
            if any(hint in str(key).lower() for hint in REDACT_KEY_HINTS):
                out[key] = REDACT_PLACEHOLDER
            else:
                out[key] = _redact_deep(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_redact_deep(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _dump(payload: dict) -> None:
    """转储完整请求体，仅在 BRIDGE_DUMP_ENABLED=true 时执行。

    默认不落盘：完整对话含 system 全文与用户数据，日常运行不该留这份副本。
    打开后按 BRIDGE_DUMP_MAX_CHARS 截断、敏感字段脱敏、权限收紧到当前用户。
    保留策略：单文件覆盖写，只保留最近一次；不再需要时删除 logs/last_request.json
    并清掉 .env 里的 BRIDGE_DUMP_ENABLED。
    """
    if not DUMP_ENABLED:
        return
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": payload.get("model"),
            "max_tokens": payload.get("max_tokens"),
            "tool_choice": payload.get("tool_choice"),
            "tools": payload.get("tools"),
            "messages": payload.get("messages"),
        }
        text = json.dumps(_redact_deep(rec), ensure_ascii=False, indent=1)
        truncated = len(text) > DUMP_MAX_CHARS
        if truncated:
            text = text[:DUMP_MAX_CHARS] + f"\n...<dump truncated at {DUMP_MAX_CHARS} chars>"
        DUMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        DUMP_FILE.write_text(text, encoding="utf-8")
        try:
            os.chmod(DUMP_FILE, 0o600)
        except OSError:
            pass
        if truncated:
            print(f"[bridge_guard] 转储超过 {DUMP_MAX_CHARS} 字符，已截断")
    except Exception as exc:  # noqa: BLE001
        print(f"[bridge_guard] 转储失败，忽略: {exc}")


def _has_guard(messages: list[dict]) -> bool:
    """只按标记去重。多轮对话里规范会随工具下发方式变化，命中即不重复注入。"""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            if GUARD_MARKER in _content_as_text(msg.get("content")):
                return True
    return False


def _inject_guard(messages: list[dict], text: str) -> None:
    """把规范并入首条 system；没有 system 就新建一条置于最前。"""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            old = msg.get("content")
            if isinstance(old, str):
                msg["content"] = f"{text}\n\n{old}"
            elif isinstance(old, list):
                old.insert(0, {"type": "text", "text": text})
            else:
                msg["content"] = text
            return
    messages.insert(0, {"role": "system", "content": text})


def _ensure_min_tokens(data: dict) -> None:
    """第一步：把偏小的 max_tokens 兜到下限。

    必须跑在按模型夹紧之前。反过来的话，下限值本身高于某个模型的上限时，
    兜底成果会原样发出，整轮 400。
    """
    try:
        cur = int(data.get("max_tokens") or 0)
    except (TypeError, ValueError):
        cur = 0
    if cur < MIN_MAX_TOKENS:
        data["max_tokens"] = MIN_MAX_TOKENS


def _clamp_max_tokens(data: dict) -> None:
    """第二步：按模型把 max_tokens 夹到实测上限内（pitfalls 第 13 条）。

    上限是按模型设的，客户端超限会整轮 400 而非截断——宁可截断也别整轮失败。
    顺序固定为「先兜下限、后夹上限」，保证最终值一定落在 [MIN_MAX_TOKENS, cap]
    的可行区间内；区间为空（下限大于上限）时以上限为准，让请求能跑通。
    模型名要剥 provider 前缀：日志里是 anthropic/glm-5.3，配置表里是 glm-5.3。
    """
    model = str(data.get("model") or "").split("/")[-1]
    cap = MAX_OUTPUT_CAPS.get(model)
    if not cap:
        return
    try:
        cur = int(data.get("max_tokens") or 0)
    except (TypeError, ValueError):
        return
    if cur > cap:
        data["max_tokens"] = cap


# ---------- 工具改名：消除 ToolSearch 对模型的诱导 ----------
#
# 实测（每项 5 次，同一句"读本地文件"，看是否直接调 Read）：
#   原名 ToolSearch 在场        1/5
#   只改描述、保留原名          4/5
#   改名 SearchDeferredMcpTools 5/5
# 结论：模型看到名为 ToolSearch 的工具就认定"原生工具是延迟加载的，必须先检索"，
# 于是反复检索、始终不调用真实存在的 Read。改描述不足以压制，必须改名。
TOOL_RENAMES = {
    "ToolSearch": "SearchDeferredMcpTools",
    "DeferExecuteTool": "InvokeDeferredMcpTool",
}
TOOL_REVERSE = {v: k for k, v in TOOL_RENAMES.items()}

DESC_OVERRIDES = {
    "ToolSearch": (
        "检索延迟挂载的 MCP 第三方服务工具（邮件、在线文档、表格、建站、知识库等）。"
        "本地文件读写、代码搜索、终端命令、子代理等核心工具已随本轮 tools 字段直接加载，"
        "不属于本工具范围；禁止用本工具检索、确认或校验它们是否存在。"
    ),
    "DeferExecuteTool": (
        "执行已加载的 MCP 第三方服务工具。核心工具（Read/Write/Edit/Glob/Grep/Bash 等）"
        "直接调用即可，无需经过本工具。"
    ),
}


# GLM 自带服务端内置工具（webReader / web_search 等）的"肌肉记忆"。
# 即使本轮 tools 里没有它，模型在"读网页"语境下仍会调 webReader，
# 同时输出 **🌐 Z.ai Built-in Tool: webReader** / Executing on server... / Output: ...
# 这类模拟执行块，连 uuid 都一并编造（实测六轮编出同一个 uuid）。
# 宿主没有这个工具，收到 tool_call 后永远等不到执行结果——
# 界面就卡在「执行中 webReader」，看起来就是"思考卡住"。
#
# 处置必须两头做，缺一不可：
#   请求侧：宿主声明了 WebFetch 时，补一份同 schema 的 webReader 别名，
#           把模型的调用参数约束在 url/prompt 上，避免它按记忆传 retain_images。
#   响应侧：把 webReader 改回宿主认识的 WebFetch，宿主才能真正执行拿到真实内容。
# 映射不到的未知工具直接丢弃，并把 finish_reason 归位成 stop，
# 否则宿主会一直等一个永不存在的工具。
BUILTIN_MAP = {
    "webReader": "WebFetch",
    "web_reader": "WebFetch",
    "webreader": "WebFetch",
    "browse": "WebFetch",
    "web_search": "WebSearch",
    "webSearch": "WebSearch",
}

# 请求侧只补这一个别名。四种拼法全补会把工具表撑大一倍，反而稀释真正该用的工具。
DECLARE_ALIASES = ("webReader",)

# 代理自动补的别名在 description 里带这个标记，用于与客户端真实声明区分开。
# 响应侧要靠它把"我们加的别名"从"客户端声明"里剔掉，否则会把客户端自己
# 声明的同名工具误判成别名而丢弃。
ALIAS_DESC_MARK = "[bridge-alias]"


def _builtin_schema(alias: str, target: str) -> dict:
    """别名工具的 schema：字段与 target 对齐，避免模型按记忆传多余参数。"""
    return {
        "type": "function",
        "function": {
            "name": alias,
            "description": (
                f"{ALIAS_DESC_MARK} 读取 http/https 网页，等价于 {target}。"
                "只接受 url 与 prompt 两个参数，不要传 retain_images / return_format 等字段。"
                "发起后必须等待工具的 real 结果，严禁自己撰写执行过程或返回内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整的 http/https 地址"},
                    "prompt": {"type": "string", "description": "要从该网页提取或核实的内容"},
                },
                "required": ["url", "prompt"],
            },
        },
    }


def _declare_builtin_aliases(payload: dict) -> list[str]:
    """宿主声明了目标工具时，补一份别名声明，让模型的内置工具习惯落在可执行范围内。"""
    names = set(_tool_names(payload))
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return []
    added: list[str] = []
    for alias in DECLARE_ALIASES:
        target = BUILTIN_MAP[alias]
        if alias in names or target not in names:
            continue
        tools.append(_builtin_schema(alias, target))
        names.add(alias)
        added.append("%s~>%s" % (alias, target))
    return added


def _resolve_name(name: str, declared: set[str]) -> str | None:
    """把上游返回的工具名解析成客户端认识的名字；解析不了返回 None（必须丢弃）。

    declared 为空表示客户端本轮没有声明任何工具，此时任何结构化工具调用都
    无从执行——放行只会让宿主去等一个永远不存在的工具，必须一律丢弃。
    别名（BUILTIN_MAP）也只映射到客户端显式声明的目标工具。
    """
    if not name:
        return None
    n = TOOL_REVERSE.get(name, name)
    if n in declared:
        return n
    # 内置工具别名优先：schema 是照宿主工具写的，参数一定合规
    target = BUILTIN_MAP.get(name) or BUILTIN_MAP.get(n)
    if target and target in declared:
        return target
    # 短名对全限定名：模型常把 mcp__web_reader__webReader 写成 webReader
    suffix = "__" + n.lower()
    for d in declared:
        if d.lower().endswith(suffix) or d.lower() == n.lower():
            return d
    return None


def _client_declared(payload: dict) -> set[str]:
    """客户端真实声明的工具名（剔除代理补的别名，且还原成客户端原名）。

    两件事必须一起做，否则响应侧会误判：
    1. 剔除本代理自动补的别名——依据 description 上的 ALIAS_DESC_MARK，
       客户端自己声明的同名工具不带标记，要如实保留。
    2. 把请求侧改名后的名字还原成客户端原名——request_data 里的 tools 已被
       _rename_forward 改写，直接取会得到 SearchDeferredMcpTools 这类上游名，
       一旦上游回包用原始名就会被当成"未声明"而丢弃。
    """
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return set()
    names: set[str] = set()
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not name:
            continue
        if ALIAS_DESC_MARK in str(fn.get("description") or ""):
            continue
        names.add(TOOL_REVERSE.get(str(name), str(name)))
    return names


def _replace_names_in_content(content: Any) -> Any:
    if isinstance(content, str):
        for old, new in TOOL_RENAMES.items():
            content = content.replace(old, new)
        return content
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text" and isinstance(blk.get("text"), str):
                for old, new in TOOL_RENAMES.items():
                    blk["text"] = blk["text"].replace(old, new)
    return content


def _sync_tool_choice(payload: dict) -> str | None:
    """工具改名时同步 tool_choice 指向的名字。

    tool_choice 有三种形态要区分对待：
      "auto" / "none" / "required"        字符串模式，原样保留
      {"type":"function","function":{...}} OpenAI 强制指定
      {"type":"tool","name":...}           Anthropic 强制指定
    只改强制指定里的名字，模式不变；漏改会让上游收到一个不在 tools 里的名字，
    直接 400。
    """
    choice = payload.get("tool_choice")
    if not isinstance(choice, dict):
        return None  # 字符串模式与未设置都原样放过
    fn = choice.get("function")
    if isinstance(fn, dict) and fn.get("name") in TOOL_RENAMES:
        old = fn["name"]
        fn["name"] = TOOL_RENAMES[old]
        return "tool_choice:%s->%s" % (old, fn["name"])
    if choice.get("type") in ("tool", "function") and choice.get("name") in TOOL_RENAMES:
        old = choice["name"]
        choice["name"] = TOOL_RENAMES[old]
        return "tool_choice:%s->%s" % (old, choice["name"])
    return None


def _rename_forward(payload: dict) -> list[str]:
    """请求侧改名：工具定义、历史工具调用、tool_choice、system 提及保持一致。

    只改 tools 不改历史消息会导致"模型调用名"与"工具结果名"对不上，
    上游会报 tool_use 名称不匹配；漏改 tool_choice 则强制选择指向不存在的工具。
    """
    changed: list[str] = []
    tools = payload.get("tools")
    if isinstance(tools, list):
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            name = fn.get("name")
            if name in TOOL_RENAMES:
                fn["name"] = TOOL_RENAMES[name]
                if name in DESC_OVERRIDES:
                    fn["description"] = DESC_OVERRIDES[name]
                changed.append("%s->%s" % (name, fn["name"]))

    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict) and fn.get("name") in TOOL_RENAMES:
                    fn["name"] = TOOL_RENAMES[fn["name"]]
        elif role == "tool" and msg.get("name") in TOOL_RENAMES:
            msg["name"] = TOOL_RENAMES[msg["name"]]
        elif role == "system":
            msg["content"] = _replace_names_in_content(msg.get("content"))

    choice_change = _sync_tool_choice(payload)
    if choice_change:
        changed.append(choice_change)
    return changed


def _restore_in_message(msg: Any, declared: set[str]) -> list[str]:
    """响应侧改回原名；解析不了的未知工具直接移除，避免宿主空等。"""
    dropped: list[str] = []
    tcs = getattr(msg, "tool_calls", None)
    if not tcs:
        return dropped
    kept = []
    for tc in tcs:
        fn = getattr(tc, "function", None)
        raw = getattr(fn, "name", None) if fn is not None else None
        resolved = _resolve_name(raw or "", declared)
        if resolved is None:
            dropped.append(str(raw))
            continue
        fn.name = resolved
        kept.append(tc)
    if dropped:
        try:
            msg.tool_calls = kept or None
        except Exception:  # 模型对象只读时放弃，不影响主链路
            pass
    return dropped


def _restore_in_chunk(chunk: Any, declared: set[str], dropped_idx: set) -> list[str]:
    """流式逐块改名。

    工具名只在首个分片出现，参数是后续分片。未声明的工具一旦判定要丢弃，
    必须把同 index 的后续参数分片一起掐掉，否则宿主会拿到半个调用。
    丢弃标记按 (choice 序号, 工具序号) 记，避免 n>1 时不同 choice 的同号工具串味。
    """
    dropped: list[str] = []
    for ch in (getattr(chunk, "choices", None) or []):
        delta = getattr(ch, "delta", None)
        if delta is None:
            continue
        tcs = getattr(delta, "tool_calls", None)
        if not tcs:
            continue
        choice_index = getattr(ch, "index", 0)
        kept = []
        for tc in tcs:
            key = (choice_index, getattr(tc, "index", 0))
            fn = getattr(tc, "function", None)
            raw = getattr(fn, "name", None) if fn is not None else None
            if not raw:
                # 参数分片：沿用该 index 先前的判定
                if key in dropped_idx:
                    continue
                kept.append(tc)
                continue
            resolved = _resolve_name(raw, declared)
            if resolved is None:
                dropped_idx.add(key)
                dropped.append(str(raw))
                continue
            fn.name = resolved
            kept.append(tc)
        if len(kept) != len(tcs):
            try:
                delta.tool_calls = kept
            except Exception:
                pass
    return dropped



class BridgeGuard(CustomLogger):
    """转发前的最后一道防线。

    注意：LiteLLM 里 `async_pre_call_hook` 运行在"选定 deployment 之前"，
    改的是客户端原始请求体，改动不会带到上游；要改发往上游的请求体，
    必须实现 `async_pre_call_deployment_hook`（见 litellm/utils.py 的说明）。
    """

    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type: Any) -> dict:
        try:
            renamed = _rename_forward(kwargs)
            aliased = _declare_builtin_aliases(kwargs)
            _log("转发上游前", kwargs)
            if renamed or aliased:
                _log_event("工具改名", {"renamed": renamed, "aliased": aliased,
                                        "tools_after": _tool_names(kwargs)})
            _dump(kwargs)
            messages = kwargs.get("messages")
            if isinstance(messages, list) and messages:
                if not _has_guard(messages):
                    _inject_guard(messages, _pick_guard(kwargs))

            _ensure_min_tokens(kwargs)
            _clamp_max_tokens(kwargs)
        except Exception as exc:  # 加固逻辑绝不能阻断主链路
            print(f"[bridge_guard] 预处理异常，已放行原始请求: {exc}")
        return kwargs

    async def async_post_call_success_hook(
        self, data: dict, user_api_key_dict: UserAPIKeyAuth, response: Any,
    ) -> Any:
        try:
            declared = _client_declared(data)
            for ch in (getattr(response, "choices", None) or []):
                msg = getattr(ch, "message", None)
                if msg is None:
                    continue
                dropped = _restore_in_message(msg, declared)
                if dropped:
                    # 工具全被丢弃时，若仍报 tool_calls，宿主会去等结果
                    if not getattr(msg, "tool_calls", None):
                        try:
                            ch.finish_reason = "stop"
                        except Exception:
                            pass
                    _log_event("拦截未知工具", {
                        "model": data.get("model"), "dropped": dropped,
                        "declared": sorted(declared)[:40]})
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge_guard] 响应名还原异常: {exc}")
        return response

    # ---------- 响应侧：判定"模型到底有没有拿到工具" ----------

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            info = _resp_summary(response_obj)
            info["model"] = kwargs.get("model")
            info["elapsed_s"] = round((end_time - start_time).total_seconds(), 1)
            _log_event("上游成功", info)
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge_guard] 成功日志异常: {exc}")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            exc = kwargs.get("exception")
            info = {
                "model": kwargs.get("model"),
                "error_type": type(exc).__name__ if exc else None,
                "error": str(exc)[:600] if exc else None,
                "status": getattr(exc, "status_code", None) if exc else None,
            }
            _log_event("上游失败", info)
        except Exception as e:  # noqa: BLE001
            print(f"[bridge_guard] 失败日志异常: {e}")

    async def async_post_call_failure_hook(
        self, request_data: dict, original_exception: Exception,
        user_api_key_dict: UserAPIKeyAuth, traceback_str: str | None = None,
    ):
        try:
            _log_event("代理层失败", {
                "model": request_data.get("model"),
                "error_type": type(original_exception).__name__,
                "error": str(original_exception)[:600],
                "status": getattr(original_exception, "status_code", None),
                "tool_count": len(_tool_names(request_data)),
            })
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge_guard] 失败钩子异常: {exc}")

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data: dict,
    ):
        """流式响应逐块统计与改写。

        记录两项用于定位"思考卡住"：
        - reasoning_len：思考增量字符数。它远大于 content_len 时，客户端只在转圈。
        - unknown_tools：模型返回了、但客户端根本没声明的工具名（如 webReader）。

        改写两类工具名：
        - 延迟 MCP 对应的改名还原（SearchDeferredMcpTools -> ToolSearch）
        - 内置工具别名映射（webReader -> WebFetch）
        完全无法映射的直接丢弃，并把 finish_reason 归位成 stop——
        否则宿主会拿着一个永不存在的工具名去执行，界面永远停在「执行中」。
        """
        chunks = 0
        raw_names: list[str] = []
        names: list[str] = []
        text_len = 0
        reasoning_len = 0
        finish = None
        declared = _client_declared(request_data)
        dropped_idx: set = set()
        dropped: list[str] = []
        try:
            async for chunk in response:
                chunks += 1
                try:
                    ch = (getattr(chunk, "choices", None) or [None])[0]
                    if ch is not None:
                        delta = getattr(ch, "delta", None)
                        if delta is not None:
                            for tc in (getattr(delta, "tool_calls", None) or []):
                                fn = getattr(tc, "function", None)
                                nm = getattr(fn, "name", None) if fn is not None else None
                                if nm and nm not in raw_names:
                                    raw_names.append(nm)
                            text_len += len(getattr(delta, "content", None) or "")
                            reasoning_len += len(
                                getattr(delta, "reasoning_content", None) or "")
                    # 先记原始名，再改名 / 拦截
                    dropped.extend(_restore_in_chunk(chunk, declared, dropped_idx))
                    if ch is not None:
                        delta = getattr(ch, "delta", None)
                        for tc in (getattr(delta, "tool_calls", None) or []):
                            fn = getattr(tc, "function", None)
                            nm = getattr(fn, "name", None) if fn is not None else None
                            if nm and nm not in names:
                                names.append(nm)
                        if getattr(ch, "finish_reason", None):
                            finish = ch.finish_reason
                            if dropped_idx and not names:
                                try:
                                    ch.finish_reason = "stop"
                                    finish = "stop"
                                except Exception:
                                    pass
                except Exception:  # 单块解析失败不影响转发
                    pass
                yield chunk
        finally:
            _log_event("流式完成", {
                "model": request_data.get("model"),
                "chunks": chunks,
                "tool_calls": names,
                "raw_tool_calls": raw_names,
                "mapped_tools": [n for n in raw_names
                                 if n not in declared and n not in dropped],
                "dropped_tools": sorted(set(dropped)),
                "content_len": text_len,
                "reasoning_len": reasoning_len,
                "finish_reason": finish,
                "tool_count_sent": len(_tool_names(request_data)),
            })

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: str,
    ) -> dict:
        """只做记录，不改写。

        纠偏注入与工具改名统一放在 async_pre_call_deployment_hook 里做：
        那里能拿到改名后的真实工具名，两处都注入会让规范里的工具名与实际不符。
        """
        try:
            _log("客户端到达", data)
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge_guard] 预处理异常，已放行原始请求: {exc}")
        return data


proxy_handler_instance = BridgeGuard()
