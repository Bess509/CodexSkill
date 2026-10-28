---
name: anthropic-to-openai-bridge
description: 把仅提供 Anthropic Messages 协议（/v1/messages）的模型接入 WorkBuddy 的完整 SOP。适用于接入中转站、内网网关、Claude Code 风格的兼容端点，或排查「代理接好了但模型说没有工具 / 不调用工具 / 不读本地文件」这类问题。
metadata:
  agent_created: true
---

# Anthropic 协议接入 WorkBuddy

## 用途

WorkBuddy 的自定义模型只走 OpenAI Chat Completions；上游若只给 Anthropic Messages，
中间必须放一层代理做协议转换。用 LiteLLM 的 `anthropic` provider 承担转换，不自己手写
请求/响应映射——手工转换在工具调用、流式、思考块这三处极易出错。

## 何时使用

- 上游只有 `/v1/messages` 端点，或只有 Claude Code 风格的中转/内网网关
- 要给 WorkBuddy 加自定义模型，且要求工具调用、图片、推理可用
- 代理已接上，但模型声称「没有工具」「没有权限」，或该读文件时反复检索工具

按场景分流，别从头读全篇：

| 场景 | 走法 |
|---|---|
| 从零接入新上游 | 本篇五步落地 → 验收 |
| 已接入但行为异常（无工具/不调用/卡执行中） | 直接跳「坑点速查」+ `references/diagnostics.md` 三层取证 |
| 只想实测某模型能力再填配置 | 直接跑 `scripts/probe_model_caps.py` |

## 前置脚手架

本 SOP 的第 3、4 步依赖一套可运行的代理脚手架（协议转换器、钩子回调、配置生成器、
启动器），全部打包在本 skill 的 `assets/scaffold/` 内，可直接复制到任意目录运行：

| 文件 | 作用 |
|---|---|
| `bridge_guard.py` | 钩子回调：工具改名、纠偏注入、`type: custom` 清理、响应侧映射/丢弃 |
| `compat_patch.py` | LiteLLM `_map_tools` 的 monkey patch，严格网关 422 修复 |
| `bridge_env.py` | `.env` 读取、base 归一化、模型别名配对 |
| `gen_config.py` | 从 `.env` 生成 `config.yaml`（第 3 步的「脚本」就是它） |
| `start_proxy.py` / `start.bat` | 前台启动器（注入环境变量后拉起 LiteLLM） |
| `daemon.py` + `daemon_ctl.py` + `start_background.bat` / `stop.bat` / `status.bat` | 后台常驻：pythonw 无窗口 + pidfile + watchdog 自愈 + 日志落盘 |
| `install_boot.bat` / `uninstall_boot.bat` | 登录自启注册/卸载（计划任务 ONLOGON） |
| `boot_common.py` | 前台/后台共用的环境装配与命令构建 |
| `test_bridge.py` / `test.bat` | 端到端自检（配合 `mock_anthropic.py` 可离线验证） |
| `mock_anthropic.py` | 假上游，不接真实网关也能全链路验证 |
| `detect_models.py` / `detect.bat` | 列出上游可用模型 |
| `env.example` | `.env` 模板（唯一模板，bat 启动器自动复制） |
| `README.md` | 脚手架自带说明，含环境初始化与协议映射对照 |

用法：把 `assets/scaffold/` 整目录复制到工作区，`python -m venv .venv` 后
`pip install "litellm[proxy]==1.106.4"`（版本必须固定，原因见 `references/pitfalls.md` 第 10 条），
后续按脚手架 README 走。若 scaffold 已在别的目录跑着，直接复用，别复制第二份。

## 总体链路

```
WorkBuddy ──OpenAI /v1/chat/completions──▶ LiteLLM 本地代理(127.0.0.1:4000)
                                              │  协议转换
                                              ▼
                                   上游网关 ──Anthropic /v1/messages──▶ 模型
```

转换点在 config.yaml 的 `model: anthropic/<上游真实模型名>`。这一行决定用哪个
provider 的转换器，写错等于没转换。

## 五步落地

### 1. 探测上游

