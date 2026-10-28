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

  -Staging ("Through this PC", same as MtrDbMigration): for a login that may only
  create tables in schema public (no CREATE on the database, no postgres_fdw).
  The scripts come from sql_staging\ (generate.py --staging): _merge becomes
  tables public._merge_*, and 'attach' creates staging tables
  public."src_<source>__<table>" and copies every source table into them through
  this computer (generate.py --staging --copy). 'detach' drops the staging tables.
  -UseExistingTarget builds into an existing EMPTY database (e.g. one the DBA
  created) instead of CREATE DATABASE.

  Target login password: set $env:PGPASSWORD before running (or use a .pgpass).
  -Kerberos logs in to the target as the signed-in Windows (domain) user instead
  (GSSAPI/SSPI, no password; -PgHost must be the server's full DNS name, -User
  the PostgreSQL role the principal maps to, -KrbSrvName the service, default postgres).
  -Role runs every target session as SET ROLE <role> (e.g. a group such as
  cwp_mig_prd), so everything is created as that role. Source logins don't use it.
  Source login: -SrcUser/-SrcPassword. With -Staging and no -SrcPassword the
  sources are read with Kerberos too. In direct mode -SrcNoPassword makes the
  postgres_fdw user mappings password-less (superuser only).

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

.EXAMPLE
  # only CREATE on schema public of an existing, empty cwp2: through this PC
  .\run_consolidation.ps1 -Staging -UseExistingTarget -TargetDb cwp2 -PgHost db01 -User mig_login -SrcUser mig_login

.EXAMPLE
  # Kerberos login, working as group role cwp_mig_prd, through this PC
  .\run_consolidation.ps1 -Kerberos -Role cwp_mig_prd -Staging -UseExistingTarget -TargetDb cwp2 `
      -PgHost HQSCWPD001v.corp.mtrc.com -User ssishadm_prj1 -SrcUser ssishadm_prj1
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
    [switch]   $SrcNoPassword,
    [switch]   $Kerberos,
    [string]   $KrbSrvName = 'postgres',
    [string]   $Role,
    [string[]] $Steps = @('generate', 'create', 'schema', 'attach', 'preflight', 'merge', 'finalize', 'verify'),
    [switch]   $DropExistingTarget,
    [switch]   $UseExistingTarget,
    [switch]   $Staging,
    [switch]   $Yes
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$sqlDir = Join-Path $here $(if ($Staging) { 'sql_staging' } else { 'sql' })
$logDir = Join-Path $here 'logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'

$cfg = Get-Content (Join-Path $here 'sources.json') -Raw | ConvertFrom-Json
if (-not $TargetDb) { $TargetDb = $cfg.target_dbname }
if ($TargetDb -notmatch '^[A-Za-z0-9_]+$') { throw "Target database name must be [A-Za-z0-9_]: $TargetDb" }
# accept "cwp_mig_prd" as well as a pasted "SET ROLE cwp_mig_prd;"
$Role = ($Role -replace '^\s*set\s+role\s+', '' -replace ';\s*$', '').Trim()
if ($Role -and $Role -notmatch '^[A-Za-z0-9_$]+$') { throw "-Role: enter just the role name, e.g. cwp_mig_prd" }
# psql must not wait for a password prompt when Kerberos is meant to log in
$noPrompt = $(if ($Kerberos) { @('-w') } else { @() })

function Invoke-Psql {
    param([string] $Database, [string] $File, [string] $Command, [string] $LogName, [string[]] $Vars = @(), [string[]] $Extra = @())
    $psqlArgs = @('-X') + $noPrompt + @('-v', 'ON_ERROR_STOP=1', '-U', $User, '-d', $Database)
    foreach ($v in $Vars) { $psqlArgs += @('-v', $v) }
    $psqlArgs += $Extra
    if ($Command) { $psqlArgs += @('-c', $Command) }
    $log = Join-Path $logDir "${stamp}_$LogName.log"

    if ($DockerContainer) {
        if ($File) { $psqlArgs += @('-f', '-') }
        $cmd = { if ($File) { Get-Content -Raw -Encoding utf8 $File | docker exec -i @dockerEnv $DockerContainer psql @psqlArgs }
                 else { docker exec -i @dockerEnv $DockerContainer psql @psqlArgs } }
    } else {
        $psqlArgs = @('-h', $PgHost, '-p', $Port) + $psqlArgs
        if ($File) { $psqlArgs += @('-f', $File) }
        $cmd = { & $PsqlPath @psqlArgs }
    }
    $prevEnc = $OutputEncoding; $OutputEncoding = [System.Text.Encoding]::UTF8
    # Windows PowerShell 5.1 turns every stderr line (even a NOTICE) into an error that
    # 'Stop' would throw on; success is decided by psql's exit code below instead.
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $cmd 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $log | Out-Host }
    finally { $OutputEncoding = $prevEnc; $ErrorActionPreference = $prevEap }
    if ($LASTEXITCODE -ne 0) { throw "psql failed ($LogName) - see $log" }
}

function Get-PsqlScalar([string] $Database, [string] $Sql) {
    $a = @('-X') + $noPrompt + @('-tA', '-U', $User, '-d', $Database, '-c', $Sql)
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'  # see Invoke-Psql
    try {
        if ($DockerContainer) { $out = docker exec -i @dockerEnv $DockerContainer psql @a 2>&1 }
        else { $out = & $PsqlPath (@('-h', $PgHost, '-p', $Port) + $a) 2>&1 }
    } finally { $ErrorActionPreference = $prevEap }
    $err = @($out | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" })
    $r = @($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $($err -join ' ')`n  ($Sql)" }
    return "$r".Trim()
}

