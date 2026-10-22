<#
.SYNOPSIS
  Build the consolidated MTR Phase 3 database from the source databases.

.DESCRIPTION
  Steps (run in order; pick a subset with -Steps):
    generate   python generate.py           (re-create sql/01,02,03,05,06 from sources.json)
    create     CREATE DATABASE <target>     (refuses if it exists unless -DropExistingTarget)
    schema     sql/00_target_schema.sql
    attach     sql/01_attach_sources.sql    (postgres_fdw link to every source)
    preflight  sql/02_preflight.sql         (read-only report -> logs/, then asks to continue)
    merge      sql/03_merge.sql             (one transaction)
    finalize   sql/04_finalize.sql          (indexes, FKs, view)
               sql/04b_sequences.sql        (sequences, ANALYZE)
    verify     sql/05_verify.sql            (fails if any row is unaccounted for)
    detach     sql/06_detach_sources.sql    (drop the fdw links; keeps _merge audit schema)

  The target server must be able to reach every source server (postgres_fdw).
  When all DBs live on one server (e.g. the mtr-postgres container) keep
  host=localhost in sources.json.

  Target login password: set $env:PGPASSWORD before running (or use a .pgpass).

.EXAMPLE
  # psql on PATH, sources and target on the same server
  $env:PGPASSWORD = '...'
  .\run_consolidation.ps1 -PgHost db01 -SrcUser postgres -SrcPassword '...'

.EXAMPLE
  # everything inside the docker container from mtr-phase3/docker-compose.yaml
  .\run_consolidation.ps1 -DockerContainer mtr-postgres -SrcUser postgres -SrcPassword '...'

.EXAMPLE
  # only the read-only preflight report
  .\run_consolidation.ps1 -Steps create,schema,attach,preflight -SrcUser postgres -SrcPassword '...'
#>
[CmdletBinding()]
param(
    [string]   $PsqlPath = 'psql',
    [string]   $DockerContainer,
    [string]   $PgHost = 'localhost',
    [int]      $Port = 5432,
    [string]   $User = 'postgres',
    [string]   $TargetDb,
    [string]   $SrcUser = 'postgres',
    [string]   $SrcPassword = '',
    [string[]] $Steps = @('generate', 'create', 'schema', 'attach', 'preflight', 'merge', 'finalize', 'verify'),
    [switch]   $DropExistingTarget,
    [switch]   $Yes
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$sqlDir = Join-Path $here 'sql'
$logDir = Join-Path $here 'logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'

$cfg = Get-Content (Join-Path $here 'sources.json') -Raw | ConvertFrom-Json
if (-not $TargetDb) { $TargetDb = $cfg.target_dbname }
if ($TargetDb -notmatch '^[A-Za-z0-9_]+$') { throw "Target database name must be [A-Za-z0-9_]: $TargetDb" }

function Invoke-Psql {
    param([string] $Database, [string] $File, [string] $Command, [string] $LogName, [string[]] $Vars = @(), [string[]] $Extra = @())
    $psqlArgs = @('-X', '-v', 'ON_ERROR_STOP=1', '-U', $User, '-d', $Database)
    foreach ($v in $Vars) { $psqlArgs += @('-v', $v) }
    $psqlArgs += $Extra
    if ($Command) { $psqlArgs += @('-c', $Command) }
    $log = Join-Path $logDir "${stamp}_$LogName.log"

    if ($DockerContainer) {
        if ($File) { $psqlArgs += @('-f', '-') }
        $cmd = { if ($File) { Get-Content -Raw -Encoding utf8 $File | docker exec -i $DockerContainer psql @psqlArgs }
                 else { docker exec -i $DockerContainer psql @psqlArgs } }
    } else {
        $psqlArgs = @('-h', $PgHost, '-p', $Port) + $psqlArgs
        if ($File) { $psqlArgs += @('-f', $File) }
        $cmd = { & $PsqlPath @psqlArgs }
    }
    $prevEnc = $OutputEncoding; $OutputEncoding = [System.Text.Encoding]::UTF8
    try { & $cmd 2>&1 | Tee-Object -FilePath $log | Out-Host }
    finally { $OutputEncoding = $prevEnc }
    if ($LASTEXITCODE -ne 0) { throw "psql failed ($LogName) - see $log" }
}

function Get-PsqlScalar([string] $Database, [string] $Sql) {
    $a = @('-X', '-tA', '-U', $User, '-d', $Database, '-c', $Sql)
    if ($DockerContainer) { $r = docker exec -i $DockerContainer psql @a }
    else { $r = & $PsqlPath (@('-h', $PgHost, '-p', $Port) + $a) }
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $Sql" }
    return "$r".Trim()
}

$srcVars = @("src_user=$SrcUser", "src_password=$SrcPassword")

foreach ($step in $Steps) {
    Write-Host "`n==== $step ====" -ForegroundColor Cyan
    switch ($step) {
        'generate' {
            # For sources without a "dump" in sources.json the schema is read live.
            $env:MTR_PSQL = $PsqlPath; $env:MTR_SRC_USER = $SrcUser; $env:MTR_SRC_PASSWORD = $SrcPassword
            $env:MTR_SCHEMA_HOST = $PgHost; $env:MTR_SCHEMA_PORT = "$Port"; $env:MTR_DOCKER = $DockerContainer
            & python -I (Join-Path $here 'generate.py')
            if ($LASTEXITCODE -ne 0) { throw 'generate.py failed' }
        }
        'create' {
            $exists = Get-PsqlScalar 'postgres' "SELECT 1 FROM pg_database WHERE datname = '$TargetDb'"
            if ($exists -eq '1') {
                if (-not $DropExistingTarget) { throw "Database $TargetDb already exists. Re-run with -DropExistingTarget to rebuild it." }
                # Only drop a database this script built (00_target_schema.sql creates _merge), never a source.
                if ((Get-PsqlScalar $TargetDb "SELECT 1 FROM pg_namespace WHERE nspname = '_merge'") -ne '1') {
                    throw "Database $TargetDb was not built by this script (no _merge schema); refusing to drop it."
                }
                Invoke-Psql -Database 'postgres' -Command "DROP DATABASE `"$TargetDb`" WITH (FORCE)" -LogName 'drop'
            }
            Invoke-Psql -Database 'postgres' -Command "CREATE DATABASE `"$TargetDb`" ENCODING 'UTF8' TEMPLATE template0" -LogName 'create'
        }
        'schema'    { Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '00_target_schema.sql') -LogName 'schema' -Extra '--single-transaction' }
        'attach'    { Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '01_attach_sources.sql') -LogName 'attach' -Vars $srcVars }
        'preflight' {
            Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '02_preflight.sql') -LogName 'preflight'
            if (-not $Yes -and $Steps -contains 'merge') {
                $answer = Read-Host "`nReview the preflight report above (also in logs\). Continue with the merge? [y/N]"
                if ($answer -notmatch '^[yY]') { Write-Host 'Stopped before merge.'; return }
            }
        }
        'merge'     { Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '03_merge.sql') -LogName 'merge' }
        'finalize'  {
            Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '04_finalize.sql') -LogName 'finalize'
            Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '04b_sequences.sql') -LogName 'sequences'
        }
        'verify'    { Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '05_verify.sql') -LogName 'verify' }
        'detach'    { Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '06_detach_sources.sql') -LogName 'detach' }
        default     { throw "Unknown step: $step" }
    }
}
Write-Host "`nDone. Logs: $logDir" -ForegroundColor Green