先跑 `scripts/probe_upstream.py`，确认四件事：base 地址、端点路径、模型名、是否支持工具。
这步不能跳过——端点究竟是 `/v1/messages` 还是 `/messages`、模型名大小写，各家中转都不一样。

```bash
python scripts/probe_upstream.py --base https://host:port --key sk-xxx --model glm-5.3
```

TLS 默认校验证书链与主机名。私有 CA 签发的网关用 `--ca <证书.pem>`（或 `.env` 的
`UPSTREAM_CA_BUNDLE`）；`--insecure` 会关闭校验，只用于临时排查，不要在长期配置里留。
三个探测脚本（`probe_upstream.py`、`probe_model_caps.py`、`detect_models.py`）的开关一致。

### 2. 写 .env

| 键 | 作用 | 易错点 |
|---|---|---|
| `UPSTREAM_BASE_URL` | 上游根地址 | 只填到域名/端口，不要带 `/v1/messages` |
| `UPSTREAM_BASE_URL_OPENAI` | OpenAI 原生模式的显式路由地址 | 仅 `UPSTREAM_IS_OPENAI_NATIVE=true` 时生效；网关把 OpenAI 路由挂在非标准路径（如 `/api/openai/v1`）时手填，留空则自动在根地址后补 `/v1` |
| `UPSTREAM_API_KEY` | 上游令牌 | 也认 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` 环境变量 |
| `UPSTREAM_MODELS` | 上游真实模型名，逗号分隔 | 填上游认的名字，不是别名 |
| `PROXY_MODEL_ALIASES` | 对外别名，数量须与上一行一致 | 不填则别名等于真实名 |
| `PROXY_PORT` | 本地端口，默认 4000 | 端口占用时启动脚本只提示不换端口 |
| `PROXY_MASTER_KEY` | WorkBuddy 连代理用的 key | 必须与 models.json 的 apiKey 完全一致；留空则首次启动自动生成并写回 .env，忘了跑 `status.bat` 查 |
| `DEFAULT_MAX_TOKENS` | 缺省 max_tokens 兜底 | OpenAI 客户端常不传，Anthropic 侧必填 |
| `MIN_OUTPUT_TOKENS` | 输出 token 下限 | 必须是正整数；低于 1600 时思考块会吃掉正文；开思考时必须大于 `THINKING_BUDGET_TOKENS`，否则生成配置时直接报错 |
| `MAX_OUTPUT_TOKEN_CAPS` | 按模型的输出上限表，JSON 对象如 `{"glm-5.3": 131072}` | 超限会被上游整轮 400 而非截断，代理按表夹紧；上限值必须为正整数且不小于 `MIN_OUTPUT_TOKENS`，否则生成配置时直接报错 |
| `UPSTREAM_CA_BUNDLE` | 私有 CA 证书文件（PEM） | 仅供自建中转使用；探测脚本与代理都会读它。不填则用系统信任库 |
| `BRIDGE_DUMP_ENABLED` | 完整请求转储开关，默认关 | 打开后落盘完整对话（含 system 全文），只在复现具体问题时临时开启，见下文「诊断转储的保留与删除」。写 `.env` 即可生效：启动脚本会把解析后的值导出给 LiteLLM 子进程；环境变量优先于 `.env` |
| `BRIDGE_DUMP_MAX_CHARS` | 转储文件字符上限，默认 200000 | 超出部分截断，防止一次长对话把磁盘写满。必须是正整数，非法值（0 / 负数 / 非数字）会打印提示并按默认值处理，不拦停启动 |
| `EXTRA_HEADERS` | 额外请求头，JSON 对象 | 中转校验自定义头时用 |
| `UPSTREAM_IS_OPENAI_NATIVE` | 上游其实是 OpenAI 协议时置 true | 这种场景不需要协议转换；api_base 会自动补 `/v1` 尾巴（LiteLLM 直接拼 `/chat/completions`，已实测） |
| `REQUEST_TIMEOUT` | 上游请求超时秒数，默认 600 | 转发超时自动放大 1.1 倍并至少加 30 秒 |
| `GUARD_ENABLED` | 是否挂载纠偏回调，默认 true | 关掉它 `compat_patch.py` 会跟着失效（422 修复不再生效），见 `references/pitfalls.md` 第 10 条 |
| `THINKING_ENABLED` / `THINKING_BUDGET_TOKENS` | Anthropic 思考块开关与预算，默认关 / 2000 | 上游报错提到 thinking / budget_tokens 时置 false |
| `ALLOW_ANY_MODEL` | 未列出的模型名也放行转发，默认 true | 通配兜底 `"*"`，关闭后未配置模型直接 400 |

### 3. 生成 config.yaml

用脚手架的 `gen_config.py` 生成（`python gen_config.py`），不手改 YAML。关键字段：

```yaml
model_list:
  - model_name: glm-5.3             # 对外名，WorkBuddy 里选的就是它
    litellm_params:
      model: anthropic/glm-5.3      # provider 前缀 + 上游真实名
      api_base: "https://host:port" # 不带请求路径
      api_key: "os.environ/UPSTREAM_API_KEY"
      max_retries: 2                # 一次客户端请求会变成最多 3 次上游调用
