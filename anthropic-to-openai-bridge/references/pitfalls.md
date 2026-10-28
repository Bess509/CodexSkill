# 坑点详解

每条按「现象 → 证据 → 根因 → 处置」写。证据都来自实测日志，不是推断。

---

## 1. 把「模型说没有工具」误判成「宿主没下发工具」

**现象**：模型回复「反复检索了工具列表，确实没有 Read/Grep/Glob/Bash，剩下的都是邮件、
表格、建站、LSP 这类专用工具」，并给出「你的工具没加载」的诊断。

**证据**：请求侧日志显示宿主下发了 24 个工具，
`Read/ViewImage/Write/Edit/Glob/Grep/Bash/PowerShell/...` 全在，
「客户端到达」与「转发上游前」两个阶段完全一致。

**根因**：当时只记了请求侧，没有响应侧日志，无法判断模型到底拿到什么，
于是把模型的自我陈述当成了事实。模型的陈述只是它的信念，不是链路状态。

**处置**：先装三层日志（见 `diagnostics.md`），用数据说话。工具在不在、
模型调没调、上游报没报错，是三个独立问题，必须分开取证。

---

## 2. `ToolSearch` 这个工具名会把模型带偏（本 SOP 最隐蔽的坑）

**现象**：工具全在，模型却连续 15 轮调用 `ToolSearch`，`msg_count` 每轮 +2
（工具调用 + 工具结果），最后放弃并输出「没有文件读取工具」。

**证据**（同一句「读一下本地文件」，各重复 5 次，看是否直接调用 `Read`）：

| 工具集 | 直接调 `Read` | 明细 |
|---|---|---|
| 含原名 `ToolSearch` | 1/5 | `ToolSearch, ToolSearch, ToolSearch, Read, ToolSearch` |
| 只改描述、保留原名 | 4/5 | 时好时坏，不可接受 |
| 改名 `SearchDeferredMcpTools` | 5/5 | `Read` ×5 |
| 移除 `ToolSearch` 与 `DeferExecuteTool` | 5/5 | `Read` ×5 |

**根因**：`ToolSearch` 的语义是「先检索、再执行」的延迟加载范式。模型一旦看到这个工具，
就认为原生工具也是延迟挂载的，必须先检索才能用。而 `ToolSearch` 实际只返回 MCP 延迟工具，
于是「检索结果里没有 Read」被模型理解成「本会话没有 Read」。

**处置**：双向改名，保留 MCP 能力。直接移除虽然同样 5/5，但会让邮件、表格、建站等
延迟工具全部不可用，不采用。

| 原名 | 改名后 | 理由 |
|---|---|---|
| `ToolSearch` | `SearchDeferredMcpTools` | 名字自带「只查 MCP」限定，不再暗示原生工具要检索 |
| `DeferExecuteTool` | `InvokeDeferredMcpTool` | 与上者配成一对，语义闭环 |

改名必须三处同步，否则上游会报工具名不匹配：

1. `tools` 里的工具定义（含 `description` 改写为 MCP 专用语义）
2. 历史消息中 `assistant.tool_calls[].function.name`
3. `system` 文本里出现的工具名

响应侧再把名字转回原名，客户端全程只看到它认识的名字。实现见 `callbacks.md`。

**注意**：改名只影响发给上游的请求，`WorkBuddy` 侧配置无需改动。

---

## 3. 改请求体的钩子选错，改动不生效

**现象**：纠偏文本注入了、日志也记了，可上游收到的请求就是没变。

**根因**：LiteLLM 的 `async_pre_call_hook` 运行在「选定 deployment 之前」，
它改的是客户端原始请求体，改动不会带到上游。要改发往上游的请求体，
必须用 `async_pre_call_deployment_hook`。

**处置**：所有改上游请求的逻辑（纠偏注入、工具改名、`max_tokens` 兜底）
统一放 `async_pre_call_deployment_hook`；`async_pre_call_hook` 只做记录。
两处都写会出现「规范里的工具名与实际不符」——`pre_call_hook` 记的是改名前的名字。

---

## 4. 报 `No connected db`，其实是鉴权失败

**现象**：请求返回 400，`{"error": {"message": "No connected db.", "type": "no_db_connection"}}`。

**根因**：代理设了 `master_key`，但客户端带来的 key 与它不一致。LiteLLM 认定这不是
master key，于是尝试去数据库校验虚拟 key，而本地没有配置数据库，就抛出这个错。

