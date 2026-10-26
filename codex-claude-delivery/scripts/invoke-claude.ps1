<#
.SYNOPSIS
Runs one Claude Code execution attempt and records evidence for Codex review.
.DESCRIPTION
Requires Windows PowerShell 5.1+ and a configured Claude Code CLI. Input is a
UTF-8 task file. Credentials remain in Claude's existing local configuration.
Exit 0 means the CLI returned a non-error result, not that Codex accepted it.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$TaskFile,
    [Parameter(Mandatory = $true)][string]$RunDirectory,
    [ValidateRange(1, 9999)][int]$Attempt = 1,
    [ValidateRange(1, 86400)][int]$TimeoutSeconds = 900,
    [ValidateNotNullOrEmpty()][string]$AvailableTools = 'Read,Glob,Grep,Edit,Write',
    [ValidateNotNullOrEmpty()][string]$AllowedTools = 'Read,Glob,Grep,Edit,Write',
    [switch]$CommandPermissionsReviewed,
    [string]$Model,
    [string]$ClaudePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)

function ConvertTo-WindowsArgument {
    param([AllowEmptyString()][string]$Value)
    # Windows command-line quoting: double backslashes before a quote or EOL.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Get-RemainingMilliseconds {
    param([Diagnostics.Stopwatch]$Timer, [int]$LimitSeconds)
    return [int][Math]::Max(0, ([long]$LimitSeconds * 1000 - $Timer.ElapsedMilliseconds))
}

function Wait-ExecutorTask {
    param([Threading.Tasks.Task]$Task, [Diagnostics.Stopwatch]$Timer,
        [int]$LimitSeconds, [string]$Stage)
    $remaining = Get-RemainingMilliseconds $Timer $LimitSeconds
    if ($remaining -le 0 -or -not $Task.Wait($remaining)) {
        throw [TimeoutException]::new("Executor time limit exceeded during $Stage.")
    }
    [void]$Task.GetAwaiter().GetResult()
}

function Stop-ExecutorProcess {
    param([Diagnostics.Process]$Executor)
    if ($Executor.HasExited) { return $true }
    $killer = New-Object Diagnostics.Process
    try {
        $killer.StartInfo.FileName = Join-Path $env:SystemRoot 'System32\taskkill.exe'
        $killer.StartInfo.Arguments = '/PID {0} /T /F' -f $Executor.Id
        $killer.StartInfo.UseShellExecute = $false
        $killer.StartInfo.CreateNoWindow = $true
        $killer.StartInfo.RedirectStandardOutput = $true
        $killer.StartInfo.RedirectStandardError = $true
        [void]$killer.Start()
        $killStdout = $killer.StandardOutput.ReadToEndAsync()
        $killStderr = $killer.StandardError.ReadToEndAsync()
        if (-not $killer.WaitForExit(3000)) { $killer.Kill() }
        if (-not $Executor.WaitForExit(1000)) {
            $Executor.Kill()
            [void]$Executor.WaitForExit(1000)
        }
    } catch {
        try {
            if (-not $Executor.HasExited) {
                $Executor.Kill()
                [void]$Executor.WaitForExit(1000)
            }
        } catch { }
    } finally {
        $killer.Dispose()
    }
    return $Executor.HasExited
}

$supportedTools = @('Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash', 'PowerShell')
$availableToolNames = @($AvailableTools.Split(',') | ForEach-Object {
    $candidate = $_.Trim()
    $canonical = @($supportedTools | Where-Object { $_ -ieq $candidate })
    if ($canonical.Count -ne 1) { throw 'AvailableTools contains an unsupported tool name.' }
    $canonical[0]
} | Select-Object -Unique)
$commandToolsEnabled = @($availableToolNames | Where-Object { $_ -in @('Bash', 'PowerShell') }).Count -gt 0
if ($commandToolsEnabled -and -not $CommandPermissionsReviewed) {
    throw 'Command tools require review of inherited permissions; see reference/cli-execution.md.'
}

$projectItem = Get-Item -LiteralPath $ProjectPath
if (-not $projectItem.PSIsContainer) { throw 'ProjectPath must be an existing directory.' }
$projectFullPath = $projectItem.FullName
$taskItem = Get-Item -LiteralPath $TaskFile
if ($taskItem.PSIsContainer) { throw 'TaskFile must be a UTF-8 text file.' }
$taskFullPath = $taskItem.FullName
$taskText = [System.IO.File]::ReadAllText($taskFullPath, [System.Text.Encoding]::UTF8)
if ([string]::IsNullOrWhiteSpace($taskText)) { throw 'TaskFile is empty.' }