litellm_settings:
  drop_params: true                 # 丢弃上游不支持的参数；不会丢 tools
  callbacks:
    - bridge_guard.proxy_handler_instance   # 脚手架的回调模块，随代理启动自动挂载
general_settings:
  master_key: "os.environ/PROXY_MASTER_KEY"
```

### 4. 启动代理

脚手架提供两种模式：

| 模式 | 命令 | 适合 |
|---|---|---|
| 前台窗口 | `start.bat`（或 `python start_proxy.py`） | 调试期看实时日志 |
| 后台常驻（推荐） | `start_background.bat`（pythonw 拉起 `daemon.py`，无窗口） | 日常使用，崩溃自愈、可开机自启 |

后台模式三件套：`status.bat` 查状态、`stop.bat` 整树停止（依赖 `logs/daemon.pid`）、
`install_boot.bat` 注册登录自启（计划任务，延迟 30 秒）。日志按日期轮转：
`logs/proxy-YYYY-MM-DD.log`（litellm 输出）、`logs/daemon-YYYY-MM-DD.log`（守护日志）、
`logs/requests-YYYY-MM-DD.log`（回调请求日志）；设 `BRIDGE_LOG_ROTATE=false` 回退单文件。
watchdog：litellm 挂掉 5 秒后自动重启，
指数退避封顶 60 秒，连续崩溃 10 次放弃。

daemon 被强杀或崩溃后，litellm 子进程常常仍然存活并占着端口，此时 pidfile 指向的
pid 已死。`stop.bat` / `status.bat` 都会探测端口：发现「pid 已死但端口仍被监听」时
打印占用者、进程链与清理命令，`stop.bat` 询问确认后直接清掉整棵树。这样不会陷入
「stop 说无需停止、start 说端口冲突」的死角。

无论哪种模式，启动器必须在拉起 LiteLLM 前注入下列环境变量，否则会出现
「本地自检正常、WorkBuddy 一用就怪」（daemon.py / start_proxy.py 均已内置）：

| 变量 | 原因 |
|---|---|
| `NO_PROXY=127.0.0.1,localhost,::1` | 本机常有全局 HTTP_PROXY，会把回环请求也劫持走 |
| `DEFAULT_ANTHROPIC_CHAT_MAX_TOKENS` | 补 Anthropic 必填的 max_tokens |
| `LITELLM_LOCAL_MODEL_COST_MAP=True` | 否则启动时联网拉价格表，国内网络白等约 30 秒 |
| `LITELLM_TELEMETRY=False`、`DO_NOT_TRACK=True` | 关闭遥测 |

就绪判据是 `curl http://127.0.0.1:<port>/health/liveliness` 返回 `"I'm alive!"`。
终端打印 `Uvicorn running` 不代表可以接请求，必须等健康检查通过；后台模式无窗口，
只能靠健康检查判定，litellm 冷启动需 10–30 秒。

### 5. 写入 WorkBuddy models.json

位置 `~/.workbuddy/models.json`（用户级）。项目级 `.workbuddy/models.json` 优先级更高，
且 `availableModels` 整体覆盖不合并——项目里放一份就会让用户级配置失效。

