<#
.SYNOPSIS
    Windows launcher for Cairn.

.DESCRIPTION
    Thin wrapper around `uv run --project cairn cairn ...` so the common tasks are one
    command in PowerShell. Uses a uv-managed Python 3.13 (matching the upstream image), so
    it works even when the system Python is newer than the dependencies have wheels for.

.EXAMPLE
    .\cairn.ps1 setup                                   # one-time: install uv + deps
    .\cairn.ps1 serve                                   # API + web UI on http://127.0.0.1:8000
    .\cairn.ps1 dispatch                                # uses .\dispatch.yaml
    .\cairn.ps1 dispatch -Config dispatch.windows-mock.yaml
    .\cairn.ps1 check                                   # startup health checks only
    .\cairn.ps1 test                                    # run the test suite
    .\cairn.ps1 start                                   # serve + dispatch in two windows
#>
# Arguments are parsed by hand instead of with a param() block: under `powershell -File`
# (which cairn.cmd uses) Windows PowerShell 5.1 rejects a literal `--` as an ambiguous
# parameter name, so pass-through arguments would be impossible. Manual parsing behaves
# identically whether the script is called inline, via -File, or via cairn.cmd.
$ErrorActionPreference = 'Stop'

$Command = 'help'
$Config = 'dispatch.yaml'
$BindHost = '127.0.0.1'
$Port = 8000
$Rest = @()
$commands = @('setup', 'serve', 'dispatch', 'check', 'test', 'start', 'help')

$i = 0
$sawCommand = $false
while ($i -lt $args.Count) {
    $a = [string]$args[$i]
    if ($a -eq '--') {
        if ($i + 1 -lt $args.Count) { $Rest += $args[($i + 1)..($args.Count - 1)] }
        break
    }
    switch -Regex ($a) {
        '^-(Config|BindHost|Port)$' {
            if ($i + 1 -ge $args.Count) { throw "Missing value for $a" }
            $value = [string]$args[$i + 1]
            switch ($Matches[1]) {
                'Config' { $Config = $value }
                'BindHost' { $BindHost = $value }
                'Port' {
                    if ($value -notmatch '^\d+$') { throw "-Port must be a number, got '$value'" }
                    $Port = [int]$value
                }
            }
            $i += 2
            continue
        }
        '^-(h|help|\?)$' { $Command = 'help'; $sawCommand = $true; $i += 1; continue }
        default {
            if (-not $sawCommand -and -not $a.StartsWith('-')) {
                if ($commands -notcontains $a.ToLower()) { throw "Unknown command '$a'. Use one of: $($commands -join ', ')" }
                $Command = $a.ToLower()
                $sawCommand = $true
            } else {
                $Rest += $a   # anything unrecognised is passed through
            }
            $i += 1
        }
    }
}
$Root = $PSScriptRoot
$PythonVersion = '3.13'
Set-Location $Root

function Get-Uv {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { return @($uv.Source) }
    # Fall back to uv installed as a Python package (`py -m pip install --user uv`).
    foreach ($py in @('py', 'python')) {
        $cmd = Get-Command $py -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        & $cmd.Source -m uv --version *> $null
        if ($LASTEXITCODE -eq 0) { return @($cmd.Source, '-m', 'uv') }
    }
    return $null
}

function Invoke-Uv([string[]]$UvArgs) {
    $uv = Get-Uv
    if (-not $uv) { throw "uv not found. Run '.\cairn.ps1 setup' first." }
    $exe = $uv[0]
    $prefix = @()
    if ($uv.Count -gt 1) { $prefix = $uv[1..($uv.Count - 1)] }
    & $exe @prefix @UvArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Invoke-Cairn([string[]]$CairnArgs) {
    Invoke-Uv (@('run', '--project', 'cairn', '--python', $PythonVersion, 'cairn') + $CairnArgs)
}

function Assert-Config {
    if (-not (Test-Path $Config)) {
        throw "Config '$Config' not found. Copy dispatch.windows.example.yaml to dispatch.yaml (or pass -Config)."
    }
}

switch ($Command) {
    'setup' {
        if (-not (Get-Uv)) {
            Write-Host '[*] Installing uv (Python package manager) for the current user...'
            $py = Get-Command py -ErrorAction SilentlyContinue
            if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
            if (-not $py) { throw 'No Python found. Install Python from https://www.python.org/downloads/ or run: winget install astral-sh.uv' }
            & $py.Source -m pip install --user --upgrade uv
            if ($LASTEXITCODE -ne 0) { throw 'pip install uv failed' }
        }
        Write-Host "[*] Installing Python $PythonVersion and Cairn dependencies..."
        Invoke-Uv @('sync', '--project', 'cairn', '--python', $PythonVersion, '--group', 'dev')
        if (-not (Test-Path 'dispatch.yaml')) {
            Copy-Item 'dispatch.windows.example.yaml' 'dispatch.yaml'
            Write-Host '[+] Created dispatch.yaml from dispatch.windows.example.yaml - review it before dispatching.'
        }
        Write-Host '[+] Setup complete. Next: .\cairn.ps1 start'
    }
    'serve' {
        Invoke-Cairn (@('serve', '--host', $BindHost, '--port', "$Port") + $Rest)
    }
    'dispatch' {
        Assert-Config
        Invoke-Cairn (@('dispatch', '--config', $Config) + $Rest)
    }
    'check' {
        Assert-Config
        Invoke-Cairn (@('dispatch', '--config', $Config, '--startup-healthcheck-only') + $Rest)
    }
    'test' {
        Invoke-Uv (@('run', '--project', 'cairn', '--python', $PythonVersion, '--group', 'dev', 'pytest', 'cairn/tests') + $Rest)
    }
    'start' {
        Assert-Config
        $shell = (Get-Process -Id $PID).Path
        $self = Join-Path $Root 'cairn.ps1'
        Start-Process $shell -ArgumentList @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', $self, 'serve', '-BindHost', $BindHost, '-Port', "$Port")
        Write-Host "[*] Waiting for the server on http://${BindHost}:$Port ..."
        $up = $false
        foreach ($i in 1..60) {
            try { Invoke-WebRequest "http://${BindHost}:$Port/projects" -UseBasicParsing -TimeoutSec 2 | Out-Null; $up = $true; break }
            catch { Start-Sleep -Milliseconds 500 }
        }
        if (-not $up) { throw 'Server did not start - check the server window for errors.' }
        Start-Process $shell -ArgumentList @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', $self, 'dispatch', '-Config', $Config)
        Start-Process "http://${BindHost}:$Port/"
        Write-Host "[+] Server and dispatcher running in their own windows. UI: http://${BindHost}:$Port/"
    }
    default {
        Get-Help $PSCommandPath -Detailed
    }
}
