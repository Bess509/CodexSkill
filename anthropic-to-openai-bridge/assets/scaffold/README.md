# Anthropic 端点转 OpenAI 兼容代理

把只支持 Anthropic 协议的第三方接口，转成标准的 OpenAI 格式接口供本地调用。

对外：`http://127.0.0.1:4000/v1`（完整 OpenAI 兼容）
对内：`你的地址 + /v1/messages`（Anthropic Messages API）

---

## 一、四步上手

### 0. 初始化环境（每个新环境只做一次）

```bash
cd <脚手架目录>
python -m venv .venv
.venv/Scripts/pip install litellm[proxy]==1.106.4
```

版本必须固定：`compat_patch.py` 的 monkey patch 依赖 LiteLLM 内部的
`AnthropicConfig._map_tools` 函数（`litellm/llms/anthropic/chat/transformation.py`），
升级后该函数签名或行为可能变化，补丁会静默失效——症状回归为带 tools 的 422。
升级版本前先读 skill 的 `references/pitfalls.md` 第 10 条，升级后必须重跑
验收第 2 条（带 tools 的非流式请求）。

### 1. 填写配置

复制 `env.example` 为 `.env`，至少改这三项：

```
UPSTREAM_BASE_URL=https://你的域名或IP（含端口）
UPSTREAM_API_KEY=你的令牌
UPSTREAM_MODELS=上游真实模型名
```

不确定模型名就跑 `detect.bat`，脚本会自动列出上游可用的模型。

令牌如果不想写进 `.env`，也可以改用环境变量，脚本按此顺序自动查找，
找到一个就不再往下找：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `.env` 的 `UPSTREAM_API_KEY` | 推荐，最简单直观 |
| 2 | 环境变量 `ANTHROPIC_AUTH_TOKEN` | 沿用 Anthropic SDK 习惯写法 |
| 3 | 环境变量 `ANTHROPIC_API_KEY` | 同上 |

启动时终端会打印令牌的来源，便于确认到底读到了哪一个。

> 注意：`ANTHROPIC_AUTH_TOKEN` 这类变量在某些工具里会被写成占位值。
> 例如 Claude Code 的供应商切换工具（CC Switch）会把它设为 `PROXY_MANAGED`，
> 真实令牌存在该工具自己的数据库里——`PROXY_MANAGED` 本身并不是可用令牌。
> 这种情况下建议直接填 `.env`，把占位值留给工具自身的链路。

### 2. 启动代理

两种模式选一：

| 模式 | 启动方式 | 适合
|---|---|---|
| 前台窗口 | 双击 `start.bat` | 临时用、调试期看实时日志
| **后台常驻（推荐）** | 双击 `start_background.bat` | 日常使用，无窗口、崩溃自愈、可开机自启 |

前台模式看到下面的提示就是好了：

```
OpenAI 兼容地址 : http://127.0.0.1:4000/v1
API Key         : <你的 PROXY_MASTER_KEY，见下方说明>
```

这个窗口不要关，关掉代理就停了。

API Key 的来源：`.env` 里填了 `PROXY_MASTER_KEY` 就用它的值；
留空则首次启动自动生成随机密钥并写回 `.env`（终端会打印前 12 位提示）。
忘记 key 时双击 `status.bat` 查看。

#### 后台模式说明

`start_background.bat` 用 `pythonw` 拉起 `daemon.py`，全程无窗口：

- 代理的 LiteLLM 输出写进 `logs/proxy.log`，daemon 自身日志写 `logs/daemon.log`
- 进程号记在 `logs/daemon.pid`，`stop.bat` 靠它定位进程树
- **watchdog 自愈**：litellm 进程挂掉后 5 秒自动重启，指数退避封顶 60 秒；连续崩溃 10 次放弃并写日志
- 后台模式没有窗口，就绪判据只能靠健康检查：`curl http://127.0.0.1:4000/health/liveliness` 返回 `"I'm alive!"`（约 10–30 秒，litellm 冷启动需要时间）

日常管理三件套：