**处置**：核对 `.env` 的 `PROXY_MASTER_KEY` 与 `models.json` 的 `apiKey` 是否完全一致。
看到这个报错先查 key，不要去找数据库。

---

## 5. `max_retries: 2` 让一次测试看起来像三次

**现象**：日志里出现 3 条几乎相同的请求，间隔 10–20 秒，容易被误读成「用户测了三次」。

**根因**：`max_retries: 2` 表示最多再重试 2 次，合计 3 次上游调用。
上游持续失败时会连续打满。

**处置**：判断「是重试还是多次测试」看两点——间隔是否在秒级、`msg_count` 是否相同。
两者都符合就是重试。真正要查的是响应侧的 `上游失败` 记录里的错误内容。

---

## 6. 用单次实验下结论

**现象**：同一份配置，第一次测模型直接调 `Read`，第二次却去检索工具。

**根因**：模型采样有随机性，工具选择在边界情况下不稳定。

**处置**：每项至少重复 5 次，看命中率而不是看单次结果。本 SOP 里所有
「改名前 1/5、改名后 5/5」的结论都基于 5 次重复。

---

## 7. 思考块吃掉正文

**现象**：回复为空、或只有半句话，`finish_reason` 是 `length`。

**根因**：这类模型先输出思考块，再输出正文。客户端给的 `max_tokens` 偏小时，
预算被思考块耗尽。

**处置**：代理层强制输出下限，`MIN_OUTPUT_TOKENS` 不低于 1600。

---

## 8. base 地址带路径导致双路径 404

**现象**：请求 404，路径形如 `/v1/v1/messages` 或 `/v1/messages/v1/messages`。

**根因**：`api_base` 本应只到域名/端口，转换器自己会拼 `/v1/messages`；
用户常把文档里的完整端点直接填进去。

**处置**：归一化时循环剥离 `/v1/messages`、`/messages`、`/v1/chat/completions`、
`/chat/completions`、`/v1` 这些尾巴，直到稳定。

---

## 9. 上游偶发 502 被误判为配置错误

**现象**：`litellm.InternalServerError: AnthropicException - 502, message='Bad Gateway'`。

**根因**：网关瞬时故障，与本地配置无关。

**处置**：重试即可。若连续多次 502 才考虑网络或上游额度问题。

---

## 10. LiteLLM 给函数工具硬编码 `type: "custom"`，严格网关直接 422

**现象**：同一个网关，GLM / MiniMax 带工具正常，只有 deepseek 一给 `tools` 就 422：

```
litellm.BadRequestError: AnthropicException -
Failed to deserialize the JSON body into the target type:
tools[0]: unknown variant `custom`, expected `web_search_20250305`
or `web_search_20260209`
```

**证据**：绕过 LiteLLM 直连上游，手工构造同一条请求：

| tools 结构 | deepseek-v4-flash | MiniMax-M3 |
|---|---|---|
| `{name, description, input_schema}`（无 `type`） | `stop_reason=tool_use`，正常返回 | 正常 |
| 多一个 `type: "custom"` | **422**，即上面那段报错 | 正常（容忍） |

结论：上游网关支持自定义工具，只是它的反序列化只接受带类型的服务端工具
（`web_search_*`），不接受 `custom` 这个变体。

**根因**：`litellm/llms/anthropic/chat/transformation.py` 在构造 `AnthropicMessagesTool`
时把 `type="custom"` 写死了（验证版本 LiteLLM 1.106.4，源码约 789/795 行，
以 `def _map_tools` 函数为准 grep 定位，行号随版本漂移）。Anthropic 官方接口
接受该字段，第三方兼容网关不一定——这类网关按 Rust/强类型反序列化，多一个未知
variant 就整条请求失败。

**处置**：在工具映射完成后删掉该字段。函数工具省略 `type` 完全合法
（Anthropic 缺省即视为 custom），对本来容忍该字段的模型没有副作用。

```python
from litellm.llms.anthropic.chat.transformation import AnthropicConfig
_original = AnthropicConfig._map_tools

def _map_tools(self, tools):
    anthropic_tools, mcp_servers = _original(self, tools)
    for tool in anthropic_tools:
        if isinstance(tool, dict) and tool.get("type") == "custom":
            tool.pop("type", None)          # 只删函数工具，服务端工具必须保留类型
    return anthropic_tools, mcp_servers

AnthropicConfig._map_tools = _map_tools
```

