<#
.SYNOPSIS
  Build dist\MtrDbMigration.exe: one file with the migration GUI, generate.py,
  sql\ and a private psql.exe (no install needed on the target computer).
  Source schemas are read live, so the pg_dump files are not bundled.

.EXAMPLE
  .\build_exe.ps1
  .\build_exe.ps1 -PgBin 'D:\PostgreSQL\18\bin'
#>
[CmdletBinding()]
param([string] $PgBin = 'C:\Program Files\PostgreSQL\18\bin')

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$stage = Join-Path $here 'build\pgbin'

# psql.exe and the DLLs it loads (checked by running it with only these files).
$pgFiles = 'psql.exe', 'libpq.dll', 'libssl-3-x64.dll', 'libcrypto-3-x64.dll',
           'libintl-9.dll', 'libiconv-2.dll', 'zlib1.dll', 'libwinpthread-1.dll'
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
foreach ($f in $pgFiles) { Copy-Item (Join-Path $PgBin $f) $stage }
# MSVC runtime, for computers without the VC++ redistributable.
foreach ($f in 'vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll') {
    $p = Join-Path $env:WINDIR "System32\$f"
    if (Test-Path $p) { Copy-Item $p $stage }
}

& python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name MtrDbMigration `
    --distpath (Join-Path $here 'dist') --workpath (Join-Path $here 'build\pyi') --specpath (Join-Path $here 'build') `
    --paths $here --paths (Join-Path $here '..\metadata_horizontal') `
    --add-data "$(Join-Path $here 'sql');consolidation\sql" `
    --add-data "$(Join-Path $here 'sources.json');consolidation" `
    --add-data "$stage;pgbin" `
    (Join-Path $here 'migrate_app.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
Write-Host "`nBuilt: $(Join-Path $here 'dist\MtrDbMigration.exe')" -ForegroundColor Green
