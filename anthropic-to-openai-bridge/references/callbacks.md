# 钩子选型与实现要点

## 钩子能力对照

在 `litellm_settings.callbacks` 里挂一个 `CustomLogger` 实例，按需实现下列方法。
关键区别是「能改什么、什么时候跑」。

| 钩子 | 能改什么 | 何时运行 | 典型用途 |
|---|---|---|---|
| `async_pre_call_hook` | 客户端原始请求体 | 选定 deployment 之前，改动不带入上游 | 只做记录（请求到达快照） |
| `async_pre_call_deployment_hook` | **发往上游的请求体** | deployment 选定之后、发出之前 | 纠偏注入、工具改名、`max_tokens` 兜底 |
| `async_post_call_success_hook` | 非流式响应对象 | 收到完整响应后、返回客户端前 | 把改名后的工具名还原 |
| `async_post_call_streaming_iterator_hook` | 流式响应逐块 | 每个数据块转发前 | 逐块还原工具名 + 统计块数与工具调用 |
| `async_log_success_event` | 不能改，只能记 | 请求完成后（流式是聚合后） | 记 `finish_reason`、工具名、token 用量 |
| `async_log_failure_event` | 不能改，只能记 | 调用失败 | 记异常类型、错误文本、状态码 |
| `async_post_call_failure_hook` | 可返回自定义异常 | 代理层抛错 | 记鉴权失败、请求被拒等 |

核心结论：**要改上游收到的内容，只能用 `async_pre_call_deployment_hook`**。
把逻辑写在 `async_pre_call_hook` 里，日志显示成功、上游却没变化，就是这个原因。

## 工具改名：请求侧

以下为实现要点的讲解版；两处如有出入，以 `assets/scaffold/bridge_guard.py` 为准绳，
升级 LiteLLM 或调整行为时只改实现版，再同步本文档。

```python
TOOL_RENAMES = {
    "ToolSearch": "SearchDeferredMcpTools",
    "DeferExecuteTool": "InvokeDeferredMcpTool",
}
TOOL_REVERSE = {v: k for k, v in TOOL_RENAMES.items()}


def rename_forward(payload: dict) -> list[str]:
    """三处同步改名，缺一处上游就报工具名不匹配。"""
    changed = []
    # 1) 工具定义（同时改写 description 为 MCP 专用语义）
    for t in payload.get("tools") or []:
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if fn.get("name") in TOOL_RENAMES:
            fn["name"] = TOOL_RENAMES[fn["name"]]
            fn["description"] = DESC_OVERRIDES[fn["name"]]
            changed.append(fn["name"])

    # 2) 历史消息里的工具调用名、3) system 文本中的提及
    for msg in payload.get("messages") or []:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name") in TOOL_RENAMES:
                    fn["name"] = TOOL_RENAMES[fn["name"]]
        elif msg.get("role") == "tool" and msg.get("name") in TOOL_RENAMES:
            msg["name"] = TOOL_RENAMES[msg["name"]]
        elif msg.get("role") == "system":
            msg["content"] = _replace_names(msg.get("content"))
    return changed
```

`_replace_names` 需同时处理 `content` 是字符串和是 `[{"type": "text", ...}]` 两种形态。

## 工具改名：响应侧

```python
def resolve(name, declared):                    # 名字解析，解析不了返回 None
    n = TOOL_REVERSE.get(name, name)
    if not declared or n in declared:
        return n
    tgt = BUILTIN_MAP.get(name) or BUILTIN_MAP.get(n)   # webReader -> WebFetch
    if tgt and tgt in declared:
        return tgt
    suffix = "__" + n.lower()                   # webReader -> mcp__web_reader__webReader
    for d in declared:
        if d.lower().endswith(suffix):
            return d
    return None


def restore_in_message(msg, declared) -> list:   # 非流式，返回被丢弃的名字
    dropped, kept = [], []
    for tc in (getattr(msg, "tool_calls", None) or []):
        fn = getattr(tc, "function", None)
        raw = getattr(fn, "name", None) if fn is not None else None
        resolved = resolve(raw or "", declared)
        if resolved is None:
            dropped.append(raw)
            continue
        fn.name = resolved
        kept.append(tc)
    if dropped:
        msg.tool_calls = kept or None
    return dropped


def restore_in_chunk(chunk, declared, dropped_idx) -> list:   # 流式
    dropped = []
    for ch in (getattr(chunk, "choices", None) or []):
        delta = getattr(ch, "delta", None)
        if delta is None:
            continue
        tcs = getattr(delta, "tool_calls", None)
        if not tcs:
            continue
        kept = []
        for tc in tcs:
            idx = getattr(tc, "index", 0)
            fn = getattr(tc, "function", None)
            raw = getattr(fn, "name", None) if fn is not None else None
            if not raw:                          # 参数分片，沿用该 index 的判定
                if idx not in dropped_idx:
                    kept.append(tc)
                continue
            resolved = resolve(raw, declared)
            if resolved is None:
                dropped_idx.add(idx)
                dropped.append(raw)
                continue
            fn.name = resolved
            kept.append(tc)
        if len(kept) != len(tcs):
            delta.tool_calls = kept
    return dropped
```

要点：

- 响应对象是 pydantic 模型，直接给 `fn.name` / `delta.tool_calls` 赋值即可，无需重建对象
- 流式钩子里**先记录原始工具名，再改写**，否则日志里看不到模型真实输出了什么
- **工具名只出现在首个分片**，参数是后续分片。判定为丢弃后必须按 `index` 记住，
  否则后续参数会漏给宿主，变成半个残缺调用
- 全部 tool_call 都被丢弃时，要把 `finish_reason` 从 `tool_calls` 改成 `stop`，
  否则宿主仍然按"有工具要执行"处理，继续等待
- `declared` 要用**客户端原始声明**：我们自己补进去的别名（如 `webReader`）必须剔除，
  否则"未知工具"永远判不成立

## 纠偏文本（system 注入）

按「宿主实际怎么下发工具」分三支，用错分支会让模型主动放弃：

| 宿主下发方式 | 注入内容 |
|---|---|
| 有 `tools` 字段 | 列出本轮实际工具名，声明可直接调用，禁止用检索类工具确认它们是否存在 |
| 无 `tools`，但 system 里描述了工具 | 按 system 给的格式发起调用，不要声称没工具 |
| 都没有 | 声明无能力，要求标注为未核实推测，不许伪造调用结果 |

工具名清单要动态生成（从 `tools` 里取），硬编码会在工具集变化后与实际不符。
去重只按标记字符串判断，不要按内容全等判断。

## 日志字段

请求侧：`stage / model / msg_count / tool_count / tools / tool_choice / max_tokens /
stream / system_len / system_mentions_tools`

响应侧：`chunks / tool_calls / raw_tool_calls / mapped_tools / dropped_tools /
content_len / reasoning_len / finish_reason / error / status / elapsed_s`

`raw_tool_calls` 与 `tool_calls` 不一致，就说明发生了改名或映射；
`dropped_tools` 非空说明模型调了宿主没有的工具，被拦下来了。
`reasoning_len` 远大于 `content_len` 时，客户端表现为"一直在思考"。

完整请求体（含工具 schema 与 system 全文）另存一份单独文件，覆盖写。
只记工具数量不足以复现问题，schema 才是关键输入。