**注意**：补丁要打在「工具映射之后、发请求之前」。`async_pre_call_deployment_hook`
拿到的是 OpenAI 格式的 `kwargs`，那时还没映射，删不到这个字段——必须包住
`_map_tools`。补丁的加载时机放在回调模块的导入处（`bridge_guard` 顶层调用），
因为该模块随 `config.yaml` 的 `callbacks` 在代理启动时必然被导入；如果关掉
`GUARD_ENABLED`，回调不再挂载，补丁也会跟着失效。

**版本锁定**：补丁是对 LiteLLM 内部函数的 monkey patch，依赖 `_map_tools` 的
存在与签名。验证时的版本是 **LiteLLM 1.106.4**（脚手架 README 的安装命令已
固定 `pip install litellm==1.106.4`）。升级 LiteLLM 前必做三件事：

1. 读上游源码确认 `_map_tools` 仍在且返回 `(anthropic_tools, mcp_servers)` 二元组
2. 升级后重跑验收第 2 条（带 tools 的非流式请求），422 复现即补丁失效
3. 若官方已修复（不再写死 `type: custom`），直接删掉 `compat_patch.py`，
   以官方实现为准

补丁静默失效的症状：原配置原网关，升级后某模型一给 tools 就 422——
别去怀疑模型和网关，先查补丁。

**判断口诀**：同网关下「只有一个模型带工具报 422、报错里出现 unknown variant」，
先怀疑字段兼容，别急着怀疑模型不支持工具。

---

## 11. 能力字段照抄介绍页，填出来的配置一半是错的

**现象**：`supportsImages` 填了 true，模型却对着图片答「看不到图」；`maxOutputTokens`
填得比上游允许的大，长输出被上游 400 拦下。

**证据**（同一网关三个模型，实测而非查文档）：

| 模型 | 工具 | 三张纯色图 | 视觉判定 | 输出上限（报错原文） |
|---|---|---|---|---|
| deepseek-v4-flash | 打补丁后正常 | 3/3 正确 | 支持 | 上游不校验，2,000,000 也接受 |
| MiniMax-M2.7 | 正常 | 0/3，三张都回「看不到图」 | 不支持 | `does not support max tokens > 196608` |
| MiniMax-M3 | 正常 | 3/3 正确 | 支持 | `does not support max tokens > 524288` |

**根因**：同一个网关背后挂着不同厂商的模型，能力差异极大；`/v1/models`
通常只返回 id 和名字，不返回上下文长度、是否多模态。

**处置**：填 `models.json` 前逐个跑 `scripts/probe_model_caps.py`（六项全探测：
文本、工具、视觉、输出上限、输入下限、推理）。几个判定要点：

1. **视觉要问「看不到就直说」**，否则模型会顺着你的问题编一个颜色。三张图 0/3
   且明确回「看不到图」才算不支持；乱猜颜色同样判为不支持。
2. **`maxOutputTokens` 从报错里读**，报错没写就取保守值，不要按介绍页填。
3. **`maxInputTokens` 只能拿下限**（发 400k / 800k 字符看是否通过），配置值取
   不高于实测下限的保守数——填大了会在长对话中途被上游 400 打断，填小了只是提前压缩。
4. **`supportsReasoning` 看响应里有没有 `reasoning_content`**，没有就别开，
   开了界面只会显示一块空白的思考区。

## 12. 模型调用它自家的内置工具，宿主永远等不到结果

**现象**：界面停在「思考中」，下面挂一行灰色的「执行中 webReader」，一直不动。
同一会话里模型还会输出格式化得很漂亮的执行块：

```
**🌐 Z.ai Built-in Tool: webReader**
**Input:** {"url": "http://httpbin.org/uuid", ...}
*Executing on server...*
**Output:** **webReader_result_summary:** [{"text": {"url": "...", "content": "...uuid..."}}]
```

**证据**（代理日志 + 一次可核对的实验）：

- 该轮 `tools` 里只有 `WebFetch`，没有 `webReader`；日志 `unknown_tools=['webReader']`
- 连续 6 轮让 GLM 读 `httpbin.org/uuid`，每轮返回的 uuid **完全相同**（`3d28ce67-...`），
  而本地实测真值是 `0a6a0c35-...`。同一接口两次返回同一个 uuid 在物理上不可能，
  这些内容是模型按照自己的格式模板编出来的
