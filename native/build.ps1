# native/build.ps1 - Compiles jai_sandbox.c into jai_sandbox.dll with zero warnings

param (
    [string]$OutputDll = "$PSScriptRoot\jai_sandbox.dll"
)

$ErrorActionPreference = "Stop"

$SourceFile = "$PSScriptRoot\jai_sandbox.c"
if (-not (Test-Path $SourceFile)) {
    Write-Error "Source file '$SourceFile' not found."
}

Write-Host "🔨 [Build] Compiling $SourceFile -> $OutputDll..."

# 1. Search for available C compilers
$Compiler = $null
$CompilerType = $null

# Check PATH first
if (Get-Command gcc -ErrorAction SilentlyContinue) {
    $Compiler = (Get-Command gcc).Source
    $CompilerType = "gcc"
} elseif (Get-Command clang -ErrorAction SilentlyContinue) {
    $Compiler = (Get-Command clang).Source
    $CompilerType = "clang"
} elseif (Get-Command cl.exe -ErrorAction SilentlyContinue) {
    $Compiler = (Get-Command cl.exe).Source
    $CompilerType = "cl"
} elseif (Get-Command zig -ErrorAction SilentlyContinue) {
    $Compiler = (Get-Command zig).Source
    $CompilerType = "zig"
}

# If not found on PATH, search Program Files / MSYS2 / WinGet paths
if (-not $Compiler) {
    $SearchLocations = @(
        "C:\Program Files (x86)\Dev-Cpp\MinGW64\bin\gcc.exe",
        "C:\Program Files\Dev-Cpp\MinGW64\bin\gcc.exe",
        "C:\msys64\ucrt64\bin\gcc.exe",
        "C:\msys64\mingw64\bin\gcc.exe",
        "C:\Program Files\LLVM\bin\clang.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\*\zig.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\*\*\zig.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\zig.exe"
    )

    foreach ($loc in $SearchLocations) {
        $found = Get-Item $loc -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) {
            $Compiler = $found.FullName
            if ($Compiler -match "zig\.exe") { $CompilerType = "zig" }
            elseif ($Compiler -match "gcc\.exe") { $CompilerType = "gcc" }
            elseif ($Compiler -match "clang\.exe") { $CompilerType = "clang" }
            break
        }
    }
}

if (-not $Compiler) {
    Write-Error "No compatible C compiler found (gcc, clang, cl.exe, or zig). Please install gcc/MinGW, LLVM, or Zig."
}

Write-Host "⚙️ [Build] Using compiler: $Compiler ($CompilerType)"

# 2. Execute compilation based on compiler type
switch ($CompilerType) {
    "gcc" {
        & $Compiler -std=c99 -shared -O3 -Wall -Wextra -Werror -o $OutputDll $SourceFile -ladvapi32
    }
    "clang" {
        & $Compiler -std=c99 -shared -O3 -Wall -Wextra -Werror -o $OutputDll $SourceFile -ladvapi32
    }
    "zig" {
        & $Compiler cc -std=c99 -shared -O3 -Wall -Wextra -Werror -o $OutputDll $SourceFile -ladvapi32
    }
    "cl" {
        & $Compiler /nologo /O2 /W4 /WX /LD /Fe:$OutputDll $SourceFile advapi32.lib
    }
    default {
        Write-Error "Unsupported compiler type: $CompilerType"
    }
}

if ($LASTEXITCODE -ne 0) {
    Write-Error "Compilation failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path $OutputDll)) {
    Write-Error "Output DLL was not generated at '$OutputDll'."
}

$dllSize = (Get-Item $OutputDll).Length
Write-Host "✅ [Build] Successfully generated '$OutputDll' ($dllSize bytes) with zero warnings."
