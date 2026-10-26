# PowerShell 诊断、持久化与回滚

以下为按证据填值、分步执行的模板，不可整段盲跑。仅覆盖 IPv4 主机路由；文档保留地址必须替换。不要照搬历史公司网络网关。普通会话可诊断，写路由需要管理员权限。

## 只读采集

```powershell
Get-NetAdapter -IncludeHidden |
    Select-Object Name, InterfaceIndex, Status, HardwareInterface, InterfaceDescription
Get-NetIPConfiguration -All
Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore
Get-NetRoute -AddressFamily IPv4 -PolicyStore PersistentStore
route.exe PRINT -4
Resolve-DnsName -Name 'app.example.com' -Type A -DnsOnly
Resolve-DnsName -Name 'app.example.com' -Type AAAA -DnsOnly
Find-NetRoute -RemoteIPAddress '192.0.2.10'
```

`../scripts/inspect-routes.ps1` 将 IPv4 信息汇总为 JSON，显式记录读取失败和 IPv6 未检测状态。可将输出保存到用户任务资料目录，不写入项目代码。按需要检查系统代理和 WinHTTP，不打印带凭据的代理 URL。代理关闭仍可能存在 VPN `/1` 路由。路由只选择本机出口，不能代替上游网络、服务器防火墙或服务访问授权。

## 计划与备份

从采集结果查证物理接口、当前网关和所有目标 IP，再逐个填写。不能假定物理默认出口必然能访问内网。

同一服务的全部 A 记录及本次确认的必要登录依赖作为一个批次；按 IP 去重。修改前保存整个批次的计划与原始状态，逐条更新实际变更日志。下面展示单目标字段，处理多 IP 时每个目标使用独立记录，不覆盖前一条记录。

```powershell
$ErrorActionPreference = 'Stop'
$targetIp = '192.0.2.10'        # 替换；多个 A 记录逐条处理
$prefix = "$targetIp/32"
$physicalIndex = 12            # 替换为查证的物理接口索引
$physicalGateway = '192.0.2.1'  # 替换为该接口当前网关
$plannedMetric = 1
$backupRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'VpnRouteBackups'
$backupDir = Join-Path $backupRoot ('vpn-route-audit-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backupDir -ErrorAction Stop | Out-Null

# 先完整读取，再过滤，避免把读取失败当作不存在。
$allActive = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore -ErrorAction Stop)
$allPersistent = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore PersistentStore -ErrorAction Stop)
$activeBefore = @($allActive | Where-Object DestinationPrefix -eq $prefix)
$persistentBefore = @($allPersistent | Where-Object DestinationPrefix -eq $prefix)
$record = [ordered]@{
    CapturedAt = (Get-Date).ToString('o')
    Prefix = $prefix
    PlannedInterfaceIndex = $physicalIndex
    PlannedNextHop = $physicalGateway
    PlannedMetric = $plannedMetric
    ActiveBefore = @($activeBefore | Select-Object DestinationPrefix, InterfaceIndex, NextHop, RouteMetric)
    PersistentBefore = @($persistentBefore | Select-Object DestinationPrefix, InterfaceIndex, NextHop, RouteMetric)
    EffectiveBefore = @(Find-NetRoute -RemoteIPAddress $targetIp | Select-Object DestinationPrefix, InterfaceIndex, NextHop, IPAddress, RouteMetric)
}
$backupPath = Join-Path $backupDir 'before.json'
$record | ConvertTo-Json -Depth 8 -ErrorAction Stop |
    Set-Content -LiteralPath $backupPath -Encoding UTF8 -ErrorAction Stop
$routePrint = @(route.exe PRINT -4)
if ($LASTEXITCODE -ne 0 -or $routePrint.Count -eq 0) { throw '路由表导出失败，停止变更' }
$routePrintPath = Join-Path $backupDir 'route-print-before.txt'
$routePrint | Set-Content -LiteralPath $routePrintPath -Encoding UTF8 -ErrorAction Stop

# 写入和读回任一步失败都必须终止，不能继续进入 New-NetRoute。
$savedRecord = Get-Content -LiteralPath $backupPath -Raw -Encoding UTF8 -ErrorAction Stop |
    ConvertFrom-Json -ErrorAction Stop
if ($savedRecord.Prefix -ne $prefix -or
    $savedRecord.PlannedInterfaceIndex -ne $physicalIndex -or
    $savedRecord.PlannedNextHop -ne $physicalGateway -or
    $savedRecord.PlannedMetric -ne $plannedMetric) { throw '备份读回核验失败，停止变更' }
$savedPrint = Get-Content -LiteralPath $routePrintPath -Raw -Encoding UTF8 -ErrorAction Stop
if ([string]::IsNullOrWhiteSpace($savedPrint)) { throw '路由表备份为空，停止变更' }
```