- `finish_reason=stop` 却同时带着 tool_calls，正文 600–900 字，思考 2000–5500 字
- 只给 `Read`、不给任何联网工具时，模型会老实回答「无法获取」，并不编造

**根因**：GLM 在自家平台上有服务端内置工具（`webReader`、`web_search`），模型对它们
有很强的习惯性调用。经第三方网关接入时，这些工具不会真的存在，但模型照旧发起调用、
并顺手把「执行过程和结果」写进正文。宿主收到一个自己不认识的名字，找不到执行器，
promise 永远不落地——界面就卡在「执行中」。这类名字还会被模型当成"内建能力"，
连 system 里禁掉都不一定听。

**处置**：两头一起做，少一头都不行。

1. **请求侧补别名**：宿主声明了 `WebFetch` 时，往 `tools` 里再塞一个名为 `webReader`
   的工具，schema 与 `WebFetch` 对齐（只要 `url` + `prompt`，并注明不要传
   `retain_images` / `return_format`）。这样模型调用时参数落在可执行范围内。
   实测：补了 schema 后，非流式返回的参数就是干净的 `{"url":..., "prompt":...}`。
2. **响应侧映射**：把 `webReader` 改回 `WebFetch`，宿主才能真正执行、拿到真实内容。
   只做第 1 步不做第 2 步，宿主依旧不认识 `webReader`；只做第 2 步不做第 1 步，
   模型可能按记忆传一堆多余参数，执行时校验失败。
3. **映射不到就丢弃**：目标工具不在本轮 `tools` 里时，把该 tool_call 及其后续参数分片
   一起清掉，并把 `finish_reason` 从 `tool_calls` 归位成 `stop`。否则宿主会去等一个
   永不返回的工具，照样卡死。工具名只在首个分片出现，参数是后续分片，必须按 `index`
   记住"这个调用已被丢弃"，否则宿主会收到半个残缺调用。
4. **短名对全限定名**：模型常把 `mcp__web_reader__webReader` 写成 `webReader`。
   解析顺序应为：精确匹配 → 内置别名 → `declared` 里以 `__短名` 结尾的项 → 丢弃。

**别做的事**：不要为了避免卡死而直接删掉 `WebFetch` 之类的联网工具。模型会转而
硬调内置工具，卡死照旧，还少了真实可用的执行通道。

**校验**：拿一句「用 webReader 抓 httpbin.org/uuid」硬碰，看客户端拿到的是
`WebFetch`（映射成功）还是空调用 + `stop`（丢弃成功）。两种都算过，
只有拿到 `webReader` 才是没过。

**怎么判定是真抓还是编的**：别看 uuid 新不新——模型完全有能力每次编一个新的。
唯一可靠的是看**请求消息数有没有推进**：

| 现象 | 含义 |
|---|---|
| 请求 `msg_count` 从 N 变成 N+2，且新增一条 `role=tool` 消息 | 宿主真的执行了工具，结果被回传 |
| `msg_count` 原地不动、反复出现同名请求 | 宿主没执行，在重试，内容不可信 |
| `role=tool` 的消息内容是"从提供的 JSON 中提取…"这类摘要 | 工具真实执行 + 宿主侧二次提炼，可信 |

再配合两个旁证：`finish_reason=tool_calls`（不是 `stop` 却带 tool_calls），
以及从发起调用到回传结果的耗时有几秒（真实网络请求 + 提取），编造通常是瞬时的。
完整请求体转储里能直接看到那条 `role=tool`，比任何口头结论都硬。

## 13. `max_tokens` 给大了不是被截断，是整轮 400

**现象**：某个模型偶发整轮失败，报错形如
`[1210][max_tokens参数非法：限制数值范围[1,131072]]`，
同网关别的模型没事。

**根因**：上限是**按模型**设的，网关背后挂着不同厂商的模型，各不一样。客户端传一个
超过上限的值，上游直接 400，整个请求失败——不是把输出截到上限。

**证据**（经代理逐档试探 glm-5.3）：

| max_tokens | 结果 |
|---|---|
| 65536 / 100000 / 131072 | 通过 |
| 131073 | 400 `限制数值范围[1,131072]` |

**处置**：在请求侧按模型夹紧，超限就改成上限值，宁可截断也不要整轮失败。
已实现在 `bridge_guard.py` 的 `_clamp_max_tokens`（内置实测表 `MAX_OUTPUT_CAPS`，
可用环境变量 `BRIDGE_MAX_OUTPUT_CAPS`（JSON 对象）追加模型）：

