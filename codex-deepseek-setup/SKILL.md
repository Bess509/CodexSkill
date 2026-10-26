---
name: codex-deepseek-setup
description: 在 Windows Codex 桌面端或 CLI 中接入 DeepSeek，处理配置与登录态备份、模型目录、API Key、连接测试和回滚；也用于诊断切换后仍使用原模型的问题及规划双配置共存。
---

# Codex 接入 DeepSeek

将 DeepSeek 原生 Responses API 接入 Codex。完整流程包含配置文件、模型目录和登录态切换，不能仅修改模型名或 base_url 就宣告完成。

本技能基于 2026-10-26 的本机操作记录：桌面版 26.1021.31345（21828），内置 CLI 0.164.0-alpha.11.1。用户补充确认：该环境还需要备份原 auth.json、执行 logout，再通过 login --with-api-key 保存 DeepSeek Key。此经验适用于这套环境，不据此断言所有版本和自定义 provider 都必须登出。

## 范围与操作选择

- 用户要求安装、接入或切换时执行配置流程；仅问原理或要求总结技能时，不实际登出或修改运行配置。
- 用户明确要求自行填写 Key 时，写占位符并提供位置；等用户填好后再继续认证切换和真实请求测试。不要把占位符提交给 login 或 API。
- 用户要求共存时，先区分“保留两套配置按需切换”和“同一个桌面模型菜单同时选择两家服务”。不要将前者宣称为后者。
- 不自动结束当前 Codex 进程；保存完成后由用户重启桌面应用，避免中断任务。

## 1. 检查版本、路径与官方资料

解析实际配置目录：优先使用现有 CODEX_HOME，否则使用用户目录下的 .codex。不要把本技能所在目录当作配置目录，也不要覆盖当前进程的 CODEX_HOME。

确定实际 CLI 路径：优先使用桌面应用当前运行的 codex.exe，其次使用 Get-Command codex。不要把旧安装的可执行文件路径硬编码到配置中。

执行 --version、login --help、doctor --help，确认该版本支持的命令。读取配置时只输出白名单字段或脱敏内容，不输出 auth.json、API Key、token 或完整认证环境变量。

接入前核对以下官方资料，避免沿用过期模型名和模型目录结构：

