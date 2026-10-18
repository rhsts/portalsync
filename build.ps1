param([string]$WorkDirectory = (Join-Path $PSScriptRoot 'build'))
$ErrorActionPreference = 'Stop'

# Requires Python, requirements.txt, PyInstaller, and the Visual Studio x64 C++ toolchain.
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$visualStudio = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $visualStudio) { throw 'Visual Studio C++ x64 build tools were not found.' }
$vcvars = Join-Path $visualStudio 'VC/Auxiliary/Build/vcvars64.bat'
New-Item -ItemType Directory -Force -Path $WorkDirectory | Out-Null
$WorkDirectory = (Resolve-Path -LiteralPath $WorkDirectory).Path
$compile = Join-Path $WorkDirectory 'compile-portal-hide.cmd'
@"
@echo off
call "$vcvars"
if errorlevel 1 exit /b 1
cl /nologo /O2 /LD "$PSScriptRoot\portal_hide.c" cfgmgr32.lib /link /OUT:"$PSScriptRoot\portal_hide.dll"
exit /b %errorlevel%
"@ | Set-Content -LiteralPath $compile -Encoding Ascii
Push-Location $WorkDirectory
try {
    & $env:ComSpec /d /c $compile
    if ($LASTEXITCODE -ne 0) { throw 'portal_hide.dll compilation failed.' }
} finally { Pop-Location }

python -m PyInstaller --noconfirm --clean --onefile --windowed --name PortalSync `
    --icon "$PSScriptRoot/portal.ico" --collect-all libusb_package --hidden-import usb.backend.libusb1 `
    --distpath "$PSScriptRoot" --workpath "$WorkDirectory/pyinstaller" `
    --specpath "$WorkDirectory" "$PSScriptRoot/portalsync.py"
if ($LASTEXITCODE -ne 0) { throw 'PortalSync.exe build failed.' }
Write-Output "Built $PSScriptRoot\PortalSync.exe and portal_hide.dll"