同时保存 DNS、接口别名/描述、地址/网关和业务测试范围。默认备份到文档目录，也可改为用户指定的持久资料目录。所有原始备份和计划在第一条变更之前完成；日志区分原已存在、临时创建、持久创建和失败恢复。修改后立即记录实际结果，不能只保存期望计划。日志续写失败时停止后续新增操作，利用先前备份及内存中的精确变更记录清理已创建项，并报告记录缺口。

检查管理员权限：

```powershell
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

返回 false 时报告需管理员会话及具体操作，不修改 UAC、安全策略或创建提权任务。

## 临时验证

检查精确前缀的活动/持久记录。正确规则直接复用；同前缀不同下一跳/接口的规则先解释冲突，不自动替换。

```powershell
# 仅在确需创建且没有同前缀冲突时执行。
$createdTemporary = New-NetRoute -DestinationPrefix $prefix `
    -InterfaceIndex $physicalIndex -NextHop $physicalGateway `
    -RouteMetric $plannedMetric -PolicyStore ActiveStore -ErrorAction Stop
Find-NetRoute -RemoteIPAddress $targetIp
```

记录本次确实创建的 `$createdTemporary`，验证有效选路选中预期 `/32`、接口和下一跳，再测业务。VPN `/1` 比物理 `/0` 更具体；`/32` 覆盖单一主机流量。

## 停止与失败清理

- 全批临时选路核验通过，且服务域名的网络/TLS 验证通过，才能进入持久化。逐 IP 应用测试没做时明确记录未测，不能据域名的一次成功声称每个 IP 的应用均成功。
- 任一目标选路错误、网络连接或 TLS 失败时，不持久化；按“精确回滚”撤销本批新建临时规则，保留原有/复用规则。多 IP 部分成功默认也清理本批新增规则，避免留下一半生效的配置；用户另有明确的分批保留要求时按其范围执行并列出结果。
- HTTP 登录跳转、401/403 或数据库鉴权失败应单独归类。若已证实网络路径和 TLS 有效，不能将账号权限问题当作网络失败；根据任务范围报告授权未通过，不修改账号权限，也不声称业务访问完整成功。
- 查询失败、规则被第三方修改或无法精确确认归属时，停止自动删除。报告尚存规则和具体阻塞；不盲目重试、清表或扩大分流范围。

## 持久化

本机 `route.exe /?` 说明：`-p` 配合 ADD 可跨重启保留。不直接改注册表。

```powershell
route.exe -p -4 ADD $targetIp MASK 255.255.255.255 $physicalGateway METRIC $plannedMetric IF $physicalIndex
$addExitCode = $LASTEXITCODE
```

规则完全一致且活动选路也正确时复用。仅持久表已有记录、活动选路仍不正确时，不能直接判定完成；先查明激活/选路原因，不覆盖旧记录。若同一临时规则导致 ADD 冲突：

1. 先重读活动/持久表，核对临时规则确实由本次创建、变更前没有同前缀规则、且未发生第三方变更。
2. 仅对该精确 CIM 对象执行 `Remove-NetRoute -InputObject $createdTemporary -Confirm:$false -ErrorAction Stop`。
3. 立即执行上述持久 ADD，并保存退出码及输出。
4. ADD 失败则重读两表，排除部分成功和并发变更。若本次临时规则确实缺失且无冲突，使用之前相同的 `New-NetRoute ... -PolicyStore ActiveStore` 恢复；核验恢复选路并记录结果。停止新增操作，随后按日志回滚本批新建永久与临时规则，恢复批次开始前的状态；不能删除后失败就退出，或将部分持久化当作全批完成。恢复/回滚失败则报告精确剩余状态，不掩盖异常。

来自更早任务或他人的临时规则不能套用此删除流程；先调查并准备精确恢复。`New-NetRoute -PolicyStore PersistentStore` 在部分环境可能写入失败，根据实际错误改用支持的 `route.exe -p ADD`，不能将失败当作已持久化。

## 验收

```powershell
Find-NetRoute -RemoteIPAddress $targetIp
Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore |
    Where-Object DestinationPrefix -eq $prefix
