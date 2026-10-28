<#
Horizontal metadata (PowerShell version of metadata_horizontal.py + metadata_matrix.py).

One row per file for a fixed, config-driven set of metadata keys, while every
other key stays in the vertical "Metadata" table (one row per key). Same
config.json, same tables and same rules as the Python version; the database is
reached through psql.exe instead of SQLAlchemy, Excel files are read with the
ImportExcel module (EPPlus) instead of openpyxl.

Upload (CSV or XLSX) - Import-MhFile:
    every row  ->  matched to Files by FileId (only if the row's other identities
                   agree), else UniqueId (SharePointUniqueId), else
                   OriginalSharePointPath, else FileRef (path part of
                   OriginalSharePointPath). Folder rows (FSObjType=1) and old
                   versions are skipped; unmatched rows go to a report CSV.
    configured keys  ->  1 row in the horizontal table (upsert by FileId)
    other keys       ->  vertical "Metadata" rows (non-empty values only)
    For every matched file the old Metadata rows of all keys present in the file
    (horizontal + vertical) are replaced, so a re-upload never duplicates.
    One transaction per upload: it either fully applies or not at all.

Existing data - Convert-MhExisting:
    pivots configured keys already in "Metadata" into the horizontal table and
    (optionally) deletes them from "Metadata".

Matrix - Update-MhMatrix:
    rebuilds "MetadataMatrix" (config "MetadataMatrix.TableName"): one row per
    file, one text column per distinct "Metadata"."MetadataKey". "Metadata" is
    only read, never changed.

psql: config.json "PsqlPath", else psql.exe on PATH, next to this file
(psql\ or bin\), or the newest C:\Program Files\PostgreSQL\<ver>\bin.
Every script goes to psql through stdin (no temp files), and every script that
changes data runs between BEGIN and COMMIT: if psql stops early the server
rolls the whole job back.

ImportExcel: installed module, or a copy in .\modules\ImportExcel next to this
file (for PCs without internet: Save-Module ImportExcel -Path .\modules).
#>
$ErrorActionPreference = 'Stop'

$script:BaseColumns  = @('MetadataHorizontalId', 'FileId', 'SiteName', 'SiteId', 'SourceFile', 'ImportedAt', 'UpdatedAt')
$script:IdColumns    = @('FileId', 'UniqueId', 'FileRef', 'OriginalSharePointPath', 'FSObjType', 'VersionLabel')
$script:DefaultTable = 'MetadataHorizontal'
$script:IdentRe      = '^[A-Za-z_][A-Za-z0-9_]{0,62}\z'
$script:Inv          = [Globalization.CultureInfo]::InvariantCulture
$script:EndMark      = '#MH-END-OF-SCRIPT'

$script:MatrixDefaultTable = 'MetadataMatrix'
$script:MatrixMaxKeys      = 1590                  # PostgreSQL allows 1600 columns per table
$script:MatrixBaseColumns  = @('FileId', 'SiteName', 'SiteId')
$script:MatrixMarker       = 'Metadata matrix:'    # start of the table comment; only such tables are replaced

# a portable ImportExcel copy next to the app (modules\ImportExcel) is found like an installed one
$script:ModulesDir = Join-Path $PSScriptRoot 'modules'
if ((Test-Path -LiteralPath $script:ModulesDir) -and ($env:PSModulePath -split ';') -notcontains $script:ModulesDir) {
    $env:PSModulePath = $script:ModulesDir + ';' + $env:PSModulePath
}
Add-Type -AssemblyName System.Web, Microsoft.VisualBasic


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

function q([string] $Ident) { '"' + $Ident.Replace('"', '""') + '"' }

function lit([string] $Text) { "'" + $Text.Replace("'", "''") + "'" }

function fmt($n) { ([long]$n).ToString('N0', $script:Inv) }

function secs([Diagnostics.Stopwatch] $Sw) { $Sw.Elapsed.TotalSeconds.ToString('0.0', $script:Inv) }

function jstr([string] $Text) { '"' + [System.Web.HttpUtility]::JavaScriptStringEncode($Text) + '"' }

function cut([string] $Text, [int] $Max) { if ($Text.Length -gt $Max) { $Text.Substring(0, $Max) } else { $Text } }

function Get-MhProp($Object, [string] $Name, $Default = $null) {
    if ($null -ne $Object -and $Object.PSObject.Properties[$Name] -and $null -ne $Object.$Name) { return $Object.$Name }
    return $Default
}

function New-OrdinalSet { New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal) }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

function Read-MhConfig([string] $Path) {
    [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) | ConvertFrom-Json
}

function Save-MhConnection([string] $Path, [Collections.IDictionary] $Values) {
    <# Merge $Values into the "Connection" object of config.json. Only that (flat)
       block is rewritten, so the rest of the file keeps its layout. #>
    $text = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
    $cfg = $text | ConvertFrom-Json
    $merged = [ordered]@{}
    $old = Get-MhProp $cfg 'Connection'
    if ($old) { foreach ($p in $old.PSObject.Properties) { $merged[$p.Name] = $p.Value } }
    foreach ($k in $Values.Keys) { $merged[$k] = $Values[$k] }
    $nl = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $lines = foreach ($k in $merged.Keys) { '    ' + (jstr $k) + ': ' + (ConvertTo-Json $merged[$k] -Compress) }
    $block = '"Connection": {' + $nl + ($lines -join (',' + $nl)) + $nl + '  }'
    $re = [regex] '"Connection"\s*:\s*\{[^{}]*\}'
    if ($re.IsMatch($text)) { $text = $re.Replace($text, $block.Replace('$', '$$'), 1) }
    else { $text = ([regex] '\{').Replace($text, '{' + $nl + '  ' + $block.Replace('$', '$$') + ',', 1) }
    [IO.File]::WriteAllText($Path, $text, (New-Object Text.UTF8Encoding($false)))
}

function Get-MhRoleName([string] $Raw) {
    <# Clean the "Role" setting: accepts "cwp_mig_prd" or a pasted "SET ROLE cwp_mig_prd;". #>
    $role = "$Raw".Trim().TrimEnd(';').Trim()
    if ($role.Length -ge 9 -and $role.Substring(0, 9).ToLowerInvariant() -eq 'set role ') { $role = $role.Substring(9).Trim() }
    if ($role -and $role -notmatch '^[\w$]+\z') {
        throw 'Role: enter just the role name, e.g. cwp_mig_prd (letters, digits, _ or $).'
    }
    return $role
}