```json
{
  "id": "glm-5.3",
  "name": "glm-5.3",
  "vendor": "Custom",
  "apiKey": "<与 PROXY_MASTER_KEY 完全一致>",
  "maxInputTokens": 262144,
  "maxOutputTokens": 65536,
  "url": "http://127.0.0.1:4000/v1/chat/completions",
  "supportsToolCall": true,
  "supportsImages": false,
  "supportsReasoning": true,
  "useCustomProtocol": true
}
```

`url` 写完整路径，`supportsToolCall` 必须为 true，否则宿主不会下发工具定义。

**能力字段必须实测，不能照抄模型介绍页。** 多模型接入时先跑
`scripts/probe_model_caps.py`，再按结果填：

| 探测项 | 对应字段 | 脚本覆盖 |
|---|---|---|
| 工具调用是否成功 | `supportsToolCall` | 覆盖；直连能调、经代理不能调，多半是 `type: custom` 被上游拒（见坑点速查） |
| 三张纯色图是否全对 | `supportsImages` | 覆盖；0/3 且回「看不到图」= 不支持；乱猜颜色也要判为不支持 |
| 超大 `max_tokens` 的报错 | `maxOutputTokens` | 覆盖；报错常直接写明上限，如 `does not support max tokens > 196608` |
| 400k 字符输入是否通过 | `maxInputTokens` | 覆盖；拿的是下限，配置值取不高于实测下限的保守数 |
| 响应里有无 `reasoning_content` | `supportsReasoning` | 覆盖；有才开，否则界面显示空白思考区 |

工具调用探测默认重复 3 次输出命中率（`--repeats` 可调）；视觉本身跑三张图。
下正式结论仍需按 `references/diagnostics.md` 的隔离实验规范重复 5 次。

## 安全边界

脚手架会在几个位置主动"拒绝执行"，这些是有意设计，不是 bug。

**TLS 校验默认开启。** 三个探测脚本默认走系统信任库，证书过期、域名不符、自签名
都会直接失败，并打印三条处置建议。私有 CA 用 `--ca` / `UPSTREAM_CA_BUNDLE`；
`--insecure` 是临时排查手段，会同时关掉主机名校验。

**终止进程前先核对身份。** `logs/daemon.pid` 是 JSON，记录 pid、启动时间、解释器
路径、脚本绝对路径、脚手架根目录。`stop.bat` 在 `taskkill` 之前逐项比对：
Python 实际执行的脚本参数必须解析到本脚手架的 `daemon.py`，解释器路径要对上，启动时间差不得超过
5 秒。任何一项对不上就打印人工核实命令并拒绝终止——pid 被系统复用是真实存在的，
照着残留 pidfile 杀进程会误伤无关程序。旧版单行 pidfile 仍可读，退化为只核对
命令行与解释器路径。

孤儿清理同样要求归属：路径先解析 `..` 与目录链接，再检查目录边界。最终清理目标
在发信号前必须与原始 OS 快照的创建时间、命令行、可执行路径一致；证据缺失或变化
就拒绝自动清理。细节见 `references/pitfalls.md` 第 19 条。

**未声明的工具调用一律拒绝。** 客户端本轮 `tools` 字段为空时，上游返回的任何结构化
工具调用都会被丢弃，并把 `finish_reason` 归位为 `stop`——放行等于让宿主去等一个
永远不存在的工具，界面会卡在「执行中」。代理自动补的别名带 `[bridge-alias]` 描述
标记，与客户端真实声明分开记账；别名只映射到客户端显式声明的目标工具。

**诊断转储的保留与删除。** 常规日志（`logs/requests-YYYY-MM-DD.log`）只记运维元数据：
消息条数、工具名、`max_tokens`、耗时、`finish_reason`、错误摘要。回复正文只记长度，
不记内容。完整请求体（含 system 全文与工具 schema）默认**不落盘**，需要时设
`BRIDGE_DUMP_ENABLED=true`，此时：

- 写入 `logs/last_request.json`，单文件覆盖，只保留最近一次
- 敏感键（`authorization` / `api_key` / `token` / `cookie` 等）打码为 `<redacted>`
- 图片 base64 折叠成 `<redacted image base64, N chars>`
- 超过 `BRIDGE_DUMP_MAX_CHARS` 截断
- 文件权限收紧到 0600