if ([string]::IsNullOrWhiteSpace($ClaudePath)) {
    $claudeCommand = Get-Command claude.exe -ErrorAction SilentlyContinue
    if ($claudeCommand) {
        $ClaudePath = $claudeCommand.Source
    } else {
        $ClaudePath = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    }
}
$claudeItem = Get-Item -LiteralPath $ClaudePath
if ($claudeItem.PSIsContainer -or $claudeItem.Extension -ine '.exe') {
    throw 'ClaudePath must point to a Claude Code .exe executable.'
}
$ClaudePath = $claudeItem.FullName

$runFullPath = [System.IO.Path]::GetFullPath($RunDirectory)
[void][System.IO.Directory]::CreateDirectory($runFullPath)
$prefix = 'attempt-{0:D2}' -f $Attempt
$resultPath = Join-Path $runFullPath ($prefix + '.result.json')
$stderrPath = Join-Path $runFullPath ($prefix + '.stderr.log')
$metadataPath = Join-Path $runFullPath ($prefix + '.meta.json')
foreach ($outputPath in @($resultPath, $stderrPath, $metadataPath)) {
    if (Test-Path -LiteralPath $outputPath) {
        throw "Attempt evidence already exists; choose a new Attempt or RunDirectory: $outputPath"
    }
}

$executorRole = @'
You execute a scoped task assigned by Codex. Follow the task file and applicable
project instructions. Codex owns planning and final acceptance. Implement only
the assigned scope. Do not weaken tests or acceptance criteria, alter credentials,
or bypass tool permissions. Report changed files, commands actually run, evidence,
and limitations. Do not declare final acceptance. Do not read or output secrets.
'@