- [DeepSeek Codex 接入](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)
- [DeepSeek Responses API 兼容范围](https://api-docs.deepseek.com/guides/responses_api/)
- [Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)

资料获取失败时明确说明，使用已保存且来源可追溯的版本；不得虚构最新模型或验证结果。不要直接下载并执行远程一键脚本；可以读取脚本了解当前配置规则。

## 2. 备份配置和认证状态

在实际配置目录下创建唯一的 backup-deepseek/<时间戳>/ 目录。备份所有将修改的现有文件，至少包括：

- config.toml
- auth.json（存在时；它包含敏感凭据）
- models.json（存在时），以及实际被覆盖的其他模型目录文件

记录每个文件原先是否存在、原路径和备份路径。通过 SHA-256 比较确认复制一致，再执行修改或 logout。缺失文件应记录为原先不存在，不创建伪备份。

备份保持在本机用户配置目录，不放进 Git 仓库，不上传、不在聊天中展开认证内容。若使用系统凭据库，单独说明 auth.json 备份不一定包含完整登录状态；恢复时可能需要重新登录。

## 3. 添加模型目录与 provider

从 DeepSeek 官方接入页取得完整 models.json 示例，解析 JSON 后再写入。不要复制网页中的行号或 HTML；不要自行编造模型元数据、工具格式、上下文限制或最低客户端版本。

如果已有自定义模型目录，不直接覆盖：备份后为 DeepSeek 使用单独文件，并只在 DeepSeek 配置中引用它。目录元数据不能承担不同 provider 间的请求路由。

以下是记录时的配置形状；模型名及支持字段以执行时文档和客户端为准：

```toml
model = "deepseek-flash"
model_provider = "deepseek"
preferred_auth_method = "apikey"
forced_login_method = "api"
model_reasoning_effort = "high"
web_search = "disabled"
show_raw_agent_reasoning = true
model_catalog_json = "C:/Users/<用户>/.codex/models.json"

[model_providers.deepseek]
name = "deepseek"
base_url = "https://api.deepseek.com/"
wire_api = "responses"
experimental_bearer_token = "REPLACE_WITH_YOUR_DEEPSEEK_API_KEY"

[desktop]
enabled-reasoning-efforts = ["low", "medium", "high", "xhigh", "ultra", "max"]
```

按 TOML 层级修改：顶层字段必须位于表声明之前；已有 [desktop] 时合并字段，不能重复声明该表。保留 plugins、marketplaces、mcp_servers、projects、windows、notify 等无关设置。

处理旧 provider 专属字段，例如原有 service_tier = "priority"；不要把 OpenAI 的服务档位当作 DeepSeek 能力。默认采用官方示例模型，用户指定模型时遵从用户选择。web_search 只在所选模型及当前接口明确支持时开启。

把 model_catalog_json 改为实际路径。写入后使用 TOML/JSON 解析器验证，比较备份确认无关配置未变化。

## 4. 切换登录态（本机接入流程的重要补充）

配置完成且真实 Key 已填好后，确认第 2 步已备份 auth.json，再在相同配置目录和同一个 CLI 下执行：

1. `codex.exe logout`：本机成功输出为 `Successfully logged out`，文件认证模式下原 auth.json 被移除。
2. 将 DeepSeek Key 经 stdin 交给 `codex.exe login --with-api-key`：本机成功输出为 `API key saved`。
3. `codex.exe doctor --json` 或 `codex.exe doctor --summary`：检查实际 provider、模型和认证状态。

每步检查退出码；失败时停止依赖该步的操作并报告具体原因。不要直接删除 auth.json 代替正常登出，也不要在未备份时覆盖原认证。

安全传递 Key：使用 TOML 解析器在本机内存中读取 experimental_bearer_token，或读取已配置的 env_key 对应环境变量，再以子进程 stdin 输入 login。不要把真实 Key 插入工具调用文本、命令行参数、脚本源码、输出或历史记录。PowerShell 中可用内存变量管道传递；若 Key 缺失或仍为占位符，要求用户在本机填好，不能要求其在聊天中粘贴。

这里的 login --with-api-key 用于切换客户端认证状态；provider 的凭据仍须按 DeepSeek 配置正确设置。doctor 中 `OpenAI auth is not required for the active model provider` 是合理结果，不能据此跳过 provider 的 API Key，也不能认为它证明请求已成功。

## 5. 分层验证

区分以下结果，逐项报告实际做到的层级：

1. 静态检查：TOML、JSON 可解析，默认 provider 为 deepseek，模型在目录内，凭据存在且不是占位符。只输出布尔状态，不输出 Key。
2. 客户端体检：doctor 显示 `default model provider deepseek`，认证检查没有阻塞错误。不同版本措辞可以不同；不机械匹配整句。
3. 真实 API 请求：从配置读取 base_url 和 Key，先确认目标仍是 DeepSeek 官方 HTTPS 地址，再向 /responses 发送仅要求回复 `OK` 的短请求，设置合理超时与输出上限。确认 HTTP 成功且响应 status 为 completed，并提取最终文本；HTTP 200 本身不代表生成完成。不要发送仓库文件、聊天历史或其他无关数据。
4. Codex 实际调用：按该版本 `exec --help` 支持的选项，从空的临时工作目录进行一次不读写项目、不使用工具的短提示测试，确认加载 DeepSeek provider 并完成响应。必要时使用临时参数关闭无关 MCP，避免把插件启动问题误判为 API 问题。CLI 通过不等同于桌面 UI 已刷新，桌面端仍需重启后新建聊天确认。

用户只要求配置并自行填 Key 时，完成前两层中能执行的部分即可，明确真实请求待验证。用户要求连接测试则继续真实调用；这会产生少量 API 用量。

遇到 401/403 检查认证与权限，402 检查余额，429 检查限流，400 检查模型及协议字段，网络超时检查代理/DNS/TLS。不要无上限重试，不自动购买额度或修改网络路由。不记录完整请求头、原始认证输出或思维链，只报告脱敏错误、完成状态和必要的用量。

## 6. 共存与回滚

单个活动配置的 model_provider 负责请求路由。仅把 OpenAI 与 DeepSeek 模型合并进 models.json，不能保证切换模型名时自动切换 provider 和认证。

若用户要求保留两家服务，优先设计两套配置目录和各自的认证状态，通过启动子进程时设置独立 CODEX_HOME 来隔离 CLI。不得登出另一套目录。桌面端是否支持独立实例、配置目录或 profile，必须核对当前版本，不能承诺仅用 CLI profile 就实现桌面菜单共存。不要复制正在使用的会话数据库来建立第二套环境。

切回原配置时：先备份当前 DeepSeek 配置和认证，再从指定备份恢复 config.toml、auth.json 和原有模型目录。只清理本次新建且备份清单明确记录原先不存在的文件；移入备份目录比永久删除更易恢复。不要覆盖整个 .codex 目录。恢复后解析配置、检查登录状态并重启桌面端；旧登录凭据可能已过期，需要重新登录 ChatGPT。

交付时提供实际备份位置、Key 填写位置（如仍待填写）、当前模型/provider、验证到哪一层、重启步骤及回滚方式。不要将本机实测、用户反馈和文档推断混为同一种证据。