排查结束后请立刻：把 `BRIDGE_DUMP_ENABLED` 改回 false，删除 `logs/last_request.json`。
该文件含完整对话与 system 提示词，不要随仓库提交，也不要长期留在机器上。

**token 限制的执行顺序固定。** 先把 `max_tokens` 兜到 `MIN_OUTPUT_TOKENS`，再按模型
能力表夹到上限。顺序反了会出现「兜底值本身超上限」的整轮 400。下限高于某模型上限时
以上限为准；配置阶段若发现上限小于下限，`gen_config.py` 直接报错而不是带着矛盾运行。

**验证范围。** 修复版在 LiteLLM 1.106.4 上通过 115 项回归与 18 项本地集成检查。
2026-10-28 针对一处真实兼容网关的 5 个模型，常规自然语言工具调用为 25/25，结果
回传均通过；deepseek-v4-flash 的 thinking 模式仅在 auto 工具选择下通过流式测试，
强制指定工具名会被上游拒绝。glm-5.3-flash 的图片识别在直连与代理下均有误判。
这些是特定网关的观测，其他上游的工具语义、metadata 与私有 CA 链仍需分别实测。

## 验收

五条都过才算接好，缺一条都会在真实使用中暴露：

1. 健康检查返回 `"I'm alive!"`
2. 非流式 + 带 tools 的请求：`finish_reason == "tool_calls"`，工具名正确
3. 流式 + 带 tools 的请求：能从流里解析出工具名
4. 用真实自然语言提问（如「读一下某个本地文件」），模型第一轮就发起读文件调用，
   而不是先检索工具或声明没有工具
5. 故意让它调用宿主没有的工具（如「用 webReader 抓某网页」）：要么被映射成宿主
   存在的等价工具，要么被丢弃并回 `stop`，绝不能留下一个宿主执行不了的 tool_call

改完脚手架或升级 LiteLLM 后，先跑回归再上线：

```bash
# 全部定向回归：TLS、进程身份、路径边界、工具声明、流式移除、转储、token 冲突
.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py" -v

# 集成回归：起 mock 上游 + 真实 LiteLLM 代理，跑 test_bridge.py 的 5 项
.venv/Scripts/python.exe test_bridge.py
```

`tests/test_security_fixes.py` 需要 litellm 才能导入 `bridge_guard`，没有时自动跳过
相关用例并提示。它自带一张自签证书的 HTTPS 服务，用来验证「默认拒绝自签名、
带 `--ca` 放行、`--insecure` 放行」三种情形。

## 坑点速查

按踩坑概率排序，机理与证据见 `references/pitfalls.md`。