$cliArguments = @(
    '-p',
    '--output-format', 'json',
    '--no-session-persistence',
    '--permission-mode', 'dontAsk',
    '--permission-prompts', 'none',
    '--allowedTools', $AllowedTools,
    '--tools', ($availableToolNames -join ','),
    '--strict-mcp-config',
    '--append-system-prompt', $executorRole
)
if (-not [string]::IsNullOrWhiteSpace($Model)) {
    $cliArguments += @('--model', $Model)
}
$argumentString = ($cliArguments | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' '

$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $ClaudePath
$startInfo.Arguments = $argumentString
$startInfo.WorkingDirectory = $projectFullPath
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.StandardOutputEncoding = $utf8
$startInfo.StandardErrorEncoding = $utf8

$process = New-Object System.Diagnostics.Process
$process.StartInfo = $startInfo
$startedAt = [DateTime]::UtcNow
$timer = [Diagnostics.Stopwatch]::StartNew()
$timedOut = $false
$processExitCode = $null
$stdoutText = ''
$stderrText = ''
$executionStatus = 'runner_error'
$runnerError = $null
$permissionDenialCount = 0
$stdoutTask = $null
$stderrTask = $null
$stdinTask = $null
$flushTask = $null
$started = $false
$executionPhase = 'starting'
$rootProcessExited = $null
$treeCleanupAttempted = $false

try {
    [void]$process.Start()
    $started = $true
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $taskBytes = $utf8.GetBytes($taskText)
    $executionPhase = 'stdin_write'
    $stdinTask = $process.StandardInput.BaseStream.WriteAsync($taskBytes, 0, $taskBytes.Length)
    Wait-ExecutorTask $stdinTask $timer $TimeoutSeconds $executionPhase
    $executionPhase = 'stdin_flush'
    $flushTask = $process.StandardInput.BaseStream.FlushAsync()
    Wait-ExecutorTask $flushTask $timer $TimeoutSeconds $executionPhase
    $process.StandardInput.BaseStream.Close()

    $executionPhase = 'running'
    $remaining = Get-RemainingMilliseconds $timer $TimeoutSeconds
    if ($remaining -le 0 -or -not $process.WaitForExit($remaining)) {
        throw [TimeoutException]::new('Executor time limit exceeded during running.')
    }

    $executionPhase = 'output'
    Wait-ExecutorTask $stdoutTask $timer $TimeoutSeconds $executionPhase
    Wait-ExecutorTask $stderrTask $timer $TimeoutSeconds $executionPhase
    $stdoutText = $stdoutTask.GetAwaiter().GetResult()
    $stderrText = $stderrTask.GetAwaiter().GetResult()
    if ($process.HasExited) { $processExitCode = $process.ExitCode }

    if ($processExitCode -ne 0) {
        $executionStatus = 'cli_error'
    } else {
        try {
            $resultObject = $stdoutText | ConvertFrom-Json
            if (-not $resultObject -or -not $resultObject.PSObject.Properties['is_error']) {
                throw 'CLI output is missing the is_error field.'
            }
            if ($resultObject.PSObject.Properties['permission_denials']) {
                $permissionDenialCount = @($resultObject.permission_denials).Count
            }
            if ($resultObject.is_error) {
                $executionStatus = 'model_error'
            } elseif ($permissionDenialCount -gt 0) {
                $executionStatus = 'needs_review'
            } else {
                $executionStatus = 'execution_succeeded'
            }
        } catch {
            $executionStatus = 'invalid_result'
            $runnerError = $_.Exception.Message
        }
    }
} catch {
    $runnerError = $_.Exception.Message
    $timedOut = $_.Exception -is [TimeoutException]
    $executionStatus = if ($timedOut) { 'execution_timeout' } else { 'runner_error' }
    if ($started -and -not $process.HasExited) {
        $treeCleanupAttempted = $true
        $rootProcessExited = Stop-ExecutorProcess $process
    }
    # A short, bounded drain after cleanup preserves available evidence.
    $outputTasks = @($stdoutTask, $stderrTask | Where-Object { $null -ne $_ })
    if ($outputTasks.Count -gt 0) {
        try { [void][Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]$outputTasks, 1000) } catch { }
    }
    if ($null -ne $stdoutTask -and $stdoutTask.IsCompleted -and -not $stdoutTask.IsFaulted) {
        $stdoutText = $stdoutTask.GetAwaiter().GetResult()
    }
    if ($null -ne $stderrTask -and $stderrTask.IsCompleted -and -not $stderrTask.IsFaulted) {
        $stderrText = $stderrTask.GetAwaiter().GetResult()
    }
} finally {
    if ($started) {
        $rootProcessExited = $process.HasExited
        if ($rootProcessExited) { $processExitCode = $process.ExitCode }
    }
    foreach ($ioTask in @($stdinTask, $flushTask, $stdoutTask, $stderrTask)) {
        if ($null -ne $ioTask -and $ioTask.IsFaulted) { [void]$ioTask.Exception }
    }
    $timer.Stop()
    [IO.File]::WriteAllText($resultPath, $stdoutText, $utf8)
    [IO.File]::WriteAllText($stderrPath, $stderrText, $utf8)
    $metadata = [ordered]@{
        schema_version = 1
        project_path = $projectFullPath
        task_file = $taskFullPath
        attempt = $Attempt
        started_at_utc = $startedAt.ToString('o')
        duration_ms = $timer.ElapsedMilliseconds
        process_exit_code = $processExitCode
        timed_out = $timedOut
        timeout_seconds = $TimeoutSeconds
        execution_phase = $executionPhase
        root_process_exited = $rootProcessExited
        process_tree_cleanup_attempted = $treeCleanupAttempted
        stdout_capture_complete = ($null -ne $stdoutTask -and $stdoutTask.IsCompleted -and -not $stdoutTask.IsFaulted)
        stderr_capture_complete = ($null -ne $stderrTask -and $stderrTask.IsCompleted -and -not $stderrTask.IsFaulted)
        execution_status = $executionStatus
        permission_denial_count = $permissionDenialCount
        model_override = $Model
        available_tools = $availableToolNames
        additional_auto_approval_rules = $AllowedTools
        command_permissions_reviewed = [bool]$CommandPermissionsReviewed
        permission_boundary = 'tool_availability_with_inherited_settings_not_a_sandbox'
        result_file = $resultPath
        stderr_file = $stderrPath
        runner_error = $runnerError
        acceptance = 'not_evaluated_by_runner'
    }
    [IO.File]::WriteAllText($metadataPath, ($metadata | ConvertTo-Json -Depth 5), $utf8)
    $process.Dispose()
}

# Print compact metadata only. Codex reads the result file for independent review.
$metadata | ConvertTo-Json -Depth 5
if ($executionStatus -eq 'execution_succeeded') { exit 0 }
exit 1
