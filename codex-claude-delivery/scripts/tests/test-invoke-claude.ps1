# Offline regression tests. Uses a compiled dummy executable, never Claude.
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false)
$runner = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\invoke-claude.ps1'))
$testRoot = Join-Path $env:TEMP ('claude-runner-tests-' + [guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($testRoot)
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Tests require the Windows .NET Framework C# compiler.' }
$fakeExe = Join-Path $testRoot 'dummy-executor.exe'
$sourcePath = Join-Path $testRoot 'dummy-executor.cs'
$source = @'
using System;
using System.IO;
using System.Text;
using System.Threading;
using System.Diagnostics;
public static class Program {
    static string Json(string s) {
        return "\"" + s.Replace("\\", "\\\\").Replace("\"", "\\\"")
            .Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t") + "\"";
    }
    public static void Main(string[] args) {
        if (args.Length > 0 && args[0] == "--child") { Thread.Sleep(10000); return; }
        File.WriteAllText("started.txt", "dummy only");
        string[] quoted = new string[args.Length];
        for (int i = 0; i < args.Length; i++) quoted[i] = Json(args[i]);
        File.WriteAllText("arguments.json", "[" + string.Join(",", quoted) + "]");
        if (new DirectoryInfo(Environment.CurrentDirectory).Name == "blocked-input") {
            Thread.Sleep(10000); return;
        }
        string task;
        using (var reader = new StreamReader(Console.OpenStandardInput(), Encoding.UTF8)) {
            task = reader.ReadToEnd();
        }
        File.WriteAllText("received-input.txt", task, new UTF8Encoding(false));
        if (task == "HANG_WITH_CHILD") {
            var start = new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName, "--child");
            start.UseShellExecute = false; start.CreateNoWindow = true;
            using (var child = Process.Start(start)) {
                File.WriteAllText("child.pid", child.Id.ToString());
                Thread.Sleep(10000);
            }
        }
        if (task == "NONZERO") { Console.Error.WriteLine("dummy failure"); Environment.Exit(7); }
        if (task == "INVALID") { Console.WriteLine("not json"); return; }
        if (task == "MODEL_ERROR") { Console.WriteLine("{\"is_error\":true}"); return; }
        if (task == "DENIED") {
            Console.WriteLine("{\"is_error\":false,\"permission_denials\":[{\"tool_name\":\"Bash\"}]}"); return;
        }
        Console.WriteLine("{\"is_error\":false,\"permission_denials\":[]}");
    }
}
'@
[IO.File]::WriteAllText($sourcePath, $source, $utf8)
& $compiler /nologo /target:exe "/out:$fakeExe" $sourcePath
if ($LASTEXITCODE -ne 0) { throw 'Dummy executable compilation failed.' }

function Invoke-TestCase {
    param([string]$Name, [string]$TaskText, [int]$Limit = 10, [string[]]$Extra = @())
    $project = Join-Path $testRoot $Name
    [void][IO.Directory]::CreateDirectory($project)
    $taskPath = Join-Path $project 'task.md'
    [IO.File]::WriteAllText($taskPath, $TaskText, $utf8)
    $runDirectory = Join-Path $project 'attempts'
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $runner,
        '-ProjectPath', $project, '-TaskFile', $taskPath, '-RunDirectory', $runDirectory,
        '-TimeoutSeconds', [string]$Limit, '-ClaudePath', $fakeExe) + $Extra
    # Expected CLI failures must be inspected, not abort Windows PowerShell's caller.
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & powershell.exe @arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    $metaPath = Join-Path $runDirectory 'attempt-01.meta.json'
    $metadata = $null
    if (Test-Path -LiteralPath $metaPath) {
        $metadata = Get-Content -LiteralPath $metaPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    return [pscustomobject]@{ Project=$project; ExitCode=$exitCode; Metadata=$metadata; Output=$output }
}

$passed = New-Object 'Collections.Generic.List[string]'
$unicodeTask = 'UTF-8 task: ' + [char]0x6D4B + [char]0x8BD5
$normal = Invoke-TestCase 'normal success' $unicodeTask
if ($normal.ExitCode -ne 0 -or $normal.Metadata.execution_status -ne 'execution_succeeded') {
    throw ('Normal execution failed: ' + ($normal.Output | Out-String))
}
$received = [IO.File]::ReadAllText((Join-Path $normal.Project 'received-input.txt'), [Text.Encoding]::UTF8)
if ($received -ne $unicodeTask) { throw 'UTF-8 task delivery failed.' }
$argsSeen = Get-Content -LiteralPath (Join-Path $normal.Project 'arguments.json') -Raw | ConvertFrom-Json
$toolsIndex = [Array]::IndexOf($argsSeen, '--tools')
if ($toolsIndex -lt 0 -or $argsSeen[$toolsIndex + 1] -ne 'Read,Glob,Grep,Edit,Write' -or
    '--model' -in $argsSeen -or $normal.Metadata.acceptance -ne 'not_evaluated_by_runner') {
    throw 'Default tools, model inheritance, or acceptance boundary regressed.'
}
$passed.Add('Normal execution, UTF-8, default tool availability and no model override')

foreach ($toolCase in @('Read,Bash', 'Read,PowerShell', 'default')) {
    $safeName = 'rejected-' + $toolCase.Replace(',', '-')
    $rejected = Invoke-TestCase $safeName 'OK' 10 @('-AvailableTools', $toolCase)
    if ($rejected.ExitCode -eq 0 -or (Test-Path -LiteralPath (Join-Path $rejected.Project 'started.txt'))) {
        throw 'Unreviewed command tools or implicit default tools were not rejected before launch.'
    }
}
$passed.Add('Unreviewed Bash/PowerShell and implicit tool sets rejected before launch')

$reviewed = Invoke-TestCase 'reviewed-command-tools' 'OK' 10 @(
    '-AvailableTools', 'Read,Bash', '-AllowedTools', 'Read,Bash(npm run build)', '-CommandPermissionsReviewed')
if ($reviewed.ExitCode -ne 0 -or 'Bash' -notin $reviewed.Metadata.available_tools -or
    -not $reviewed.Metadata.command_permissions_reviewed) { throw 'Explicit reviewed tool scope was not passed.' }
$passed.Add('Explicit reviewed command tool availability recorded')

foreach ($case in @(
    @('nonzero', 'NONZERO', 'cli_error'),
    @('invalid-result', 'INVALID', 'invalid_result'),
    @('model-error', 'MODEL_ERROR', 'model_error'),
    @('permission-denied', 'DENIED', 'needs_review')
)) {
    $result = Invoke-TestCase $case[0] $case[1]
    if ($result.ExitCode -eq 0 -or $result.Metadata.execution_status -ne $case[2]) {
        throw ('Wrong result classification for ' + $case[0])
    }
}
$passed.Add('CLI error, invalid JSON, model error and permission denial classification')

$blocked = Invoke-TestCase 'blocked-input' ('x' * 1048576) 1
if (-not $blocked.Metadata.timed_out -or $blocked.Metadata.execution_status -ne 'execution_timeout' -or
    $blocked.Metadata.execution_phase -ne 'stdin_write' -or -not $blocked.Metadata.root_process_exited -or
    $blocked.Metadata.duration_ms -ge 8000) { throw 'Blocked input escaped the execution deadline.' }
$passed.Add('Blocked 1 MiB stdin is timed out and the dummy process stops')

$hanging = Invoke-TestCase 'hanging-execution' 'HANG_WITH_CHILD' 3
if (-not $hanging.Metadata.timed_out -or $hanging.Metadata.execution_phase -ne 'running' -or
    -not $hanging.Metadata.root_process_exited -or $hanging.Metadata.duration_ms -ge 8000) {
    throw 'Execution did not time out with bounded cleanup.'
}
$childId = [int](Get-Content -LiteralPath (Join-Path $hanging.Project 'child.pid') -Raw)
$remainingChild = Get-Process -Id $childId -ErrorAction SilentlyContinue
if ($remainingChild) {
    Stop-Process -Id $childId -ErrorAction SilentlyContinue
    throw 'Dummy child process survived tree cleanup.'
}
$passed.Add('Execution timeout stops the dummy process and its tracked child')

$evidencePath = Join-Path $normal.Project 'attempts\attempt-01.meta.json'
$beforeHash = (Get-FileHash -LiteralPath $evidencePath).Hash
$duplicate = Invoke-TestCase 'normal success' $unicodeTask
if ($duplicate.ExitCode -eq 0 -or (Get-FileHash -LiteralPath $evidencePath).Hash -ne $beforeHash) {
    throw 'Existing execution evidence was overwritten.'
}
$passed.Add('Existing attempt evidence preserved')

[pscustomobject]@{
    Mode = 'Offline dummy executable; no Claude requests or credentials'
    PassedGroups = $passed.Count
    Checks = @($passed.ToArray())
    TimeoutInputMilliseconds = $blocked.Metadata.duration_ms
    TimeoutExecutionMilliseconds = $hanging.Metadata.duration_ms
    TestArtifacts = $testRoot
} | ConvertTo-Json -Depth 5