| 坑 | 现象 | 处置 |
|---|---|---|
| 误判宿主没下发工具 | 模型说「没有 Read/Grep/Bash」 | 先装请求日志再判断，日志会证明工具在不在 |
| ToolSearch 诱导 | 连续十几轮检索工具，从不调用原生工具 | 双向改名，见 `references/pitfalls.md` 第 2 条 |
| 只改工具描述 | 时好时坏，命中率约 4/5 | 不够稳定，必须改名 |
| 钩子选错 | 改了请求体，上游却没收到 | 改上游请求体只能用 `async_pre_call_deployment_hook` |
| apiKey 与 master_key 不一致 | 报 `No connected db` | 不是数据库问题，是鉴权失败的误导性报错 |
| `max_retries: 2` | 日志里一次测试出现 3 条请求 | 是重试，不是三次独立测试，间隔约 10–20 秒 |
| 单次实验下结论 | 同一配置两次结果不同 | 每项至少重复 5 次再判定 |
| 思考块吃 token | 正文被截断或为空 | `MIN_OUTPUT_TOKENS` 提到 1600 以上 |
| 手填 base 带路径 | 404 | 归一化时剥掉 `/v1/messages`、`/v1` 等尾巴 |
| 上游偶发 502 | 单次请求失败 | 网关瞬时故障，重试即可，别误判为配置错 |
| 升级 LiteLLM 后 422 复现 | 原来能用的模型一给 tools 就 422 | 先查 `compat_patch.py` 补丁是否随版本升级失效，见 `references/pitfalls.md` 第 10 条 |
| 函数工具带 `type: custom` | 某模型一给 tools 就 422，同网关别的模型正常 | 严格网关不认该字段，删掉后所有模型都能用，见 `references/pitfalls.md` 第 10 条 |
| 能力字段照抄介绍页 | 显示能读图却答非所问、输出被截断 | 一律先跑 `scripts/probe_model_caps.py` 实测再填 |
| 网关用自签/私有 CA | 探测报 `CERTIFICATE_VERIFY_FAILED` | 默认已开启校验，用 `--ca` 或 `UPSTREAM_CA_BUNDLE` 指定证书；`--insecure` 只作临时排查 |
| pidfile 残留指向被复用的 pid | `stop.bat` 打印「拒绝」并让你人工核实 | 有意设计。按提示 `tasklist` 核对后手动 taskkill，或删掉 `logs/daemon.pid` 再启动 |
| 客户端没传 tools 却收到 tool_call | 界面卡在「执行中」 | 代理已会丢弃并归位 `stop`；若仍出现，先确认 `GUARD_ENABLED` 没被关掉 |
| 打开 `BRIDGE_DUMP_ENABLED` 后忘了关 | `logs/last_request.json` 里躺着完整对话 | 排查完立刻改回 false 并删文件，该文件含 system 全文与用户输入 |
| 模型调用自家内置工具 | 界面永远卡在「执行中 webReader」 | 宿主没这个工具，等不到结果。请求侧补别名 + 响应侧映射/丢弃，见 `references/pitfalls.md` 第 12 条 |
| `max_tokens` 超过上游上限 | 整轮 400，报「限制数值范围[1,131072]」 | 不是截断而是直接失败，代理已按模型夹紧（`MAX_OUTPUT_CAPS`，见第 13 条） |
| 旧代理没退干净 | 改动静默不生效，端口 4000 仍被旧进程占用 | 启动脚本端口被占会退出；先清进程再起（`netstat -ano` + `taskkill`），见第 14 条 |
| 安装目录含中文 | 后台启动一闪而过，日志报 `UnicodeEncodeError: 'ascii' codec` | pidfile 写读两侧已统一 UTF-8；旧纯数字 pidfile 仍兼容，见第 16 条 |
| `stop.bat` 报「无法确认身份：缺少 exe/created」 | 进程在跑但拒绝终止 | 有意设计：证据不全不动手，按提示核对后手动 taskkill，见第 17 条 |
| 开了转储后带图请求变差 | 上游 400 或模型说没看到图 | 脱敏已改为非破坏性（不改原请求）；若仍出现，确认跑的是修复后的 `bridge_guard.py`，见第 18 条 |
| 清理孤儿时波及无关进程 | 同机的 uvicorn 开发服务被一起杀 | 只从归属明确的进程里选清理根，发信号前复核，见第 19 条 |

## 参考

- `references/pitfalls.md` — 每个坑的现象、证据、根因、处置，含工具改名的完整实验数据
- `references/callbacks.md` — LiteLLM 钩子选型表，请求侧改名与响应侧还原的实现要点
- `references/diagnostics.md` — 三段式排障法与隔离实验方法
- `references/gateway-case-study.md` — 真实网关接入案例：双路由判定、端到端五项实测、多模态劣化定位
- `assets/scaffold/` — 可直接运行的代理脚手架（config 生成、前台/后台双模式启动、回调、自检、假上游），见其 README
- `scripts/probe_upstream.py` — 上游探测脚本，接任何新上游前先跑（参数化，无需 .env，纯标准库）
- `scripts/probe_model_caps.py` — 逐个模型实测工具/视觉/输出上限/输入下限/推理，填 models.json 前先跑
- `assets/scaffold/detect_models.py` — 与 `probe_upstream.py` 分工：后者在接上游之前用（命令行传参、无 .env 也能跑）；前者在脚手架就位后用（读 .env、双击 `detect.bat` 即可，额外探测网关是否同时提供 OpenAI 原生路由——可用则无需本地代理）
