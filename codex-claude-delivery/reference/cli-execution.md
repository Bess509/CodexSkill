# CLI 调用与权限

派发前读取本文件，并用本机 `claude --help` 核实参数。Windows 脚本需要 PowerShell 5.1+ 和原生 Claude Code `.exe`；不支持将 npm 的 `.cmd` 包装器作为 `-ClaudePath`。不为适配调用而改变认证或全局安全配置。

## Windows 调用

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill-root>\scripts\invoke-claude.ps1" `
  -ProjectPath "<project-absolute-path>" `
  -TaskFile "<run-dir>\task-01.md" `
  -RunDirectory "<run-dir>\task-01\attempts" `
  -Attempt 1
```

`ExecutionPolicy Bypass` 只作用于这次 PowerShell 进程，不修改持久策略；组织策略禁止执行时报告限制，不修改策略。每个独立任务使用各自的执行记录目录；同一任务的轮次递增，不并发复用同一目录和轮次。

可选参数：`-ClaudePath` 指定原生 CLI 路径；`-Model` 仅在用户要求模型时传入，默认沿用配置；`-TimeoutSeconds` 默认 900 秒。

## 工具可见性与权限

- `-AvailableTools` 对应 CLI `--tools`，控制本次可用的内置工具。默认 `Read,Glob,Grep,Edit,Write`，不开放 Bash 或 PowerShell。工具名单只能使用脚本支持的明确名称，不接受 `default` 或通配符。
- `-AllowedTools` 对应 `--allowedTools`，控制附加的自动批准规则，不会启用未列入 AvailableTools 的工具，也不会清空用户、项目或托管配置中的其他允许规则。
- `dontAsk` 与 `--permission-prompts none` 拒绝原本需要交互批准的调用；已获批准或本就无需批准的调用仍受 Claude 自身权限逻辑处理。不能将其描述成“只有本轮 AllowedTools 能执行”。
- 在需要 Bash/PowerShell 时，Codex 先核查适用的权限配置及 hooks/插件影响，确认是否与本轮任务相符。确认后显式传入 `-CommandPermissionsReviewed`；该标志只是操作者的核查声明，不证明权限已隔离，也不替代任务授权。未核查或无法排除超范围权限时，不开放命令工具。
- AvailableTools、任务单和当前工作目录均不是操作系统沙箱。文件工具也不能据任务单自动实现逐文件访问隔离；`--strict-mcp-config` 仅限制 MCP 配置，不等于关闭全部 hooks/插件。任务要求严格隔离时使用已获准的受控环境，或报告现有环境的具体限制。

需要本轮已授权的构建命令时，例如：

```powershell
# 附加到上面的调用；仅在完成继承权限核查后使用。
-AvailableTools 'Read,Glob,Grep,Edit,Write,Bash' `
-AllowedTools 'Read,Glob,Grep,Edit,Write,Bash(npm run build)' `
-CommandPermissionsReviewed
```

`Bash(npm run build)` 的语法和效果以本机版本为准。继承的宽泛 Bash 允许规则不会因这条窄规则而消失；不得据此宣称其他 Bash 命令已被禁止。权限拒绝或范围无法确认时报告阻碍，不能关闭检查、切换认证或改用绕过权限模式。

参数语义参考 [Claude CLI 文档](https://code.claude.com/docs/en/cli-reference) 与 [权限文档](https://code.claude.com/docs/en/permissions)；实际可用参数仍需核实本机版本。

## 超时和证据

脚本使用同一个截止时间控制异步任务输入、进程执行和输出读取。超时后尝试停止 CLI 及可追踪子进程，并有限等待清理；因此总耗时可能包含额外的清理时间，不保证刚到时限就已完全结束。

每轮保存 `.result.json`、`.stderr.log` 和 `.meta.json`。元数据包含超时阶段、工具范围、核查声明、主进程退出状态及输出采集是否完整。`root_process_exited=true` 不证明脱离进程树的后台任务均已停止；输出不完整、清理有疑问或存在后台执行时，先调查再重试。脚本不自动返修，不因退出码 0 判定业务验收通过。

修改脚本后，可运行 `scripts/tests/test-invoke-claude.ps1` 做离线回归。测试使用 Windows .NET Framework 编译器构建临时模拟执行器，覆盖工具可见性、输入与执行超时、子进程清理和结果分类；不调用真实 Claude，不读取认证。测试产物保存在输出注明的临时目录。

## 其他平台

使用等价的 `claude -p --output-format json` 调用，不直接运行 Windows 脚本。保留任务单、明确的可用工具、继承权限核查、统一超时、执行证据和 Codex 独立验收；采用该平台的进程组清理方式。不以平台适配为由改写用户认证配置。