```python
MAX_OUTPUT_CAPS = {"glm-5.3": 131072, "MiniMax-M2.7": 196608, "MiniMax-M3": 524288}
model = str(data.get("model") or "").split("/")[-1]   # 上游模型名可能带 anthropic/ 前缀
if (cap := MAX_OUTPUT_CAPS.get(model)) and cur > cap:
    data["max_tokens"] = cap
```

要点：

- 模型名要剥前缀，日志里是 `anthropic/glm-5.3`，配置表里是 `glm-5.3`
- **没实测过上限的模型不要设限**，猜小了会无端截断，猜大了等于没做
- 上限值从报错原文里读，报错没写就一档档试（131073 这种边界值最好用）

## 14. 旧代理没退干净，改动静默不生效

**现象**：改完代码重启，端口 4000 上还有响应，但改动静默不生效；启动日志出现
`Uvicorn running on ...:10722` 之类非预期端口。

**根因**：两处都会造成「新旧并存」。其一（实测复现）：LiteLLM 的 uvicorn 发现
端口被占时不报错，**静默挑一个空闲端口继续跑**——旧代理占着 4000，新代理悄悄
起在别的端口，models.json 指向的 4000 打到旧代码，你以为在测新代码。其二：
父进程被杀后 uvicorn 子进程仍占着端口；脚手架启动脚本（start_proxy.py /
daemon.py）检测到占用会退出，但提示容易错过，直接绕过脚本用 `litellm --config`
启动时则完全没有这层保护。

**处置**：

0. 后台模式下先双击 `stop.bat`。它会探测端口：pidfile 里的 pid 已死但端口仍被
   监听时，打印占用者 pid、进程链与清理命令，确认后连同整棵垫片链一起清掉。
   这是「停不掉也起不来」死角的正规出口
1. 重启前先确认端口确实空了：`netstat -ano | grep ":4000"`，有输出就 `taskkill //F //PID <pid>`
2. 启动后核对监听端口是不是 4000，不是就说明有残留
3. 看到 `Uvicorn running on ...:<非预期端口>`，先清进程再起，别继续测
4. 判定"改动静默不生效"时，第一反应是查"我测的是哪个进程"；怀疑代码逻辑放在这之后

## 15. 探测脚本突然连不上上游，报 CERTIFICATE_VERIFY_FAILED

**现象**：以前能连的网关，现在 `detect_models.py` / `probe_upstream.py` 一跑就失败，
报文里带 `CERTIFICATE_VERIFY_FAILED` 或 `certificate verify failed`。

**根因**：早期版本的探测脚本把证书校验写死成关闭（`CERT_NONE` + `check_hostname=False`），
任何自签证书都能连上。现在默认开启校验，证书过期、域名与 SAN 不符、自签名都会失败。
这是刻意收紧，不是回归——把校验关掉等于允许中间人替换上游，令牌和对话内容都在明文里。

**处置**（按优先级）：

1. 私有 CA：`--ca <证书.pem>`，或在 `.env` 写 `UPSTREAM_CA_BUNDLE=<证书.pem>`
2. 证书链不完整 / 已过期：修服务端证书。这是唯一长期正确的做法
3. 仅临时排查：`--insecure`。它会同时关掉主机名校验，排查完立刻去掉

注意 `.env` 里的 `UPSTREAM_CA_BUNDLE` 只被探测脚本读取；LiteLLM 自身的上游连接走
`ssl_verify` 参数，需要时在 `config.yaml` 的 `litellm_params` 里显式配置证书路径。

## 16. 安装目录含中文，后台模式启动即崩

**现象**：`start_background.bat` 一闪而过，`logs/daemon-*.log` 里是
`UnicodeEncodeError: 'ascii' codec can't encode characters ...`。

**根因**：pidfile 里存的是绝对路径（`script` / `exe` / `root`），早期版本用
`json.dumps(..., ensure_ascii=False)` 序列化却按 `encoding="ascii"` 写盘，中文目录名
在写入这一步就抛异常；这一步在 `daemon.py` 的 try 之外，崩了也没人兜底。读侧
（`daemon_ctl.read_identity`）同理由 ASCII 解码，路径会变成替换符，身份核对必然失败。

**处置**：写读两侧统一 UTF-8（读侧 `utf-8-sig` 兼容记事本 BOM），旧版纯数字 pidfile 是
ASCII 子集，照旧可读。**不要**改回 `ensure_ascii=True` 图省事——路径里的中文会变成
`\uXXXX` 转义，人能看但脚本比对时容易出岔子。

