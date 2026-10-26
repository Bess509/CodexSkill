# Claude 执行任务单

由 Codex 为每个任务和返修轮次填写本模板。所有花括号项都是模板参数，不是业务默认值。

- 运行 ID / 任务 ID / 轮次：{{run_id}} / {{task_id}} / {{attempt}}
- 项目绝对路径：{{project_path}}
- 计划文件：{{plan_path}}
- 原目标及本轮目标：{{objective}}
- 本轮输出文件或产物：{{deliverables}}

## 范围与约束

- 允许修改：{{allowed_paths}}
- 禁止修改及用户已有改动：{{protected_paths_and_baseline}}
- 获准工具/命令：{{allowed_commands}}
- 项目规范与必读文件：{{project_instructions}}
- 外部操作授权：{{existing_authorization_or_none}}

只执行分配的工作，不改变验收标准或擅自扩大范围。不要读取或输出密钥，不修改认证配置。不能执行的步骤明确说明原因，不绕过权限限制。

## 业务上下文

{{relevant_business_definitions_with_sources_and_versions}}

未提供的业务定义不能自行编造；影响实现的缺口说明具体问题，其余按任务单中已记录的假设处理。

## 验收条件（由 Codex 制定）

| 条件 ID | 可观察结果 | 检查方法 | 必须满足 |
|---|---|---|---|
| {{criterion_id}} | {{expected_behavior}} | {{check}} | {{required}} |

## 上轮失败与本轮返修

首次执行写“首次执行”。返修填写具体失败条件、复现方法、证据、修复目标和必须保留的已通过条件。

{{previous_failure_and_repair_target}}

## 返回内容

返回简明执行报告：已修改的文件、实现内容、执行过的命令与结果、对应验收条件的自查证据、权限拒绝/未完成项。不自行宣称最终验收通过，等待 Codex 独立复核。
