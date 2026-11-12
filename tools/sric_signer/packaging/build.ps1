# Builds the SRIC Invoice Signer for Windows: tests, one-file exe (PyInstaller), the exe's self-test, and the installer
# (Inno Setup; NSIS is not used). Run from any folder:
#   powershell -ExecutionPolicy Bypass -File tools\sric_signer\packaging\build.ps1 [-Python python] [-SkipInstaller]
param(
    [string]$Python = "python",
    [switch]$SkipInstaller
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& $Python -m pip install --disable-pip-version-check -q -r requirements.txt -r packaging\build-requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

& $Python -m pytest tests -q -p no:cacheprovider -rs
if ($LASTEXITCODE -ne 0) { throw "tests failed" }

$version = (& $Python packaging\gen_version_info.py).Trim()
& $Python -m PyInstaller --noconfirm --clean packaging\sric_signer.spec --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $root "dist\SRIC Invoice Signer.exe"
$report = Join-Path $root "dist\self-test.txt"
$p = Start-Process -FilePath $exe -ArgumentList "--self-test", "`"$report`"" -Wait -PassThru
Get-Content $report
if ($p.ExitCode -ne 0) { throw "the exe's self-test failed (exit $($p.ExitCode))" }

if (-not $SkipInstaller) {
    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found; install it or pass -SkipInstaller" }
    & $iscc "/DAppVersion=$version" "/DExeDir=$root\dist" "/DOutDir=$root\dist" "packaging\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
}
Get-ChildItem dist | Format-Table Name, Length
