<#
.SYNOPSIS
  Build dist\MetadataHorizontal.exe from the PYTHON app (mh_app.py) with PyInstaller
  (no Python needed on the target computer). The exe keeps config.json and
  logs\ next to itself; the first run writes the bundled default config.json.
  The PowerShell app (with Export from SharePoint) is built by build_exe.ps1;
  both write the same exe name, so the last build wins.

.EXAMPLE
  .\build_exe_python.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

& python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name MetadataHorizontal `
    --distpath (Join-Path $here 'dist') --workpath (Join-Path $here 'build\pyi') --specpath (Join-Path $here 'build') `
    --paths $here `
    --collect-data customtkinter `
    --hidden-import psycopg2 `
    --add-data "$(Join-Path $here 'config.json');." `
    (Join-Path $here 'mh_app.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
Write-Host "`nBuilt: $(Join-Path $here 'dist\MetadataHorizontal.exe')" -ForegroundColor Green