Get-NetRoute -AddressFamily IPv4 -PolicyStore PersistentStore |
    Where-Object DestinationPrefix -eq $prefix
route.exe PRINT -4
```

目标、下一跳、metric 应与计划一致，有效选路经过所选物理接口。持久记录的接口字段在不同 Windows 版本可能表现不同，以活动接口与实际选路交叉确认。关键读取失败标记为未验证。

- 网站可用 `curl.exe --noproxy '*' -I --connect-timeout 10 --max-time 20 'https://app.example.com/'`，或浏览器观察实际 GET/跳转；HEAD 不支持时改 GET。401/403 可能是应用鉴权失败，不能一概当作网络失败或业务成功。证书错误保留原文，不能用 `-k` 掩盖。
- 数据库用 `Test-NetConnection -ComputerName 'db.example.com' -Port 3306` 仅测 TCP；按用户实际端口测试。MySQL 认证及 `SELECT 1` 才验证业务连接，不把密码放入 `mysql -pPASSWORD` 命令/日志。
- 用户要求只读时，可在支持的 MySQL 客户端会话执行 `SET SESSION TRANSACTION READ ONLY; SELECT 1; SELECT @@session.transaction_read_only;`，随后关闭连接。不主动写入验证只读；会话变量不是账号权限审计。
- 保持用户要求的 TLS 保障，不自动降级。缺少证书或连接要求时报告具体阻塞。
- 核实 VPN 一直开启。未实测重启就写“持久表已核验，未实际重启验证”。

## 精确回滚

按实际变更日志逐条处理。重读并核对目标、下一跳、接口、metric 与记录一致；若第三方后来修改过规则，停止自动删除，报告差异。

- 本次只创建临时规则：从 ActiveStore 获取精确匹配的单个 CIM 对象，使用 `Remove-NetRoute -InputObject <该对象> -Confirm:$false`。
- 本次创建持久规则：从 PersistentStore 获取精确匹配的单个 CIM 对象并删除，再重读 ActiveStore，仅删除残留的本次同一精确规则。验证两表和有效选路。本机帮助说明 `-p` 对 DELETE 无特殊含义，不能依赖 `route -p DELETE` 区分存储。
- 若本次改变过原有规则，按备份恢复其原始存储类型、前缀、下一跳、接口、metric。未改变的原有规则不动。
- 不使用 `route -f`、通配删除、只按前缀宽泛删除、全表重建或重置网络。复用的原有正确永久规则不纳入本次回滚删除清单。

## 维护边界

规则记录的是当时 IP 和出口。DNS 多 IP、服务迁移、DHCP 网关、接口变化和 VPN 策略变化均可能影响结果，新 IP 不继承旧 `/32` 规则。只有用户另行要求自动维护时，才设计计划任务或动态更新机制，并另给变更和回滚方案。
