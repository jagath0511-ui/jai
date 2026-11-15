# scripts/build_standalone.ps1 - Builds standalone Windows distribution of JAI Browser.
# Non-Python users can run dist/jai-browser/jai.exe out of the box with zero external dependencies.

param (
    [switch]$SkipCompile = $false,
    [switch]$TestAfterBuild = $true
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " 🚀 Building JAI Browser Standalone Windows Bundle" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "Repository root: $RepoRoot"

# 1. Ensure C Sandbox DLL is compiled and available
Write-Host "`n[Step 1/4] Checking Native C Sandbox Library..." -ForegroundColor Yellow
$SecurityDll = "$RepoRoot\src\security\jai_sandbox.dll"
$NativeDll = "$RepoRoot\native\jai_sandbox.dll"

if (-not $SkipCompile -and (Test-Path "$RepoRoot\native\build.ps1")) {
    try {
        Write-Host "Compiling fresh native/jai_sandbox.c..."
        & "$RepoRoot\native\build.ps1" -OutputDll "$SecurityDll"
        Copy-Item "$SecurityDll" "$NativeDll" -Force
        Write-Host "✅ Native sandbox DLL compiled successfully." -ForegroundColor Green
    } catch {
        Write-Warning "C compiler not available; falling back to existing pre-compiled DLL: $_"
    }
}

if (-not (Test-Path "$SecurityDll")) {
    if (Test-Path "$NativeDll") {
        Copy-Item "$NativeDll" "$SecurityDll" -Force
    } else {
        throw "Neither src/security/jai_sandbox.dll nor native/jai_sandbox.dll exists. Compilation required."
    }
}
Write-Host "✅ Verified sandbox DLL at $SecurityDll" -ForegroundColor Green

# 2. Check PyInstaller availability
Write-Host "`n[Step 2/4] Verifying PyInstaller..." -ForegroundColor Yellow
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Host "PyInstaller not found on PATH. Installing via pip..."
    python -m pip install --upgrade pyinstaller
}
$pyinstallerVersion = python -m PyInstaller --version
Write-Host "✅ Using PyInstaller version: $pyinstallerVersion" -ForegroundColor Green

# 3. Configure environment and build paths
Write-Host "`n[Step 3/4] Configuring standalone environment & bundling..." -ForegroundColor Yellow
$env:PLAYWRIGHT_BROWSERS_PATH = "0"

$DistDir = "$RepoRoot\dist"
$FinalBundleDir = "$DistDir\jai-browser"
$BuildDir = "$DistDir\build"

# Clean previous build artifacts
if (Test-Path "$DistDir\jai") {
    Remove-Item -Recurse -Force "$DistDir\jai"
}
if (Test-Path "$FinalBundleDir") {
    Remove-Item -Recurse -Force "$FinalBundleDir"
}

Write-Host "Running PyInstaller bundling on src/cli.py..."
python -m PyInstaller `
    --noconfirm `
    --onedir `
    --name "jai" `
    --distpath "$DistDir" `
    --workpath "$BuildDir" `
    --specpath "$DistDir" `
    --paths "$RepoRoot" `
    --add-data "src/security/jai_sandbox.dll;src/security" `
    --add-data "native/jai_sandbox.dll;native" `
    --hidden-import "src" `
    --hidden-import "src.cli" `
    --hidden-import "src.browser" `
    --hidden-import "src.browser.cdp" `
    --hidden-import "src.browser.actions" `
    --hidden-import "src.browser.perception" `
    --hidden-import "src.browser.extraction" `
    --hidden-import "src.security" `
    --hidden-import "src.security.os_sandbox" `
    --hidden-import "src.agent" `
    --hidden-import "src.agent.loop" `
    --hidden-import "src.agent.browser_operator" `
    --hidden-import "src.action_gateway" `
    --hidden-import "src.llm_router" `
    --hidden-import "playwright" `
    --hidden-import "playwright.sync_api" `
    --hidden-import "pydantic" `
    --hidden-import "orjson" `
    --hidden-import "dotenv" `
    "$RepoRoot\src\cli.py"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

# Rename output directory to dist/jai-browser
if (Test-Path "$DistDir\jai") {
    Move-Item "$DistDir\jai" "$FinalBundleDir" -Force
}

$ExePath = "$FinalBundleDir\jai.exe"
if (-not (Test-Path $ExePath)) {
    throw "Expected binary was not created at $ExePath"
}

# Create a convenient run_jai.bat launcher in the bundle
$BatchLauncher = @"
@echo off
set PLAYWRIGHT_BROWSERS_PATH=0
"%~dp0jai.exe" %*
"@
Set-Content -Path "$FinalBundleDir\run_jai.bat" -Value $BatchLauncher -Encoding ASCII

Write-Host "✅ Standalone bundle successfully created at: $FinalBundleDir" -ForegroundColor Green

# 4. Optional verification check
if ($TestAfterBuild) {
    Write-Host "`n[Step 4/4] Validating bundle execution (jai.exe --check)..." -ForegroundColor Yellow
    & "$ExePath" --check
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ Standalone verification passed!" -ForegroundColor Green
    } else {
        Write-Warning "Standalone verification returned non-zero exit code: $LASTEXITCODE"
    }
}

Write-Host "`n=================================================================" -ForegroundColor Cyan
Write-Host " 🎉 Build Complete! Standalone Windows Bundle is Ready:" -ForegroundColor Cyan
Write-Host " 📁 Bundle Location: $FinalBundleDir" -ForegroundColor White
Write-Host " 🚀 Run command:     .\dist\jai-browser\jai.exe --help" -ForegroundColor White
Write-Host "=================================================================`n" -ForegroundColor Cyan