| 操作 | 命令 |
|---|---|
| 查状态 | 双击 `status.bat`（报告 daemon/litellm 进程与端口健康，**并打印 Base URL / API Key / 模型名——后台模式查连接信息就靠它**） |
| 停止 | 双击 `stop.bat`（按 pidfile 整树带走 daemon + litellm） |
| 再启动 | 双击 `start_background.bat`（已有实例在跑时只提示不重复拉起） |

#### 开机自启（可选）

双击 `install_boot.bat` 注册计划任务（`AnthropicOpenAIBridgeBoot`，登录后延迟 30 秒无窗口拉起 daemon）；`uninstall_boot.bat` 卸载。也可在「任务计划程序」里可视化管理。

### 3. 验证能力

新开一个窗口，双击 `test.bat`。会依次验证五项：

| 序号 | 验证项 | 说明 |
|---|---|---|
| 1 | 模型列表 | `GET /v1/models` 能否返回模型 |
| 2 | 非流式对话 | system 提升、max_tokens 缺省兜底是否正常 |
| 3 | 流式 SSE | 分片数、首字节延迟、usage 是否完整 |
| 4 | 工具调用 | 含 arguments 拼接与工具结果回传两轮对话 |
| 5 | 多模态输入 | base64 图片能否被正确识别 |

---

## 二、目录结构

| 文件 | 作用 |
|---|---|
| `.env` | 唯一需要你维护的配置文件 |
| `start.bat` | 前台启动代理（有窗口，调试用） |
| `start_background.bat` | 后台常驻启动（无窗口，日常推荐） |
| `status.bat` / `stop.bat` | 后台实例的状态查询 / 整树停止 |
| `install_boot.bat` / `uninstall_boot.bat` | 开机自启的注册 / 卸载（计划任务） |
| `test.bat` | 一键验证五项能力 |
| `detect.bat` | 探测上游真实可用的模型名与路径 |
| `bridge_env.py` | 配置读取与地址规范化，所有脚本共用 |
| `gen_config.py` | 由 `.env` 生成 LiteLLM 的 `config.yaml` |
| `start_proxy.py` | 前台启动入口，注入环境变量并拉起 LiteLLM |
| `boot_common.py` | 前台/后台共用的环境装配与命令构建 |
| `daemon.py` | 后台守护进程：无窗口拉起 litellm + watchdog 自愈 + 日志落盘 |
| `daemon_ctl.py` | status/stop 子命令（bat 只是它的包装） |
| `test_bridge.py` | 五项能力验证脚本 |
| `detect_models.py` | 上游模型探测脚本 |
| `bridge_guard.py` | 转发前纠偏：抑制模型虚构工具、抬升输出 token 下限 |
| `compat_patch.py` | 修复严格网关对 tools 里 `type: custom` 的 422 |
| `mock_anthropic.py` | 假上游，离线验证全链路 |
| `config.yaml` | 自动生成，不要手工修改 |
| `logs/` | 运行后生成：`proxy.log`（litellm 输出）、`daemon.log`（守护日志）、`daemon.pid`（pidfile） |

---

## 三、客户端怎么填

| 配置项 | 值 |
|---|---|
| API Base / Base URL | `http://127.0.0.1:4000/v1` |
| API Key | `.env` 里的 `PROXY_MASTER_KEY`（留空则首次启动自动生成；忘了就跑 `status.bat` 查） |
| Model | `.env` 里 `UPSTREAM_MODELS` 的名字，或你配置的别名 |

注意 API Key 填的是本地代理的密钥，不是你的上游令牌。上游令牌只存在 `.env` 里，不会出现在任何客户端配置中。

### Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:4000/v1", api_key="<PROXY_MASTER_KEY 的值>")

