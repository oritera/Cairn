<#
.SYNOPSIS
    Build the Cairn desktop app (Cairn.exe) and its installer (dist\Cairn-Setup-<version>.exe).

.DESCRIPTION
    1. Syncs dependencies (Python 3.13 via uv, desktop + build extras)
    2. Renders the app icon
    3. Runs the test suite (skip with -SkipTests)
    4. Builds build\dist\Cairn\Cairn.exe with PyInstaller
    5. Compiles the installer with Inno Setup (skipped with a note if ISCC.exe is missing)

.EXAMPLE
    .\desktop\build.ps1
    .\desktop\build.ps1 -SkipTests
#>
param([switch]$SkipTests)

# 'Continue', not 'Stop': in Windows PowerShell 5.1 'Stop' turns any stderr line from a native
# tool (uv/PyInstaller progress output) into a fatal error when output is redirected. Every step
# checks $LASTEXITCODE and throws on real failures instead.
$ErrorActionPreference = 'Continue'
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

function Get-Uv {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { return @($uv.Source) }
    foreach ($py in @('py', 'python')) {
        $cmd = Get-Command $py -ErrorAction SilentlyContinue
        if ($cmd) { & $cmd.Source -m uv --version *> $null; if ($LASTEXITCODE -eq 0) { return @($cmd.Source, '-m', 'uv') } }
    }
    throw "uv not found. Run '.\cairn.ps1 setup' or 'winget install astral-sh.uv'."
}
$uv = Get-Uv
function Uv { param([string[]]$UvArgs) $exe = $uv[0]; $pre = @(); if ($uv.Count -gt 1) { $pre = $uv[1..($uv.Count - 1)] }; & $exe @pre @UvArgs; if ($LASTEXITCODE -ne 0) { throw "uv $($UvArgs -join ' ') failed" } }

$version = (Select-String -Path 'cairn\src\cairn\__init__.py' -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "[*] Building Cairn $version"

Write-Host '[*] Syncing dependencies'
Uv @('sync', '--project', 'cairn', '--python', '3.13', '--group', 'dev', '--group', 'build', '--extra', 'desktop')
$py = Join-Path $Root 'cairn\.venv\Scripts\python.exe'

Write-Host '[*] Rendering icon'
& $py desktop\make_icon.py; if ($LASTEXITCODE -ne 0) { throw 'icon generation failed' }

if (-not $SkipTests) {
    Write-Host '[*] Running tests'
    & $py -m pytest cairn\tests -q -p no:cacheprovider; if ($LASTEXITCODE -ne 0) { throw 'tests failed' }
}

Write-Host '[*] Building Cairn.exe (PyInstaller)'
& $py -m PyInstaller desktop\cairn.spec --noconfirm --clean --distpath build\dist --workpath build\work
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
& "build\dist\Cairn\Cairn.exe" --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'built Cairn.exe does not start' }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Write-Warning 'Inno Setup not found - skipping the installer. Install it with: winget install JRSoftware.InnoSetup'
    Write-Host "[+] App built: build\dist\Cairn\Cairn.exe"
    exit 0
}
Write-Host '[*] Building installer (Inno Setup)'
& $iscc "/DAppVersion=$version" /Qp desktop\cairn.iss
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
Write-Host "[+] Installer: dist\Cairn-Setup-$version.exe"