function Get-MhSettings($AppConfig) {
    <# Validated view of config.json for horizontal metadata (HConfig in the Python version). #>
    $sec = Get-MhProp $AppConfig 'HorizontalMetadata'
    $s = @{
        Table       = [string](Get-MhProp $sec 'TableName' $script:DefaultTable)
        Keys        = New-Object 'Collections.Generic.List[string]'      # metadata key ...
        Cols        = New-Object 'Collections.Generic.List[string]'      # ... and its column, same index
        MaxLen      = [int](Get-MhProp $sec 'ColumnMaxLength' 4000)
        CurrentOnly = [bool](Get-MhProp $sec 'CurrentVersionOnly' $true)
        RemoveMoved = [bool](Get-MhProp $sec 'RemoveMovedKeysFromMetadata' $true)
        Skip        = New-OrdinalSet
        KeyByLower  = New-Object 'Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    }
    if (-not $s.Table) { $s.Table = $script:DefaultTable }
    if (-not $s.MaxLen) { $s.MaxLen = 4000 }
    $errs = New-Object 'Collections.Generic.List[string]'
    $columns = Get-MhProp $sec 'Columns'
    if ($columns) {
        foreach ($p in $columns.PSObject.Properties) {
            $s.Keys.Add($p.Name)
            if ($p.Value -is [string]) { $s.Cols.Add($p.Value) }
            else { $s.Cols.Add(''); $errs.Add("Column name '$($p.Value)' (key $($p.Name)) must be letters/digits/_ and at most 63 chars") }
        }
    }
    $skip = Get-MhProp $AppConfig 'SkipColumns'
    if ($null -eq $skip) { $skip = Get-MhProp (Get-MhProp $AppConfig 'Data') 'SkipColumns' @() }
    foreach ($k in $skip) { [void]$s.Skip.Add(([string]$k).ToLowerInvariant()) }

    if ($s.Table -notmatch $script:IdentRe) { $errs.Add("TableName '$($s.Table)' must be letters/digits/_ and at most 63 chars") }
    if ($s.Keys.Count -eq 0) { $errs.Add('HorizontalMetadata.Columns is empty') }
    if ($s.MaxLen -lt 1 -or $s.MaxLen -gt 10485760) { $errs.Add('ColumnMaxLength must be between 1 and 10485760') }
    $seen = @{}                                     # case-insensitive, like col.lower()
    for ($i = 0; $i -lt $s.Keys.Count; $i++) {
        $key = $s.Keys[$i]; $col = $s.Cols[$i]
        if (-not $key.Trim()) { $errs.Add('HorizontalMetadata.Columns has an empty key') }
        if (-not $col) { continue }
        if ($col -notmatch $script:IdentRe) { $errs.Add("Column name '$col' (key $key) must be letters/digits/_ and at most 63 chars") }
        elseif ($script:BaseColumns -ccontains $col) { $errs.Add("Column name '$col' (key $key) clashes with a built-in column") }
        elseif ($seen.ContainsKey($col)) { $errs.Add("Column name '$col' used by both $($seen[$col]) and $key") }
        else { $seen[$col] = $key }
    }
    foreach ($key in $s.Keys) {
        if ($s.KeyByLower.ContainsKey($key.ToLowerInvariant())) {
            $errs.Add('HorizontalMetadata.Columns has the same key twice (keys are case-insensitive)'); break
        }
        $s.KeyByLower[$key.ToLowerInvariant()] = $key
    }
    if ($errs.Count) { throw ("HorizontalMetadata config error:`n  " + ($errs -join "`n  ")) }
    return $s
}

function Get-MhMatrixTable($AppConfig) {
    $t = [string](Get-MhProp (Get-MhProp $AppConfig 'MetadataMatrix') 'TableName' $script:MatrixDefaultTable)
    if (-not $t) { $t = $script:MatrixDefaultTable }
    if ($t -notmatch $script:IdentRe) { throw "Matrix table name '$t' must be letters/digits/_ and at most 63 chars" }
    $protected = @('metadata', 'files', (Get-MhSettings $AppConfig).Table.ToLowerInvariant())
    if ($protected -contains $t.ToLowerInvariant()) { throw "Matrix table name '$t' would replace an existing data table" }
    return $t
}


# ---------------------------------------------------------------------------
# psql
# ---------------------------------------------------------------------------

function Find-MhPsql($AppConfig) {
    $configured = [string](Get-MhProp $AppConfig 'PsqlPath' '')
    if ($configured) {
        if (Test-Path -LiteralPath $configured -PathType Leaf) { return (Resolve-Path -LiteralPath $configured).Path }
        throw "PsqlPath in config.json not found: $configured"
    }
    $cmd = Get-Command psql.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Path }
    foreach ($p in (Join-Path $PSScriptRoot 'psql\psql.exe'), (Join-Path $PSScriptRoot 'bin\psql.exe')) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }) {
        $hit = Get-ChildItem -Path (Join-Path $root 'PostgreSQL\*\bin\psql.exe') -ErrorAction SilentlyContinue |
            Sort-Object { $v = 0.0; [void][double]::TryParse($_.Directory.Parent.Name, [Globalization.NumberStyles]::Float, $script:Inv, [ref]$v); $v } -Descending |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
        $hit = Get-ChildItem -Path (Join-Path $root 'pgAdmin 4\*runtime\psql.exe'), (Join-Path $root 'pgAdmin 4\*\runtime\psql.exe') -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    throw 'psql.exe not found. Install the PostgreSQL command line tools (or pgAdmin), or set "PsqlPath" in config.json.'
}

function New-MhConnection($AppConfig, [string] $Password) {
    <# Connection settings for Invoke-MhPsql, from config.json "Connection". #>
    $c = Get-MhProp $AppConfig 'Connection'
    @{
        Psql       = Find-MhPsql $AppConfig
        Host       = [string](Get-MhProp $c 'Host' 'localhost')
        Port       = [int](Get-MhProp $c 'Port' 5432)
        Database   = [string](Get-MhProp $c 'Database' 'postgres')
        User       = [string](Get-MhProp $c 'User' 'postgres')
        SslMode    = [string](Get-MhProp $c 'SslMode' '')
        Kerberos   = [bool](Get-MhProp $c 'Kerberos' $false)
        KrbSrvName = [string](Get-MhProp $c 'KrbSrvName' 'postgres')
        Role       = Get-MhRoleName (Get-MhProp $c 'Role' '')
        Password   = $Password
    }
}