## 17. `stop.bat` 报「无法确认身份」，坚持不杀

**现象**：`status` 显示 daemon 在跑，`stop` 却打印
`[拒绝] 无法确认 pid=xxxx 就是本脚手架的 daemon：...`，然后什么都不做。

**根因**：这是**有意设计**。新版 pidfile 记录 pid / created / exe / script / root 五项，
终止前逐项核对。以下任一情况都会拒绝：身份字段缺失（老版本写下的文件被手工改过）、
进程侧取不到启动时间、命令行里的解释器与记录不符、启动时间相差超过 5 秒（pid 被复用）、
记录的 `root` 与当前安装目录不一致。

**两个容易误判的子情形**：

- venv 的 `python.exe` 是转发器，`Win32_Process.ExecutablePath` / `/proc/<pid>/exe`
  报的是**基础解释器**，与 `sys.executable` 天然不同。所以解释器比对基准是
  「命令行第一个 token」，不是镜像路径。部分 venv 连命令行也会改成基础解释器；
  daemon 写身份时直接采集当前 OS 命令行与创建时间，避免与复核采用不同基准。
  取不到完整证据就拒绝创建身份文件，不通过猜测路径放行。
- `start_background.bat` 用相对路径 `daemon.py` 启动，命令行里没有目录。能拿到进程
  工作目录时（POSIX）要求它等于脚手架根目录；拿不到时退一步要求「解释器与记录一致 +
  记录的脚本文件存在」。**绝对路径**指向别处的同名脚本永远不会被接受。

**处置**：按提示逐条核对，确认无误后手动 `taskkill /F /T /PID <pid>`，或删掉
`logs/daemon.pid` 再启动。宁可多一步手动确认，也不赌一次误杀。

## 18. 打开转储后多模态请求开始失败

**现象**：开 `BRIDGE_DUMP_ENABLED=true` 之后，带图片的请求报上游 400，或模型答"没看到图"；
关掉转储立刻恢复正常。

**根因**：脱敏函数早期版本**原地改写** `{"type":"image","source":{"data":...}}` 里的
base64。转储快照与转发请求共享同一批对象（`payload["messages"]` 是同一个 list），
占位串 `<redacted image base64, N chars>` 就这样被发给了上游。图片块还有 OpenAI 形态
`{"type":"image_url","image_url":{"url":"data:..."}}` 和字符串形态，都要覆盖。

**处置**：脱敏改为纯函数——dict / list 一律新建后返回，入参一个字段都不动；转储只影响
落盘副本。排查完记得关掉开关并删除 `logs/last_request.json`。

## 19. 孤儿清理把无关进程一起收了

**现象**：`stop.bat` 清理残留代理时，把一个不相关的开发服务（同机跑的 uvicorn）连同
其它子进程一起杀掉了。

**根因**：只校验了**监听者**是否属于本脚手架，然后用 `_pick_kill_root` 沿父链往外找
"像代理的进程"作为清理根——判据是命令行里出现 `uvicorn` / `litellm` 字样。父进程恰好
是同名的开发服务时就被选中，`/T` 连带它的其它子进程一起走。

**处置**：

1. 清理根节点只能从**确认属于本脚手架**的进程里挑。归属判断先解析 `..` 与本机
   目录链接，再按路径边界比对；`.../bridge-old`、`bridge/../other` 都不属于本实例。
2. 挑不出来就拒绝自动清理，只打印手动命令
3. 发信号前**重新核对**目标：归属、系统进程白名单、可执行路径、命令行与 OS 创建时间。
   创建时间必须与选中目标时的快照一致；相同命令重启后也是另一个进程，不能终止。
   证据缺失或变化就放弃，不能沿用 pidfile 的秒级时间容差。
4. 非交互环境（脚本/CI）永远只打印命令，不静默杀进程

身份核对只匹配 Python 实际执行的脚本参数，解析完整路径后比较；`daemon.py.backup`
或作为其他脚本数据参数出现的 `daemon.py` 都不算本 daemon。相对路径有 cwd 时按 cwd
解析；Windows 取不到 cwd 时，只兼容 `daemon.py` / `./daemon.py` 的简单写法，仍要求
记录的脚本存在且解释器匹配。新增边界回归见 `tests/test_process_boundaries.py`。
