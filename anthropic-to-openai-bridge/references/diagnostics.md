# 排障方法

## 三层取证

出问题时不要靠猜，也不要采信模型的自我陈述。装三层日志，把「宿主发了什么」
「我们发了什么」「上游回了什么」分开记录。

| 层 | 记录位置 | 关键字段 | 回答的问题 |
|---|---|---|---|
| 客户端到达 | `async_pre_call_hook` | `tool_count`、`tools`、`msg_count`、`system_len` | 宿主到底下发工具了吗 |
| 转发上游前 | `async_pre_call_deployment_hook` | 同上 + `system_len` 变化 | 我们的处理有没有弄丢或改坏 |
| 响应侧 | `async_log_success_event` / `async_log_failure_event` / 流式钩子 | `finish_reason`、`tool_calls`、`chunks`、`error`、`status` | 模型真调了工具吗，上游报错了吗 |

再加一份完整请求体转储（含工具 schema 与 system 全文），用于离线复现。

## 症状对照表：模型说「没有工具」

| 日志表现 | 结论 | 下一步 |
|---|---|---|
| 无任何新记录 | 请求没到代理 | 查 models.json 是否被项目级配置覆盖、url 是否通 |
| `tool_count = 0` 且 system 里描述了工具 | 宿主走文本协议 | 按文本协议分支注入纠偏 |
| `tool_count = 0` 且 system 没提工具 | 宿主没下发 | 查 `supportsToolCall`、会话模式（Ask/Plan 不下发写类工具） |
| `tool_count > 0`，`上游失败` | 上游拒绝 | 看错误文本：参数、上下文超限、schema 不兼容 |
| `tool_count > 0`，`上游成功` 但 `tool_calls = []` | 模型收到工具却不调 | 查工具命名诱导（见 `pitfalls.md` 第 2 条） |
| `tool_count > 0`，`tool_calls = ['Read']` | 模型调了，问题在宿主执行 | 转去查宿主侧回传与执行 |
| 多条相同请求、间隔秒级 | 是重试不是多次测试 | 看 `上游失败` 的错误内容 |

## 隔离实验

定位「为什么模型不调某个工具」这类问题，只有隔离实验能给出结论。规范：

1. **单变量**：一次只改一处（工具名、描述、工具数量、system 有无），其余全部固定
2. **重复 5 次**：模型的工具选择有随机性，单次结果不可信
3. **同一句提示**：所有变体用完全相同的用户输入
4. **看命中率**：比较「直接调用目标工具」的比率，不比较个别样本
5. **留控制组**：把原始配置也跑 5 次，作为基线

实验模板（伪代码）：

```python
CASES = [
    ("基线", 原始工具集),
    ("仅改描述", 原始工具集 + 改写 description),
    ("改名", 原始工具集 + 替换 name),
    ("移除", 去掉可疑工具),
]
for label, tools in CASES:
    hits = 0
    for _ in range(5):
        r = client.chat.completions.create(model=M, messages=[{"role": "user", "content": PROMPT}],
                                           tools=tools, tool_choice="auto")
        if first_tool_name(r) == "Read":
            hits += 1
    print(label, f"{hits}/5")
```

实验时注意三点：

- 上游偶发 502，单次失败要重试，不能计入结果
- 工具 schema 要贴近真实形态（嵌套对象、enum、默认值），过度简化的 schema 会得出错误结论
- 结论以「命中率」表达，不要写「可以工作」这种无法验证的话

## 代理启停

命令里的 4000 是默认端口，实际按 `.env` 的 `PROXY_PORT` 替换。

```bash
# 健康检查（就绪判据）
curl -s --noproxy "*" http://127.0.0.1:4000/health/liveliness

# 端口占用排查（Windows），<port> 按 PROXY_PORT 替换
netstat -ano | grep ":4000" | grep LISTENING
taskkill //F //PID <pid>
```

`Uvicorn running` 打印完还要几秒才真正能接请求，务必用健康检查确认。
自检脚本里对 `127.0.0.1` 的请求要显式禁用系统代理（`trust_env=False` 或 `--noproxy "*"`），
否则会被本机 HTTP_PROXY 劫持。
