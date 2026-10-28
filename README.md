# CodexSkill

面向 Windows 开发环境的个人 Codex skills 集合。每个目录独立提供 `SKILL.md`，按需要附带脚本、参考资料、模板和测试。

仓库维护以下 **4 个个人 skill**；本机 `.system/` 中的系统 skills 不纳入版本控制。

## 技能总览

| Skill | 功能 | 设计亮点 |
|---|---|---|
| [anthropic-to-openai-bridge](anthropic-to-openai-bridge/SKILL.md) | 将 Anthropic Messages 上游接入使用 OpenAI Chat Completions 的 WorkBuddy，提供代理部署和排障流程 | 固定版本协议转换、工具调用双向映射、流式处理、进程身份复核、诊断转储默认关闭 |
| [codex-claude-delivery](codex-claude-delivery/SKILL.md) | 用户明确要求协作时，由 Codex 规划和独立验收，本机 Claude Code 执行与返修 | 可检查的任务单、业务概念资料、执行权限边界、返修停止条件、逐项验收证据 |
| [codex-deepseek-setup](codex-deepseek-setup/SKILL.md) | 在 Windows Codex 桌面端或 CLI 中配置 DeepSeek，处理模型目录、认证、连接测试与回滚 | 配置和认证分别备份、按实际版本核对参数、四层验证、双配置隔离 |
| [windows-vpn-routes](windows-vpn-routes/SKILL.md) | VPN 开启时诊断指定服务的选路，配置、验证和回滚 IPv4 永久直连路由 | 只读取证、最小 `/32` 路由、临时验证后持久化、精确回滚、区分网络与业务授权 |

## 1. Anthropic 协议桥接

**适用场景：** 上游只提供 `/v1/messages`，或接入后出现工具不调用、未知工具卡住、输出 token 超限等问题。

```text
WorkBuddy → OpenAI Chat Completions → 本地 LiteLLM 代理 → Anthropic Messages 网关
```

设计亮点：

- **复用协议转换器。** 使用固定版本 `litellm[proxy]==1.106.4`，兼容补丁与该版本配套验证。
- **覆盖工具调用的完整链路。** 同步处理工具定义、历史调用、强制工具选择和响应名称；流式移除未知调用时，也移除其后续参数分片。
- **按模型处理输出限制。** 校验配置冲突，先补足输出下限，再按模型夹紧上限。
- **进程清理具有身份约束。** 路径解析覆盖 `..` 与目录链接；终止前复核 PID、OS 创建时间、命令行和可执行路径。
- **诊断默认少留数据。** TLS 校验默认开启，完整对话转储默认关闭；开启诊断时，对副本脱敏，保留真实请求中的图片数据。
- **提供可运行脚手架。** 包含配置生成、前台与后台启动、健康检查、模型探测、本地 mock 上游和回归测试。

此 skill 的宿主接入流程面向 **WorkBuddy**。Codex 使用它来部署和排障桥接服务；安装 skill 不会自动更改 Codex 自身的模型 provider。

### 验证与使用边界

本次修复通过了 **115 项回归测试**和 **18 项本地集成检查**，包括中文路径、真实 Windows daemon 启停和转储开启后的图片转发。

2026-10-28 针对一处真实兼容网关的测试中，5 个配置模型的自然语言文件工具调用合计为 **25/25**，工具结果回传均通过。该样本网关还存在以下限制：

- `deepseek-v4-flash` 的 thinking 模式拒绝强制指定工具名；使用 `tool_choice="auto"` 的流式工具调用通过 5/5。
- `glm-5.3-flash` 的纯色图片识别不稳定，直连上游也出现误判。

以上是特定网关和配置的实测结果。接入其他上游时，应重新验证；图片能力、上下文长度和输出上限不由代理统一保证。

## 2. Codex 与 Claude Code 协作交付

**适用场景：** 用户明确希望 Codex 负责规划与验收，Claude Code 负责实现。

设计亮点：

- **任务单作为执行接口。** 明确允许修改的文件、业务规则、输入输出与验收条件，减少执行方对聊天上下文的依赖。
- **业务定义与通用流程分离。** 使用可扩展的业务概念索引，把领域规则放在对应参考资料中。
- **Codex 独立验收。** 检查实际差异、运行匹配的验证，并记录 `PASS`、`FAIL` 或 `NOT_RUN`。
- **返修过程可追溯。** 保留每轮任务单、结果与证据，设置轮次和无进展停止条件。
- **委派维持授权边界。** 沿用用户已授权范围，保留本机 Claude CLI 的认证与模型设置。