$srcVars = @("src_user=$SrcUser", "src_password=$SrcPassword", "src_kerberos=$(([bool]$SrcNoPassword).ToString().ToLower())")
# generate.py: sources without a "dump" in sources.json are read live; -Staging 'attach' copies with it too.
$env:MTR_PSQL = $PsqlPath; $env:MTR_SRC_USER = $SrcUser; $env:MTR_SRC_PASSWORD = $SrcPassword
$env:MTR_SCHEMA_HOST = $PgHost; $env:MTR_SCHEMA_PORT = "$Port"; $env:MTR_DOCKER = $DockerContainer
$env:MTR_TARGET_HOST = $PgHost; $env:MTR_TARGET_PORT = "$Port"; $env:MTR_TARGET_USER = $User; $env:MTR_TARGET_DB = $TargetDb
$genArgs = @('-I', (Join-Path $here 'generate.py')) + $(if ($Staging) { @('--staging') } else { @() })

# Target login for this run (restored at the end): Kerberos = no password, -Role = SET ROLE
# at the start of every target session. generate.py keeps both away from the source logins.
$savedEnv = @{}
foreach ($n in 'PGPASSWORD', 'PGOPTIONS', 'PGKRBSRVNAME') { $savedEnv[$n] = [Environment]::GetEnvironmentVariable($n) }
if ($Kerberos) { $env:PGPASSWORD = $null; $env:PGKRBSRVNAME = $KrbSrvName }
$env:PGOPTIONS = $(if ($Role) { "-c role=$Role" } else { $null })
$dockerEnv = $(if ($Role) { @('-e', "PGOPTIONS=-c role=$Role") } else { @() })

try {
foreach ($step in $Steps) {
    Write-Host "`n==== $step ====" -ForegroundColor Cyan
    switch ($step) {
        'generate' {
            & python @genArgs
            if ($LASTEXITCODE -ne 0) { throw 'generate.py failed' }
        }
        'create' {
            $exists = Get-PsqlScalar 'postgres' "SELECT 1 FROM pg_database WHERE datname = '$TargetDb'"
            if ($exists -eq '1' -and $UseExistingTarget -and -not $DropExistingTarget) {
                # Build into it only when nothing user-made is in it (the schema step must start clean).
                $used = Get-PsqlScalar $TargetDb ("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace " +
                    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%' " +
                    "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')")
                if ($used -ne '0') { throw "Database $TargetDb is not empty ($used tables/views/sequences); empty it or use another -TargetDb." }
                Write-Host "Database $TargetDb exists and is empty: using it (not created, not dropped)."
            } else {
                if ($exists -eq '1') {
                    if (-not $DropExistingTarget) {
                        throw "Database $TargetDb already exists. Re-run with -DropExistingTarget to rebuild it, or -UseExistingTarget if it is empty."
                    }
                    # Only drop a database this script built (it has _merge, either layout), never a source.
                    if ((Get-PsqlScalar $TargetDb "SELECT to_regclass('_merge.source') IS NOT NULL OR to_regclass('public._merge_source') IS NOT NULL") -ne 't') {
                        throw "Database $TargetDb was not built by this script (no _merge tables); refusing to drop it."
                    }
                    Invoke-Psql -Database 'postgres' -Command "DROP DATABASE `"$TargetDb`" WITH (FORCE)" -LogName 'drop'
                }
                Invoke-Psql -Database 'postgres' -Command "CREATE DATABASE `"$TargetDb`" ENCODING 'UTF8' TEMPLATE template0" -LogName 'create'
            }
        }
        'schema'    { Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '00_target_schema.sql') -LogName 'schema' -Extra '--single-transaction' }
        'attach'    {
            if ($Staging) {
                # staging tables, then every source table copied into them through this computer
                Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '01_attach_sources.sql') -LogName 'stage' -Extra '-q', '--single-transaction'
                $copyLog = Join-Path $logDir "${stamp}_stage_copy.log"
                $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'  # see Invoke-Psql
                try { & python @genArgs --copy 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $copyLog | Out-Host }
                finally { $ErrorActionPreference = $prevEap }
                if ($LASTEXITCODE -ne 0) { throw "copy to staging failed - see $copyLog" }
            } else {
                Invoke-Psql -Database $TargetDb -File (Join-Path $sqlDir '01_attach_sources.sql') -LogName 'attach' -Vars $srcVars
            }
        }
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
} finally {
    foreach ($n in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($n, $savedEnv[$n]) }
}
Write-Host "`nDone. Logs: $logDir" -ForegroundColor Green