function ConvertTo-MhArg([string] $Arg) {
    if ($Arg -and $Arg -notmatch '[\s"]') { return $Arg }
    '"' + ($Arg -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Get-MhPsqlError([string] $StdErr, [int] $ExitCode) {
    $lines = @($StdErr -split '\r?\n' | Where-Object { $_.Trim() } | ForEach-Object { $_ -replace '^psql:(<stdin>:\d+:)?\s*', '' })
    $main = @($lines | Where-Object { $_ -match '^(ERROR|FATAL|error|DETAIL):' })
    if (-not $main) { $main = $lines }
    if (-not $main) { return "psql failed (exit code $ExitCode)" }
    return (($main | Select-Object -First 3) -join "`n")
}

function Invoke-MhPsql {
    <# Run SQL through psql (script on stdin) and return its stdout (-tA) as a string.
       -Writer { param($w) ... } writes the script itself, e.g. to stream COPY data.
       If the writer fails, psql is killed before COMMIT, so nothing is applied. #>
    param([Parameter(Mandatory = $true)] [hashtable] $Conn, [string] $Sql, [scriptblock] $Writer)
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $Conn.Psql
    $a = @('-X', '-q', '-w', '-tA', '-v', 'ON_ERROR_STOP=1', '-h', $Conn.Host, '-p', [string]$Conn.Port,
           '-U', $Conn.User, '-d', $Conn.Database, '-f', '-')
    $psi.Arguments = ($a | ForEach-Object { ConvertTo-MhArg $_ }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
    $env = $psi.EnvironmentVariables
    foreach ($n in 'PGPASSWORD', 'PGOPTIONS', 'PGSSLMODE', 'PGKRBSRVNAME') { $env.Remove($n) }
    $env['PGCLIENTENCODING'] = 'UTF8'
    $env['PGAPPNAME'] = 'MetadataHorizontal'
    if (-not $env.ContainsKey('PGCONNECT_TIMEOUT')) { $env['PGCONNECT_TIMEOUT'] = '30' }
    if ($Conn.Kerberos) { $env['PGKRBSRVNAME'] = $(if ($Conn.KrbSrvName) { $Conn.KrbSrvName } else { 'postgres' }) }
    elseif ($Conn.Password) { $env['PGPASSWORD'] = $Conn.Password }
    if ($Conn.SslMode) { $env['PGSSLMODE'] = $Conn.SslMode }
    if ($Conn.Role) { $env['PGOPTIONS'] = "-c role=$($Conn.Role)" }   # every session starts as SET ROLE <role>

    $p = [Diagnostics.Process]::Start($psi)
    try {
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        $in = New-Object IO.StreamWriter($p.StandardInput.BaseStream, (New-Object Text.UTF8Encoding($false)), 65536)
        $in.NewLine = "`n"
        try {
            if ($Sql) { $in.Write($Sql); $in.Write("`n") }
            if ($Writer) { & $Writer $in }
            $in.Write("`n\echo $script:EndMark`n")
            $in.Close()
        } catch {
            $failure = $_
            try { if (-not $p.HasExited) { $p.Kill() } } catch { }
            $p.WaitForExit()
            # psql ended first (bad login, SQL error): its message is the real cause
            if ($failure.Exception -is [IO.IOException] -or $failure.Exception.InnerException -is [IO.IOException]) {
                throw (Get-MhPsqlError $errTask.Result $p.ExitCode)
            }
            throw $failure
        }
        $p.WaitForExit()
        $out = $outTask.Result
        if ($p.ExitCode -ne 0) { throw (Get-MhPsqlError $errTask.Result $p.ExitCode) }
        # psql reads stdin in text mode: a stray Ctrl-Z ends the input early, without an error
        $m = [regex]::Match($out, "(?m)^$([regex]::Escape($script:EndMark))\r?\n?\z")
        if (-not $m.Success) { throw 'psql did not read the whole script (input ended early); nothing was committed.' }
        return $out.Substring(0, $m.Index)
    } finally {
        $p.Dispose()
    }
}

function Get-MhLines([hashtable] $Conn, [string] $Sql) {
    @((Invoke-MhPsql -Conn $Conn -Sql $Sql) -split '\r?\n' | Where-Object { $_ -ne '' })
}

function Get-MhJson([hashtable] $Conn, [string] $Sql) {
    (Invoke-MhPsql -Conn $Conn -Sql $Sql) | ConvertFrom-Json
}

function Test-MhConnection([hashtable] $Conn) {
    <# -> "PostgreSQL 16.2 on ...  | login x, running as y" (+ a warning without "Files") #>
    $l = Get-MhLines $Conn @'
SELECT split_part(version(), ',', 1);
SELECT session_user || ', running as ' || current_user;
SELECT to_regclass('public."Files"') IS NOT NULL;
'@
    $msg = "$($l[0])  | login $($l[1])"
    if ($l[2] -ne 't') { $msg += '  - WARNING: no "Files" table in this database' }
    return $msg
}


# ---------------------------------------------------------------------------
# Table management
# ---------------------------------------------------------------------------

function Get-MhDdl([hashtable] $S, [switch] $WithSites) {
    $cols = for ($i = 0; $i -lt $S.Cols.Count; $i++) { "    $(q $S.Cols[$i]) varchar($($S.MaxLen))" }
    $siteId = if ($WithSites) { "    `"SiteId`" integer,`n" } else { '' }
    return @"
CREATE TABLE IF NOT EXISTS public.$(q $S.Table) (
    "MetadataHorizontalId" serial PRIMARY KEY,
    "FileId" integer NOT NULL UNIQUE REFERENCES public."Files"("FileId") ON DELETE CASCADE,
    "SiteName" varchar(256),
$siteId$($cols -join ",`n"),
    "SourceFile" varchar(1000),
    "ImportedAt" timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "UpdatedAt" timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"@
}

function Initialize-MhTable([hashtable] $Conn, [hashtable] $S, [scriptblock] $Log = { param($m) Write-Host $m }) {
    <# Create the table, or add columns that were added to the config since.
       Columns removed from the config are left in place (no data is dropped). #>
    $t = $S.Table
    $fk = $t + '_SiteId_fkey'
    $trg = 'trg_' + $t + '_site_id'
    $info = Get-MhJson $Conn @"
SELECT json_build_object(
    'sites', to_regclass('public."Sites"') IS NOT NULL AND EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'set_site_id'),
    'cols', COALESCE((SELECT json_agg(column_name::text) FROM information_schema.columns
                      WHERE table_schema = 'public' AND table_name = $(lit $t)), '[]'::json),
    'fk', EXISTS (SELECT 1 FROM pg_constraint WHERE conname = $(lit $fk)),
    'trg', EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = $(lit $trg)));
"@
    $sites = [bool]$info.sites
    $existing = New-OrdinalSet
    foreach ($c in @($info.cols)) { [void]$existing.Add([string]$c) }
    $sql = New-Object Text.StringBuilder
    [void]$sql.AppendLine('BEGIN;').AppendLine((Get-MhDdl $S -WithSites:$sites))
    $added = New-Object 'Collections.Generic.List[string]'
    if ($existing.Count) {                      # a new table already has every column
        foreach ($col in $S.Cols) {
            if (-not $existing.Contains($col)) {
                [void]$sql.AppendLine("ALTER TABLE public.$(q $t) ADD COLUMN IF NOT EXISTS $(q $col) varchar($($S.MaxLen));")
                $added.Add($col)
            }
        }
    }
    [void]$sql.AppendLine("CREATE INDEX IF NOT EXISTS $(q ('IX_' + $t + '_SiteName')) ON public.$(q $t) (`"SiteName`");")
    if ($sites) {
        # same SiteId convention as the consolidated DB: FK to "Sites", kept by trigger
        [void]$sql.AppendLine("ALTER TABLE public.$(q $t) ADD COLUMN IF NOT EXISTS `"SiteId`" integer;")
        [void]$sql.AppendLine("CREATE INDEX IF NOT EXISTS $(q ('IX_' + $t + '_SiteId')) ON public.$(q $t) (`"SiteId`");")
        if (-not $info.fk) {
            [void]$sql.AppendLine("ALTER TABLE public.$(q $t) ADD CONSTRAINT $(q $fk) FOREIGN KEY (`"SiteId`") REFERENCES public.`"Sites`"(`"SiteId`");")
        }
        if (-not $info.trg) {
            [void]$sql.AppendLine("CREATE TRIGGER $(q $trg) BEFORE INSERT OR UPDATE OF `"SiteName`" ON public.$(q $t) FOR EACH ROW EXECUTE FUNCTION public.set_site_id();")
        }
    }
    [void]$sql.AppendLine("COMMENT ON TABLE public.$(q $t) IS " + (lit ('One row per file: fixed metadata columns from config.json HorizontalMetadata.Columns. ' +
                          'All other metadata keys stay in "Metadata".')) + ';')
    for ($i = 0; $i -lt $S.Keys.Count; $i++) {
        [void]$sql.AppendLine("COMMENT ON COLUMN public.$(q $t).$(q $S.Cols[$i]) IS $(lit ('Metadata key: ' + $S.Keys[$i]));")
    }
    [void]$sql.AppendLine('COMMIT;')
    [void](Invoke-MhPsql -Conn $Conn -Sql $sql.ToString())

    $configured = New-OrdinalSet
    foreach ($c in $S.Cols) { [void]$configured.Add($c) }
    $unused = @($existing | Where-Object { $script:BaseColumns -cnotcontains $_ -and -not $configured.Contains($_) } | Sort-Object)
    & $Log ("Table $t ready ($($S.Cols.Count) metadata columns" +
            $(if ($added.Count) { ', added: ' + ($added -join ', ') } else { '' }) +
            $(if ($sites) { ', SiteId linked to Sites' } else { '' }) + ')')
    if ($unused) { & $Log ('Note: columns no longer in config (kept, not dropped): ' + ($unused -join ', ')) }
    return , $added.ToArray()
}


# ---------------------------------------------------------------------------
# Reading CSV / XLSX
# ---------------------------------------------------------------------------

function Open-MhReader([string] $Path, [string] $Sheet) {
    <# -> reader with .Headers; then Read-MhRow until $null, then Close-MhReader.
       Values are strings ('' for empty); empty rows are skipped. #>
    $ext = [IO.Path]::GetExtension($Path).ToLowerInvariant()
    # FileShare.ReadWrite: the file may still be open in Excel
    $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        if ($ext -eq '.xlsx' -or $ext -eq '.xlsm') {
            if (-not (Get-Module ImportExcel)) {
                if (-not (Get-Module -ListAvailable ImportExcel)) {
                    throw ('Excel files need the ImportExcel PowerShell module. Install it once with ' +
                           '"Install-Module ImportExcel -Scope CurrentUser", copy it to ' + $script:ModulesDir +
                           '\ImportExcel, or save the sheet as CSV UTF-8.')
                }
                Import-Module ImportExcel -ErrorAction Stop
            }
            $pkg = New-Object OfficeOpenXml.ExcelPackage($stream)
            $ws = if ($Sheet) { $pkg.Workbook.Worksheets[$Sheet] } else { $pkg.Workbook.Worksheets | Select-Object -First 1 }
            if (-not $ws) { throw "Sheet '$Sheet' not found in $([IO.Path]::GetFileName($Path))" }
            $dim = $ws.Dimension
            if (-not $dim) { throw "Sheet '$($ws.Name)' is empty" }
            $r = @{ Kind = 'xlsx'; Stream = $stream; Package = $pkg; Sheet = $ws.Name; Ws = $ws
                    Row = $dim.Start.Row; LastRow = $dim.End.Row; ColCount = $dim.End.Column
                    Block = $null; BlockRows = 0; BlockIdx = 0 }
            $first = Read-MhXlsxBlockRow $r -KeepEmpty
            $r.Headers = @($first | ForEach-Object { $_.Trim() })
            return $r
        }
        if ($ext -eq '.csv' -or $ext -eq '.txt') {
            $sr = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
            $parser = New-Object Microsoft.VisualBasic.FileIO.TextFieldParser($sr)
            $parser.TextFieldType = [Microsoft.VisualBasic.FileIO.FieldType]::Delimited
            $parser.SetDelimiters(',')
            $parser.HasFieldsEnclosedInQuotes = $true
            $parser.TrimWhiteSpace = $false
            if ($parser.EndOfData) { throw "$([IO.Path]::GetFileName($Path)) is empty" }
            $r = @{ Kind = 'csv'; Stream = $stream; Parser = $parser }
            $r.Headers = @($parser.ReadFields() | ForEach-Object { $_.Trim() })
            return $r
        }
        throw "Unsupported file type: $ext (use .csv or .xlsx)"
    } catch {
        $stream.Dispose()
        throw
    }
}

function Read-MhXlsxBlockRow([hashtable] $R, [switch] $KeepEmpty) {
    <# Next sheet row as string[]; cells are fetched 2000 rows at a time. #>
    while ($true) {
        if ($R.BlockIdx -ge $R.BlockRows) {
            if ($R.Row -gt $R.LastRow) { return $null }
            $end = [Math]::Min($R.Row + 1999, $R.LastRow)
            $v = $R.Ws.Cells[$R.Row, 1, $end, $R.ColCount].Value
            if ($v -isnot [Array]) { $one = New-Object 'object[,]' 1, 1; $one[0, 0] = $v; $v = $one }
            $R.Block = $v; $R.BlockRows = $end - $R.Row + 1; $R.BlockIdx = 0; $R.Row = $end + 1
        }
        $b = $R.Block; $i = $R.BlockIdx; $R.BlockIdx++
        $vals = New-Object 'string[]' $R.ColCount
        $any = $false
        for ($j = 0; $j -lt $R.ColCount; $j++) {
            $c = $b[$i, $j]
            if ($null -eq $c) { $vals[$j] = ''; continue }
            if ($c -is [string]) { $s = $c }
            elseif ($c -is [DateTime]) { $s = $c.ToString('yyyy-MM-dd HH:mm:ss', $script:Inv) }
            elseif ($c -is [double]) {
                $s = if ([Math]::Floor($c) -eq $c -and -not [double]::IsInfinity($c)) { $c.ToString('0', $script:Inv) }
                     else { $c.ToString('R', $script:Inv) }
            }
            else { $s = [Convert]::ToString($c, $script:Inv) }
            if ($s.IndexOf([char]0) -ge 0) { $s = $s.Replace([string][char]0, '') }   # PostgreSQL text cannot hold NUL
            $vals[$j] = $s
            if ($s) { $any = $true }
        }
        if ($any -or $KeepEmpty) { return , $vals }
    }
}

function Read-MhRow([hashtable] $R) {
    if ($R.Kind -eq 'xlsx') {
        $v = Read-MhXlsxBlockRow $R
        if ($null -eq $v) { return $null }
        return , $v
    }
    $p = $R.Parser
    while (-not $p.EndOfData) {
        $f = $p.ReadFields()
        if ($null -eq $f) { continue }
        $any = $false
        for ($j = 0; $j -lt $f.Length; $j++) {
            if ($f[$j].IndexOf([char]0) -ge 0) { $f[$j] = $f[$j].Replace([string][char]0, '') }
            if ($f[$j]) { $any = $true }
        }
        if ($any) { return , $f }
    }
    return $null
}

function Close-MhReader([hashtable] $R) {
    if ($R.Package) { $R.Package.Dispose() }
    if ($R.Parser) { $R.Parser.Dispose() }
    if ($R.Stream) { $R.Stream.Dispose() }
}

function Read-MhCsvText([string] $Text) {
    <# CSV text (COPY ... TO STDOUT output) -> list of string[]; first entry is the header. #>
    $rows = New-Object 'Collections.Generic.List[string[]]'
    if (-not $Text) { return , $rows }
    $parser = New-Object Microsoft.VisualBasic.FileIO.TextFieldParser((New-Object IO.StringReader($Text)))
    try {
        $parser.TextFieldType = [Microsoft.VisualBasic.FileIO.FieldType]::Delimited
        $parser.SetDelimiters(',')
        $parser.HasFieldsEnclosedInQuotes = $true
        $parser.TrimWhiteSpace = $false
        while (-not $parser.EndOfData) { $f = $parser.ReadFields(); if ($null -ne $f) { $rows.Add($f) } }
    } finally { $parser.Dispose() }
    return , $rows
}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

function Get-MhHeaderPlan([string[]] $Headers, [hashtable] $S) {
    <# -> Horiz {idx: key}, Vert {idx: header}, Skipped [header], Ids {name: idx} #>
    $plan = @{
        Horiz   = New-Object 'Collections.Generic.Dictionary[int,string]'
        Vert    = New-Object 'Collections.Generic.Dictionary[int,string]'
        Skipped = New-Object 'Collections.Generic.List[string]'
        Ids     = @{}
    }
    for ($i = 0; $i -lt $Headers.Count; $i++) {
        $h = $Headers[$i]
        if (-not $h) { continue }
        $lower = $h.ToLowerInvariant()
        $idName = $null
        foreach ($n in $script:IdColumns) { if ($n.ToLowerInvariant() -eq $lower) { $idName = $n } }
        if ($idName -and -not $plan.Ids.ContainsKey($idName)) { $plan.Ids[$idName] = $i }
        $key = $null
        if ($S.KeyByLower.TryGetValue($lower, [ref]$key)) { $plan.Horiz[$i] = $key }
        elseif ($S.Skip.Contains($lower) -or $idName) { $plan.Skipped.Add($h) }
        else { $plan.Vert[$i] = cut $h 256 }
    }
    return $plan
}

function Import-MhFile {
    <# Upload one CSV/XLSX. Returns a stats dictionary. #>
    param([hashtable] $Conn, [hashtable] $S, [string] $Path, [string] $Sheet, [scriptblock] $Log = { param($m) Write-Host $m },
          [scriptblock] $Progress, [string] $ReportDir)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $name = [IO.Path]::GetFileName($Path)
    $reader = Open-MhReader $Path $Sheet
    try {
        $headers = [string[]]$reader.Headers
        $plan = Get-MhHeaderPlan $headers $S
        $ids = $plan.Ids
        if (-not ($ids.ContainsKey('FileId') -or $ids.ContainsKey('UniqueId') -or $ids.ContainsKey('FileRef') -or
                  $ids.ContainsKey('OriginalSharePointPath'))) {
            throw ('The file needs at least one of these columns to find the file: ' +
                   'FileId, UniqueId, FileRef, OriginalSharePointPath')
        }
        & $Log ("$name$(if ($reader.Sheet) { " [$($reader.Sheet)]" }): $($headers.Count) columns -> $($plan.Horiz.Count) horizontal, " +
                "$($plan.Vert.Count) vertical, $($plan.Skipped.Count) skipped/identity")
        $inFile = New-OrdinalSet
        foreach ($k in $plan.Horiz.Values) { [void]$inFile.Add($k) }
        $missing = @($S.Keys | Where-Object { -not $inFile.Contains($_) } | Sort-Object)
        if ($missing) { & $Log ('Configured keys not in this file (left unchanged): ' + ($missing -join ', ')) }

        Initialize-MhTable $Conn $S -Log { param($m) } | Out-Null
        $stats = [ordered]@{ rows_read = 0; folders_skipped = 0; versions_skipped = 0; staged = 0; matched = 0
                             unmatched = 0; horizontal_rows = 0; vertical_rows = 0; vertical_deleted = 0; duplicates = 0 }

        $iFileId = if ($ids.ContainsKey('FileId')) { $ids['FileId'] } else { -1 }
        $iUid = if ($ids.ContainsKey('UniqueId')) { $ids['UniqueId'] } else { -1 }
        $iRef = if ($ids.ContainsKey('FileRef')) { $ids['FileRef'] } else { -1 }
        $iPath = if ($ids.ContainsKey('OriginalSharePointPath')) { $ids['OriginalSharePointPath'] } else { -1 }
        $iFs = if ($ids.ContainsKey('FSObjType')) { $ids['FSObjType'] } else { -1 }
        $iVl = if ($ids.ContainsKey('VersionLabel')) { $ids['VersionLabel'] } else { -1 }
        $hIdx = @($plan.Horiz.Keys); $hKeyJ = @($hIdx | ForEach-Object { jstr $plan.Horiz[$_] })
        $vIdx = @($plan.Vert.Keys);  $vKeyJ = @($vIdx | ForEach-Object { jstr $plan.Vert[$_] })
        $maxLen = $S.MaxLen
        $currentOnly = $S.CurrentOnly

        # ---- stage: rows stream into a temp table (COPY FROM STDIN) ----------
        $head = @'
BEGIN;
CREATE TEMP TABLE mh_stage (
    row_no      integer,
    file_id_in  text,
    unique_id   text,
    file_ref    text,
    orig_path   text,
    h           jsonb,
    v           jsonb,
    file_id     integer,
    matched_by  text
) ON COMMIT DROP;
-- FORCE_NOT_NULL: empty identity fields stay '' (not NULL) so the match rules work
COPY mh_stage (row_no, file_id_in, unique_id, file_ref, orig_path, h, v) FROM STDIN
    WITH (FORMAT csv, FORCE_NOT_NULL (file_id_in, unique_id, file_ref, orig_path));
'@
        $tail = Get-MhUploadSql $S $plan $headers $name

        $writer = {
            param($w)
            $w.Write($head); $w.Write("`n")
            $line = New-Object Text.StringBuilder
            while ($null -ne ($r = Read-MhRow $reader)) {
                $stats.rows_read++
                $n = $r.Length
                $fs = if ($iFs -ge 0 -and $iFs -lt $n) { $r[$iFs].Trim() } else { '' }
                if ($fs -eq '1') { $stats.folders_skipped++; continue }
                $vl = if ($iVl -ge 0 -and $iVl -lt $n) { $r[$iVl].Trim() } else { '' }
                if ($currentOnly -and $vl -and $vl.ToLowerInvariant() -ne 'current') {
                    if ($vl.ToLowerInvariant() -eq 'folder') { $stats.folders_skipped++ } else { $stats.versions_skipped++ }
                    continue
                }
                [void]$line.Clear()
                [void]$line.Append($stats.rows_read).Append(',"')
                foreach ($x in @($(if ($iFileId -ge 0 -and $iFileId -lt $n) { $r[$iFileId].Trim() } else { '' }),
                                 $(if ($iUid -ge 0 -and $iUid -lt $n) { $r[$iUid].Trim().Trim('{', '}').ToLowerInvariant() } else { '' }),
                                 $(if ($iRef -ge 0 -and $iRef -lt $n) { $r[$iRef].Trim() } else { '' }),
                                 $(if ($iPath -ge 0 -and $iPath -lt $n) { $r[$iPath].Trim() } else { '' }))) {
                    [void]$line.Append($x.Replace('"', '""')).Append('","')
                }
                # h: every configured key in the file ('' -> NULL later); v: non-empty values only
                $json = New-Object Text.StringBuilder '{'
                for ($k = 0; $k -lt $hIdx.Count; $k++) {
                    $i = $hIdx[$k]
                    $val = if ($i -lt $n) { cut $r[$i] $maxLen } else { '' }
                    if ($k) { [void]$json.Append(',') }
                    [void]$json.Append($hKeyJ[$k]).Append(':"').Append([System.Web.HttpUtility]::JavaScriptStringEncode($val)).Append('"')
                }
                [void]$line.Append($json.Append('}').ToString().Replace('"', '""')).Append('","')
                [void]$json.Clear().Append('{')
                $first = $true
                for ($k = 0; $k -lt $vIdx.Count; $k++) {
                    $i = $vIdx[$k]
                    if ($i -ge $n -or $r[$i] -eq '') { continue }
                    if (-not $first) { [void]$json.Append(',') }
                    $first = $false
                    [void]$json.Append($vKeyJ[$k]).Append(':"').Append([System.Web.HttpUtility]::JavaScriptStringEncode((cut $r[$i] 4000))).Append('"')
                }
                [void]$line.Append($json.Append('}').ToString().Replace('"', '""')).Append('"')
                $w.Write($line.ToString()); $w.Write("`n")
                $stats.staged++
                if ($Progress -and $stats.staged % 5000 -eq 0) { & $Progress $stats.rows_read }
            }
            $w.Write("\.`n")
            & $Log ("Staged $(fmt $stats.staged) rows (read $(fmt $stats.rows_read), folders $(fmt $stats.folders_skipped), " +
                    "old versions $(fmt $stats.versions_skipped))  [$(secs $sw)s]")
            if ($Progress) { & $Progress $stats.rows_read }
            $w.Write($tail)
        }
        $out = Invoke-MhPsql -Conn $Conn -Writer $writer
    } finally {
        Close-MhReader $reader
    }

    # ---- results ------------------------------------------------------------
    $m = [regex]::Match($out, '(?m)^#REPORT\r?\n')
    $report = if ($m.Success) { $out.Substring($m.Index + $m.Length) } else { '' }
    $by = [ordered]@{}
    foreach ($l in (($(if ($m.Success) { $out.Substring(0, $m.Index) } else { $out })) -split '\r?\n')) {
        $f = $l.Split('|')
        if ($f[0] -eq 'BY') {
            if ($f[1]) { $by[$f[1]] = [long]$f[2]; $stats.matched += [long]$f[2] } else { $stats.unmatched = [long]$f[2] }
        } elseif ($f[0] -eq 'STAT') { $stats[$f[1]] = [long]$f[2] }
    }
    & $Log ('Matched ' + (($by.Keys | ForEach-Object { "${_}: $(fmt $by[$_])" }) -join ', ') + "; unmatched: $(fmt $stats.unmatched)")
    if ($stats.duplicates) { & $Log "$(fmt $stats.duplicates) duplicate row(s) for the same file ignored (last row wins)" }

    if ($stats.unmatched) {
        if (-not $ReportDir) { $ReportDir = Join-Path $PSScriptRoot 'logs' }
        [void](New-Item -ItemType Directory -Force -Path $ReportDir)
        $rpt = Join-Path $ReportDir ("unmatched_$([IO.Path]::GetFileNameWithoutExtension($Path))_$(Get-Date -Format 'yyyyMMdd_HHmmss').csv")
        [IO.File]::WriteAllText($rpt, $report, (New-Object Text.UTF8Encoding($true)))
        $stats.unmatched_report = $rpt
        & $Log "Unmatched rows written to $rpt"
    }
    $stats.seconds = [Math]::Round($sw.Elapsed.TotalSeconds, 1)
    & $Log ("Done: $(fmt $stats.horizontal_rows) horizontal rows upserted, $(fmt $stats.vertical_rows) vertical " +
            "Metadata rows written ($(fmt $stats.vertical_deleted) old rows replaced)  [$(secs $sw)s]")
    return $stats
}

function Get-MhUploadSql([hashtable] $S, [hashtable] $Plan, [string[]] $Headers, [string] $SourceName) {
    <# SQL after the staged rows: match, upsert, replace vertical keys, report. Ends with COMMIT. #>
    $t = q $S.Table
    $sb = New-Object Text.StringBuilder
    # ---- match rows to Files -------------------------------------------------
    # FileId is trusted only when every other identity in the row agrees with
    # that file: FileIds were renumbered by the DB consolidation, so a FileId
    # from an older export may point at a different file.
    [void]$sb.AppendLine(@'
UPDATE mh_stage s SET file_id = f."FileId", matched_by = 'FileId'
FROM public."Files" f
WHERE s.file_id_in ~ '^[0-9]{1,10}$' AND f."FileId" = s.file_id_in::bigint
  AND (s.unique_id = '' OR f."SharePointUniqueId" IS NULL
       OR lower(trim(both '{}' from f."SharePointUniqueId")) = s.unique_id)
  AND (s.orig_path = '' OR f."OriginalSharePointPath" = s.orig_path)
  AND (s.file_ref = ''
       OR regexp_replace(f."OriginalSharePointPath", '^[A-Za-z]+://[^/]+', '') = s.file_ref);
UPDATE mh_stage s SET file_id = f."FileId", matched_by = 'UniqueId'
FROM public."Files" f
WHERE s.file_id IS NULL AND s.unique_id <> ''
  AND f."SharePointUniqueId" = s.unique_id;
UPDATE mh_stage s SET file_id = f."FileId", matched_by = 'UniqueId'
FROM public."Files" f
WHERE s.file_id IS NULL AND s.unique_id <> ''
  AND lower(trim(both '{}' from f."SharePointUniqueId")) = s.unique_id;
UPDATE mh_stage s SET file_id = f."FileId", matched_by = 'OriginalSharePointPath'
FROM public."Files" f
WHERE s.file_id IS NULL AND s.orig_path <> ''
  AND f."OriginalSharePointPath" = s.orig_path;
UPDATE mh_stage s SET file_id = f."FileId", matched_by = 'FileRef'
FROM public."Files" f
WHERE s.file_id IS NULL AND s.file_ref <> ''
  AND regexp_replace(f."OriginalSharePointPath", '^[A-Za-z]+://[^/]+', '') = s.file_ref;
SELECT 'BY|' || COALESCE(matched_by, '') || '|' || count(*) FROM mh_stage GROUP BY matched_by ORDER BY matched_by NULLS LAST;
-- duplicate rows for one file: the LAST row in the file wins
WITH d AS (DELETE FROM mh_stage s USING mh_stage s2
           WHERE s.file_id = s2.file_id AND s.row_no < s2.row_no RETURNING 1)
SELECT 'STAT|duplicates|' || count(*) FROM d;
'@)
    # ---- horizontal upsert -------------------------------------------------------
    $inFile = New-OrdinalSet
    foreach ($k in $Plan.Horiz.Values) { [void]$inFile.Add($k) }
    $present = @(for ($i = 0; $i -lt $S.Keys.Count; $i++) { if ($inFile.Contains($S.Keys[$i])) { $i } })
    if ($present.Count) {
        $cols =($present | ForEach-Object { q $S.Cols[$_] }) -join ', '
        $sel = ($present | ForEach-Object { "NULLIF(s.h->>$(lit $S.Keys[$_]), '')" }) -join ', '
        $upd = ($present | ForEach-Object { "$(q $S.Cols[$_]) = EXCLUDED.$(q $S.Cols[$_])" }) -join ', '
        [void]$sb.AppendLine(@"
WITH x AS (
    INSERT INTO public.$t ("FileId", "SiteName", $cols, "SourceFile")
    SELECT s.file_id, f."SiteName", $sel, $(lit (cut $SourceName 1000))
    FROM mh_stage s JOIN public."Files" f ON f."FileId" = s.file_id
    ON CONFLICT ("FileId") DO UPDATE SET $upd,
        "SiteName" = EXCLUDED."SiteName", "SourceFile" = EXCLUDED."SourceFile",
        "UpdatedAt" = CURRENT_TIMESTAMP
    RETURNING 1)
SELECT 'STAT|horizontal_rows|' || count(*) FROM x;
"@)
    }
    # ---- vertical: replace keys present in this file, insert the rest ---------
    # exact spellings: config keys, plus the file's own header text for them
    $keys = New-OrdinalSet
    foreach ($i in $Plan.Horiz.Keys) { [void]$keys.Add($Plan.Horiz[$i]); [void]$keys.Add($Headers[$i]) }
    foreach ($k in $Plan.Vert.Values) { [void]$keys.Add($k) }
    if ($keys.Count) {
        $arr = 'ARRAY[' + (($keys | Sort-Object | ForEach-Object { lit $_ }) -join ', ') + ']::text[]'
        [void]$sb.AppendLine(@"
WITH x AS (
    DELETE FROM public."Metadata" m USING mh_stage s
    WHERE m."FileId" = s.file_id AND m."MetadataKey" = ANY($arr)
    RETURNING 1)
SELECT 'STAT|vertical_deleted|' || count(*) FROM x;
"@)
    }
    if ($Plan.Vert.Count) {
        [void]$sb.AppendLine(@'
WITH x AS (
    INSERT INTO public."Metadata" ("FileId", "MetadataKey", "MetadataValue", "DataType", "SiteName")
    SELECT s.file_id, e.key, e.value, 'String', f."SiteName"
    FROM mh_stage s
    JOIN public."Files" f ON f."FileId" = s.file_id
    CROSS JOIN LATERAL jsonb_each_text(s.v) e
    WHERE e.value <> ''
    RETURNING 1)
SELECT 'STAT|vertical_rows|' || count(*) FROM x;
'@)
    }
    # ---- unmatched report (last output: everything after #REPORT is CSV) --------
    [void]$sb.AppendLine(@'
SELECT '#REPORT';
COPY (SELECT row_no AS "SourceRowNo", NULLIF(file_id_in, '') AS "FileId", NULLIF(unique_id, '') AS "UniqueId",
             NULLIF(file_ref, '') AS "FileRef",
             NULLIF(orig_path, '') AS "OriginalSharePointPath"
      FROM mh_stage WHERE file_id IS NULL ORDER BY row_no) TO STDOUT WITH (FORMAT csv, HEADER);
COMMIT;
'@)
    return $sb.ToString()
}


# ---------------------------------------------------------------------------
# Convert existing vertical Metadata
# ---------------------------------------------------------------------------

function Convert-MhExisting([hashtable] $Conn, [hashtable] $S, $RemoveFromVertical = $null, [scriptblock] $Log = { param($m) Write-Host $m }) {
    <# Pivot configured keys already in "Metadata" into the horizontal table.
       Existing horizontal values are only overwritten by non-empty vertical values. #>
    if ($null -eq $RemoveFromVertical) { $RemoveFromVertical = $S.RemoveMoved }
    [void](Initialize-MhTable $Conn $S -Log $Log)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $t = q $S.Table
    $cols = ($S.Cols | ForEach-Object { q $_ }) -join ', '
    # exact key match so IX_Metadata_MetadataKey is used on large Metadata tables
    $sel = ($S.Keys | ForEach-Object { "MAX(CASE WHEN m.`"MetadataKey`" = $(lit $_) THEN m.`"MetadataValue`" END)" }) -join ",`n           "
    $upd = ($S.Cols | ForEach-Object { "$(q $_) = COALESCE(EXCLUDED.$(q $_), t.$(q $_))" }) -join ', '
    $arr = 'ARRAY[' + (($S.Keys | ForEach-Object { lit $_ }) -join ', ') + ']::text[]'
    $sql = @"
BEGIN;
WITH x AS (
    INSERT INTO public.$t AS t ("FileId", "SiteName", $cols, "SourceFile")
    SELECT m."FileId", MAX(f."SiteName"),
           $sel,
           'Metadata (converted)'
    FROM public."Metadata" m JOIN public."Files" f ON f."FileId" = m."FileId"
    WHERE m."MetadataKey" = ANY($arr)
    GROUP BY m."FileId"
    ON CONFLICT ("FileId") DO UPDATE SET $upd, "UpdatedAt" = CURRENT_TIMESTAMP
    RETURNING 1)
SELECT count(*) FROM x;
"@
    if ($RemoveFromVertical) {
        $sql += "`nWITH x AS (DELETE FROM public.`"Metadata`" WHERE `"MetadataKey`" = ANY($arr) RETURNING 1) SELECT count(*) FROM x;"
    }
    $l = Get-MhLines $Conn ($sql + "`nCOMMIT;")
    $moved = [long]$l[0]
    $removed = if ($RemoveFromVertical) { [long]$l[1] } else { 0 }
    & $Log ("Converted $(fmt $moved) files into $($S.Table)" +
            $(if ($RemoveFromVertical) { "; removed $(fmt $removed) rows from Metadata" } else { '; Metadata left unchanged' }) +
            "  [$(secs $sw)s]")
    return [ordered]@{ files = $moved; removed = $removed }
}


# ---------------------------------------------------------------------------
# Matrix: every distinct MetadataKey as a column
# ---------------------------------------------------------------------------

function Limit-MhUtf8([string] $Text, [int] $MaxBytes) {
    <# Cut to at most $MaxBytes UTF-8 bytes without splitting a character. #>
    $b = [Text.Encoding]::UTF8.GetBytes($Text)
    if ($b.Length -le $MaxBytes) { return $Text }
    $n = $MaxBytes
    while ($n -gt 0 -and ($b[$n] -band 0xC0) -eq 0x80) { $n-- }
    return [Text.Encoding]::UTF8.GetString($b, 0, $n)
}

function Get-MhMatrixColumns([string[]] $Keys) {
    <# {key: column}. The column is the key itself, cut to PostgreSQL's 63-byte name
       limit; names that would collide (after cutting, or with FileId/SiteName/SiteId)
       get _2, _3... #>
    $used = New-OrdinalSet
    foreach ($c in $script:MatrixBaseColumns) { [void]$used.Add($c) }
    $out = New-Object 'Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    foreach ($k in $Keys) {
        $base = Limit-MhUtf8 $k 63
        if (-not $base) { $base = 'key' }
        $name = $base; $n = 1
        while ($used.Contains($name)) {
            $n++
            $sfx = "_$n"
            $name = (Limit-MhUtf8 $base (63 - $sfx.Length)) + $sfx
        }
        [void]$used.Add($name)
        $out[$k] = $name
    }
    return , $out
}

function Update-MhMatrix([hashtable] $Conn, $AppConfig, [scriptblock] $Log = { param($m) Write-Host $m }) {
    <# Rebuild the matrix table from "Metadata". One transaction, so the old matrix
       stays readable until the new one is complete. If a file has the same key
       twice, the row with the highest MetadataId wins. #>
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $table = Get-MhMatrixTable $AppConfig
    $t = "public.$(q $table)"
    $reg = lit $t
    $info = Get-MhJson $Conn @"
SELECT json_build_object(
    'keys', COALESCE((SELECT json_agg(k ORDER BY k)
                      FROM (SELECT DISTINCT "MetadataKey" AS k FROM public."Metadata") d), '[]'::json),
    'files_site_id', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
                             AND table_name = 'Files' AND column_name = 'SiteId'),
    'dups', (SELECT count(*) FROM (SELECT 1 FROM public."Metadata"
                                   GROUP BY "FileId", "MetadataKey" HAVING count(*) > 1) x),
    'exists', to_regclass($reg) IS NOT NULL,
    'comment', obj_description(to_regclass($reg), 'pg_class'));
"@
    $keys = [string[]]@($info.keys)
    if (-not $keys.Count) { throw '"Metadata" is empty: no keys to turn into columns' }
    if ($keys.Count -gt $script:MatrixMaxKeys) {
        throw "`"Metadata`" has $(fmt $keys.Count) distinct keys; a table can hold at most $(fmt $script:MatrixMaxKeys) key columns"
    }
    if ($info.exists -and -not ([string]$info.comment).StartsWith($script:MatrixMarker)) {
        throw "Table `"$table`" already exists and was not built by Build Matrix; it is left alone. Rename or drop it first."
    }
    $cols = Get-MhMatrixColumns $keys
    $renamed = @($keys | Where-Object { $cols[$_] -cne $_ }).Count
    & $Log ("$(fmt $keys.Count) distinct MetadataKey values -> $(fmt $keys.Count) columns" +
            $(if ($renamed) { " ($renamed renamed to fit, see column comments)" } else { '' }))
    if ([long]$info.dups) {
        & $Log "$(fmt $info.dups) file/key pair(s) appear more than once; the latest row (highest MetadataId) is used"
    }

    $site = [bool]$info.files_site_id
    $sb = New-Object Text.StringBuilder
    [void]$sb.AppendLine('BEGIN;').AppendLine('SET LOCAL client_min_messages = warning;').AppendLine("DROP TABLE IF EXISTS $t;")
    [void]$sb.AppendLine("CREATE TABLE $t (")
    [void]$sb.AppendLine('    "FileId" integer PRIMARY KEY REFERENCES public."Files"("FileId") ON DELETE CASCADE,')
    [void]$sb.AppendLine('    "SiteName" varchar(256),')
    if ($site) { [void]$sb.AppendLine('    "SiteId" integer,') }
    [void]$sb.AppendLine((($keys | ForEach-Object { "    $(q $cols[$_]) text" }) -join ",`n")).AppendLine(');')
    [void]$sb.Append("INSERT INTO $t (`"FileId`", `"SiteName`"" + $(if ($site) { ', "SiteId"' } else { '' }) + ', ')
    [void]$sb.AppendLine((($keys | ForEach-Object { q $cols[$_] }) -join ', ') + ')')
    [void]$sb.AppendLine('SELECT m."FileId", MAX(f."SiteName")' + $(if ($site) { ', MAX(f."SiteId")' } else { '' }) + ',')
    [void]$sb.AppendLine((($keys | ForEach-Object {
        "    NULLIF((array_agg(m.`"MetadataValue`" ORDER BY m.`"MetadataId`" DESC) FILTER (WHERE m.`"MetadataKey`" = $(lit $_)))[1], '')"
    }) -join ",`n"))
    [void]$sb.AppendLine('FROM public."Metadata" m JOIN public."Files" f ON f."FileId" = m."FileId"')
    [void]$sb.AppendLine('GROUP BY m."FileId";')
    [void]$sb.AppendLine("CREATE INDEX $(q ('IX_' + $table + '_SiteName')) ON $t (`"SiteName`");")
    [void]$sb.AppendLine("COMMENT ON TABLE $t IS " + (lit ("$($script:MatrixMarker) one row per file, one column per distinct " +
                         '"Metadata"."MetadataKey". Rebuilt by Build Matrix; "Metadata" itself is unchanged.')) + ';')
    foreach ($k in $keys) { [void]$sb.AppendLine("COMMENT ON COLUMN $t.$(q $cols[$k]) IS $(lit ('Metadata key: ' + $k));") }
    [void]$sb.AppendLine(@"
SELECT json_build_object(
    'files', (SELECT count(*) FROM $t),
    'orphans', (SELECT count(DISTINCT m."FileId") FROM public."Metadata" m
                WHERE NOT EXISTS (SELECT 1 FROM public."Files" f WHERE f."FileId" = m."FileId")));
COMMIT;
"@)
    $summary = (Invoke-MhPsql -Conn $Conn -Sql $sb.ToString()) | ConvertFrom-Json
    & $Log ("Built ${table}: $(fmt $summary.files) files x $(fmt $keys.Count) key columns" +
            $(if ([long]$summary.orphans) { "; $(fmt $summary.orphans) file(s) in Metadata without a Files row skipped" } else { '' }) +
            "  [$(secs $sw)s]")
    return [ordered]@{ files = [long]$summary.files; columns = $keys.Count; duplicates = [long]$info.dups
                       orphan_files = [long]$summary.orphans }
}


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

function Get-MhTableView([hashtable] $Conn, [string] $Table, [int] $Limit = 500) {
    <# First $Limit rows (with the file path in front) -> @{ Columns; Rows; Total }, or $null if
       the table does not exist. #>
    $t = "public.$(q $Table)"
    if ((Get-MhLines $Conn "SELECT to_regclass($(lit $t)) IS NOT NULL;")[0] -ne 't') { return $null }
    $out = Invoke-MhPsql -Conn $Conn -Sql @"
SELECT count(*) FROM $t;
COPY (SELECT f."OriginalSharePointPath", h.* FROM $t h
      JOIN public."Files" f ON f."FileId" = h."FileId"
      ORDER BY h."FileId" LIMIT $Limit) TO STDOUT WITH (FORMAT csv, HEADER);
"@
    $nl = $out.IndexOf("`n")
    $rows = Read-MhCsvText $out.Substring($nl + 1)
    $cols = if ($rows.Count) { $rows[0] } else { @() }
    if ($rows.Count) { $rows.RemoveAt(0) }
    return @{ Columns = $cols; Rows = $rows; Total = [long]$out.Substring(0, $nl).Trim() }
}


Export-ModuleMember -Function Read-MhConfig, Save-MhConnection, Get-MhRoleName, Get-MhSettings, Get-MhMatrixTable,
    Find-MhPsql, New-MhConnection, Invoke-MhPsql, Test-MhConnection, Get-MhDdl, Initialize-MhTable, Open-MhReader,
    Read-MhRow, Close-MhReader, Import-MhFile, Convert-MhExisting, Update-MhMatrix, Get-MhTableView