该 skill 需要本机 Claude Code CLI。仅在用户明确要求分工协作时使用。

## 3. Codex 接入 DeepSeek

**适用场景：** 配置 DeepSeek provider、排查切换后仍使用原模型、保存两套配置或恢复原状态。

设计亮点：

- **按实际客户端版本操作。** 先确认 CLI 路径、版本和命令帮助，再核对官方模型目录与接口资料。
- **配置与认证分别处理。** 对将修改的文件做备份和哈希核验，并保留原先不存在的文件清单。
- **分层验证结果。** 区分静态配置、客户端体检、真实 API 请求和 Codex 实际调用。
- **凭据通过安全输入传递。** API Key 从本机配置或环境读取，经 stdin 交给登录命令。
- **共存方案有明确边界。** 两套配置目录可以分别管理 provider 与认证；桌面模型菜单行为需按实际版本确认。

文档中的历史版本记录提供排障线索，执行时仍需核对当前官方文档与本机命令支持情况。

## 4. Windows VPN 直连路由

**适用场景：** VPN 或加速器开启时，指定网站、内网服务或数据库的访问路径异常。

设计亮点：

- **先采集证据。** 同时查看 DNS A 记录、网卡、网关、活动路由和持久路由。
- **针对实际选路诊断。** 检查 VPN 的 `/1` 等更具体路由，并用 `Find-NetRoute` 验证出口。
- **控制变更范围。** 默认使用 IPv4 `/32` 主机路由，保留其他应用和默认路由的设置。
- **先临时验证，再持久化。** 分别核验有效选路、持久表和应用层结果。
- **回滚依据实际创建记录。** 精确处理本次新增规则，保护已有路由。

持久路由不会自动跟随 DNS、网关或接口变化。IPv6 和重启后的效果需要分别验证。

## 安装与使用

1. 将仓库克隆或下载到普通工作目录。
2. 选择需要的 skill 文件夹，完整复制到实际 `CODEX_HOME/skills/`；未配置 `CODEX_HOME` 时，个人目录通常为 `~/.codex/skills/`。
3. 保留每个文件夹中的 `SKILL.md` 和它引用的相对目录。目标已存在时，先备份并检查差异，再合并更新。
4. 在新一轮对话中描述任务，或明确指定 skill，例如：

```text
使用 anthropic-to-openai-bridge，检查现有桥接代理并验证工具调用。
使用 codex-claude-delivery，由 Codex 规划验收、Claude Code 执行这个开发任务。
使用 codex-deepseek-setup，先备份，再检查 DeepSeek 接入配置。
使用 windows-vpn-routes，先只读诊断指定域名在 VPN 下的选路。
```

各 skill 的运行依赖和操作边界以其 `SKILL.md` 为准。安装只添加技能文件；代理部署、模型认证切换和路由变更按具体任务授权执行。

## 仓库结构

```text
CodexSkill/
├── anthropic-to-openai-bridge/  # 协议桥接、代理脚手架与测试
├── codex-claude-delivery/       # 协作流程、任务模板与 CLI 执行脚本
├── codex-deepseek-setup/        # provider、认证与回滚流程
├── windows-vpn-routes/         # 路由诊断脚本与变更工作流
├── .gitignore
└── README.md
```

## 验证与敏感信息管理

桥接 skill 的回归测试需在已安装固定版本 LiteLLM 的 Python 环境中运行：

```powershell
python -m unittest discover -s anthropic-to-openai-bridge/tests -p "test_*.py" -v
```

测试输出中的跳过项需要单独检查；缺少依赖时的退出码 0 不代表全部功能已验证。

仓库仅维护 skill 源码、模板、示例和测试。真实 `.env`、认证文件、生成的代理配置、虚拟环境、日志、pidfile、过程输出及备份应保留在本机，相关常见路径已加入 `.gitignore`。测试中的 mock 密钥与保留域名用于隔离验证，不代表实际可用凭据。

每次上传前检查暂存内容中的真实凭据、内部服务地址、数据库连接串和个人路径。`.gitignore` 无法阻止已跟踪文件或源码内嵌秘密的上传，仍需审计实际 Git 对象。

AI 生成的提交附带署名 trailer，例如：

```text
Co-Authored-By: Codex <codex@openai.com>
```