resp = client.chat.completions.create(
    model="claude-sonnet-4-5-20250929",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
for chunk in resp:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### curl

```bash
curl http://127.0.0.1:4000/v1/chat/completions ^
  -H "Authorization: Bearer <PROXY_MASTER_KEY 的值>" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"claude-sonnet-4-5-20250929\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

---

## 四、协议映射对照

| 维度 | OpenAI 侧（客户端看到） | Anthropic 侧（转发出去） |
|---|---|---|
| 鉴权 | `Authorization: Bearer xxx` | `x-api-key: xxx` |
| 路径 | `/v1/chat/completions` | `/v1/messages` |
| 系统提示 | messages 里 `role=system` | 顶层 `system` 字段 |
| 长度上限 | `max_tokens` 可缺省 | 必填，缺省时按 `.env` 兜底 |
| 停止词 | `stop`（字符串或数组，上限 4 个） | `stop_sequences` |
| 工具调用 | `tools` / `tool_calls` | `tools` / `tool_use` |
| 工具结果 | `role=tool` 消息 | `role=user` + `tool_result` 块 |
| 图片 | `image_url.url`（含 data URI） | `source.type=base64` + `media_type` |
| 流式 | `choices[0].delta` | `content_block_delta` 事件 |
| 结束原因 | `stop` / `length` / `tool_calls` | `end_turn` / `max_tokens` / `tool_use` |
| 用量 | `prompt_tokens` / `completion_tokens` | `input_tokens` / `output_tokens` |

---

## 五、常见问题

| 现象 | 原因与处理 |
|---|---|
| 报错含 `system cpu overloaded` 或 `too many requests` | 上游网关自身过载或限流，与本地配置无关。等几分钟重试；这类错误已被 `detect_models.py` 单独识别，不会误报成鉴权失败 |
| 报错提示端口被占用 | 上一个代理还在跑，照常用即可；要重启：前台模式关窗口，后台模式跑 `stop.bat`；或改 `.env` 的 `PROXY_PORT` |
| 后台模式健康检查一直不过 | 看 `logs/proxy.log`（litellm 为何没就绪）与 `logs/daemon.log`（watchdog 是否在重启循环）；litellm 冷启动需 10–30 秒 |
| 后台模式重复启动 | 第二次 `start_background.bat` 只提示已在运行，不会强占端口 |
| `404` 关于模型名 | 运行 `detect.bat` 拿真实模型名，填回 `UPSTREAM_MODELS` |
| 报错提到 `thinking` 或 `budget_tokens` | 把 `.env` 的 `THINKING_ENABLED` 改成 `false` |
| 报错提到某个字段被拒绝 | 确认 `gen_config.py` 生成的配置里 `drop_params: true`；仍不行就说明上游对该字段特别严格，看代理窗口的原报错再针对性处理 |
| 地址填写不确定带不带 `/v1` | 都可以，`bridge_env.py` 会自动剥离并重新拼接，两种写法都会试探 |
| 证书是自签 https | `detect_models.py` 已放宽校验；LiteLLM 侧若报证书错误，联系服务商换正规证书 |
| 想让局域网其他机器访问 | 把 `start_proxy.py` 里的 `127.0.0.1` 改成 `0.0.0.0`，并把 `PROXY_MASTER_KEY` 换成足够随机的值 |

---

## 五点五、真实网关接入案例

见 skill 的 `references/gateway-case-study.md`：一份完整的 new-api 系网关接入实录
（连通性探测、双路由判定、glm-5.3 五项端到端实测、多模态劣化的定位过程），
可作为接入自己网关时的参照。

---

## 五点六、模型虚构工具的问题与本仓库的处理

现象：GLM 系列在工具列表没有被充分注意时，会用 Markdown 伪造一整轮"调用某个内置
工具 + 返回结果"，例如虚构一个 `webReader`，把本地路径拼成 `file:///C:/...`，
再自己编出 `MCP error -400` 的报错，最后凭空给结论。这类输出看着像工具调用，
实际上走的不是结构化 `tool_calls`，失败也是模型自己编的。

要纠正它，先区分两类原因：

- **工具正常下发时**：实测 10 / 30 / 60 个工具规模下经本代理与直连上游，模型均能
  正确产出结构化 `Read` 调用，且 Windows 中文路径原样保留。这部分链路没有问题。
- **工具没有下发或未被注意时**：模型回退到训练期偏置，开始伪造。这是主要风险场景。

本仓库在转发前加了一道纠偏（`bridge_guard.py`），做两件事：

1. 向 system 注入一段最高优先级的工具使用规范：只准调用真实存在的工具、禁止在
   回答里伪造调用与返回、本地文件必须走 `Read` 且传绝对路径、禁止把本地路径拼成
   `file://` 去调联网工具、没有工具时直说不要编造。注入带去重标记，多轮对话不会累积。
2. 把输出 `max_tokens` 抬到下限时也一并处理（默认 1600）。该模型先吐思考块，客户端
   给的额度偏小时正文会被吞掉，实测出现过只回 11 个字就截断的情况。

修复后同一场景的对比：

| 场景 | 纠偏前 | 纠偏后 |
|---|---|---|
| 未提供工具 | 伪造 `webReader` 与报错，编造文件内容 | 明确说明无工具可用并请求开放权限，不编造 |
| 提供 `Read` 工具 | 正确调用 `Read` | 仍正确调用 `Read`，绝对路径保持原样 |
| `max_tokens=100` | 正文被思考耗尽，只回 11 字 | 抬到下限，正常输出 |

开关在 `.env`：

```
GUARD_ENABLED=true       # 想看未加固的原始行为改成 false
MIN_OUTPUT_TOKENS=1600   # 输出 token 下限
```

实现上有个坑值得记一笔：LiteLLM 的 `async_pre_call_hook` 运行在"选定 deployment
之前"，改的是客户端原始请求体，改动不会带到上游。要改发往上游的请求体必须实现
`async_pre_call_deployment_hook`，两者容易混用而导致改动看起来"没生效"。

---

## 六、离线自检：不用真实地址也能验证

仓库自带一个假的上游服务，可以在还没拿到地址、或想确认代理本身是否正常时用：

```bat
.venv\Scripts\python.exe mock_anthropic.py
```

另开一个窗口，用一个临时端口把代理指向它：

```bash
set UPSTREAM_BASE_URL=http://127.0.0.1:8899
set UPSTREAM_API_KEY=mock-upstream-key
set UPSTREAM_MODELS=claude-sonnet-4-5-20250929
set PROXY_PORT=4100
.venv\Scripts\python.exe start_proxy.py
```

再开第三个窗口运行 `python test_bridge.py`（同样带上 `set PROXY_PORT=4100`）。
上游收到的请求会记录在 `logs/last_upstream_request.json`，可以逐字段核对转换是否正确。

---

## 七、两个已内置处理的坑

### 1. 本机 HTTP 代理会劫持本地回环请求

Windows 上装了各类开发工具后，常存在 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量。
这会导致请求 `127.0.0.1:4000` 时被发往代理，表现为 `/v1/chat/completions` 返回 `404 {"detail":"Not Found"}`
或长时间无响应，而 `/v1/models` 却是正常的。

本仓库的处理：

- `start_proxy.py` 自动设置 `NO_PROXY=127.0.0.1,localhost,::1`，远端上游仍可正常走代理；
- `test_bridge.py` 用 `httpx.Client(trust_env=False)` 直连本地；
- `detect_models.py` 用空 `ProxyHandler` 绕开代理。

若你自己的代码调用本机代理时仍遇到 404，给那个进程补一句：

```python
httpx.Client(trust_env=False)          # httpx
proxies={"http": None, "https": None}  # requests
```

命令行则是 `curl --noproxy "*" ...`。

### 2. 模型名不在列表里会报 400

默认开启了通配路由（`ALLOW_ANY_MODEL=true`），任何模型名都会原样转发给上游。
副作用是 `/v1/models` 会多出 LiteLLM 内置的一批模型名，不影响使用。
想要干净的列表就把它改成 `false`，代价是客户端只能用 `UPSTREAM_MODELS` 里列出的名字。

---

## 八、说明

- 代理只在 `127.0.0.1` 监听，外部机器访问不到，本地自用场景下足够的。
- 遥测已通过 `DO_NOT_TRACK` 与 `LITELLM_TELEMETRY` 关闭，不会向 LiteLLM 官方上报调用数据。
- 启动时不会联网拉取模型价格表（`LITELLM_LOCAL_MODEL_COST_MAP`），国内网络下能省掉约三十秒等待。
- `.env` 里有真实令牌，别把它提交进任何代码仓库。
