<#
SharePoint 2013 (on-premises) metadata export: every file in the chosen document
libraries -> one Excel (or CSV) sheet, one row per file, one column per field.
The sheet can be uploaded as-is with "Upload CSV/XLSX" (Metadata Horizontal).

    $sp   = New-SpSession 'https://sp.corp/sites/abc'            # signed-in Windows user
    $sp   = New-SpSession 'https://sp.corp/sites/abc' 'CORP\me' 'pw'
    $libs = Get-SpWebs $sp -Recurse | ForEach-Object { Get-SpLibraries $sp $_ }
    Export-SpMetadata $sp $libs 'C:\data\metadata.xlsx'

Read only. REST API (/_api, odata=verbose), which SharePoint 2013 RTM and later
support; login is NTLM/Kerberos as the Windows user, or a DOMAIN\user + password.

Columns:
    OriginalSharePointPath  https://host + FileRef (identity for Upload)
    UniqueId, FileRef, FileLeafRef, FileDirRef, FSObjType
    then every field of the libraries by InternalName (the union over all
    libraries, in first-seen order): visible fields that are not Computed, plus
    Title, ContentType, Created, Modified, Author, Editor, _UIVersionString,
    CheckoutUser.
Values: the text SharePoint shows (people -> display names, lookups / managed
metadata -> labels, multi-values joined with '; '); date fields as local time
yyyy-MM-dd HH:mm:ss. Folders are left out (Upload skips them anyway).
#>
$ErrorActionPreference = 'Stop'

$script:Inv = [Globalization.CultureInfo]::InvariantCulture
$script:PageSize = 500
$script:FrontColumns = @('OriginalSharePointPath', 'UniqueId', 'FileRef', 'FileLeafRef', 'FileDirRef', 'FSObjType')
$script:AlwaysFields = @('Title', 'ContentType', 'Created', 'Modified', 'Author', 'Editor', '_UIVersionString', 'CheckoutUser')
$script:ExcelMaxRows = 1048576
$script:ExcelMaxText = 32767
# libraries SharePoint creates for itself: listed, but not ticked by default
$script:SystemFolders = @('Style Library', 'SiteAssets', 'SitePages', 'FormServerTemplates', 'SiteCollectionDocuments',
                          'SiteCollectionImages', 'PublishingImages', 'Pages', 'IWConvertedForms', '_catalogs')

try {   # SharePoint behind TLS 1.2 (Windows PowerShell 5.1 does not enable it by default)
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }


function ConvertFrom-SpJson([string] $Text) {
    <# JSON -> nested dictionaries. Windows PowerShell's ConvertFrom-Json stops at 2 MB,
       so 5.1 uses JavaScriptSerializer without that limit. #>
    if ($PSVersionTable.PSVersion.Major -ge 6) { return (ConvertFrom-Json -InputObject $Text -AsHashtable) }
    if (-not $script:Serializer) {
        Add-Type -AssemblyName System.Web.Extensions
        $script:Serializer = New-Object Web.Script.Serialization.JavaScriptSerializer
        $script:Serializer.MaxJsonLength = [int]::MaxValue
        $script:Serializer.RecursionLimit = 1000
    }
    return $script:Serializer.DeserializeObject($Text)
}

function Get-SpErrorText($ErrorRecord) {
    <# -> @{ Code; Message } from a failed request (SharePoint puts the reason in a JSON body). #>
    $e = $ErrorRecord.Exception
    while ($e -and $e -isnot [Net.WebException]) { $e = $e.InnerException }
    if (-not $e) { return @{ Code = 0; Timeout = $false; Message = $ErrorRecord.Exception.Message } }
    $code = 0
    $msg = $null
    if ($e.Response) {
        $code = [int]$e.Response.StatusCode
        try {
            $sr = New-Object IO.StreamReader($e.Response.GetResponseStream())
            $msg = (ConvertFrom-SpJson $sr.ReadToEnd())['error']['message']['value']
        } catch { }
    }
    if (-not $msg) { $msg = $e.Message }
    if ($code -eq 401) { $msg = "Access denied (401): check the login and that it can read this site. $msg" }
    return @{ Code = $code; Timeout = $e.Status -eq [Net.WebExceptionStatus]::Timeout; Message = $msg }
}

function Invoke-SpGet([hashtable] $Sp, [string] $Url) {
    <# GET a REST URL -> the "d" object. Retries throttling / timeouts twice. #>
    for ($attempt = 1; ; $attempt++) {
        $wc = New-Object Net.WebClient
        try {
            $wc.Encoding = [Text.Encoding]::UTF8
            if ($Sp.Credential) { $wc.Credentials = $Sp.Credential } else { $wc.UseDefaultCredentials = $true }
            $wc.Headers['Accept'] = 'application/json;odata=verbose'
            return (ConvertFrom-SpJson $wc.DownloadString($Url))['d']
        } catch {
            $err = Get-SpErrorText $_
            if ($attempt -lt 3 -and ($err.Timeout -or $err.Code -in 429, 502, 503, 504)) {
                Start-Sleep -Seconds (5 * $attempt * $attempt)
                continue
            }
            throw "SharePoint: $($err.Message)`n  (GET $Url)"
        } finally {
            $wc.Dispose()
        }
    }
}

function New-SpSession([string] $SiteUrl, [string] $UserName, [string] $Password) {
    <# Check the login and return the session (site URL as SharePoint knows it, credential).
       Accepts a pasted page URL too (.../Shared Documents/Forms/AllItems.aspx). #>
    $url = $SiteUrl.Trim()
    if ($url -notmatch '^https?://') { throw 'Site URL must start with http:// or https://' }
    $url = $url -replace '[?#].*$', ''
    $url = $url -replace '/(_layouts|_vti_bin|_api)(/.*)?$', ''
    $url = $url -replace '/Forms/[^/]*\.aspx$', '' -replace '/[^/]*\.aspx$', ''
    $url = $url.TrimEnd('/')
    $cred = $null
    if ($UserName) {
        $domain = ''
        $user = $UserName.Trim()
        if ($user.Contains('\')) { $domain, $user = $user.Split('\', 2) }
        $cred = New-Object Net.NetworkCredential($user, $Password, $domain)
    }
    $sp = @{ Credential = $cred; Url = $url }
    # a pasted library URL is not a site: walk up until SharePoint answers with a web
    while ($true) {
        try {
            $web = Invoke-SpGet $sp "$url/_api/web?`$select=Url,Title"
            break
        } catch {
            $up = $url -replace '/[^/]+$', ''
            if ($_.Exception.Message -match 'Access denied' -or $up -notmatch '^https?://[^/]+/.+' -or $up -eq $url) { throw }
            $url = $up
        }
    }
    $sp.Url = $web['Url']
    $sp.Title = $web['Title']
    return $sp
}

function Get-SpWebs([hashtable] $Sp, [string] $WebUrl, [switch] $Recurse, [scriptblock] $Log = { param($m) Write-Host $m }) {
    <# The web (default: the session's site) and, with -Recurse, every subsite the login can open. #>
    if (-not $WebUrl) { $WebUrl = $Sp.Url }
    $out = New-Object 'Collections.Generic.List[hashtable]'
    $web = Invoke-SpGet $Sp "$WebUrl/_api/web?`$select=Url,Title"
    $out.Add(@{ Url = $web['Url']; Title = $web['Title'] })
    if ($Recurse) {
        for ($i = 0; $i -lt $out.Count; $i++) {
            try { $subs = (Invoke-SpGet $Sp "$($out[$i].Url)/_api/web/webs?`$select=Url,Title")['results'] }
            catch { & $Log "Subsites of $($out[$i].Url) skipped: $($_.Exception.Message.Split("`n")[0])"; continue }
            foreach ($s in $subs) { $out.Add(@{ Url = $s['Url']; Title = $s['Title'] }) }
        }
    }
    return , $out
}

function Get-SpLibraries([hashtable] $Sp, [hashtable] $Web) {
    <# Visible document libraries of one web -> list of @{ WebUrl; WebTitle; Id; Title; Folder; ItemCount; System } #>
    $d = Invoke-SpGet $Sp ("$($Web.Url)/_api/web/lists?`$select=Id,Title,ItemCount,RootFolder/ServerRelativeUrl" +
                           "&`$expand=RootFolder&`$filter=BaseType%20eq%201%20and%20Hidden%20eq%20false")
    $out = New-Object 'Collections.Generic.List[hashtable]'
    foreach ($l in $d['results']) {
        $folder = [string]$l['RootFolder']['ServerRelativeUrl']
        $leaf = $folder.Substring($folder.LastIndexOf('/') + 1)
        $out.Add(@{ WebUrl = $Web.Url; WebTitle = $Web.Title; Id = [string]$l['Id']; Title = [string]$l['Title']
                    Folder = $folder; ItemCount = [long]$l['ItemCount']
                    System = ($script:SystemFolders -contains $leaf) -or $folder.Contains('/_catalogs/') })
    }
    return , $out
}

function Format-SpDate($Value) {
    <# REST dates are UTC -> local time, like the Excel upload format. #>
    if ($Value -is [DateTime]) {
        $d = if ($Value.Kind -eq 'Local') { $Value } else { [DateTime]::SpecifyKind($Value, 'Utc').ToLocalTime() }
    } elseif ($Value -is [string] -and $Value) {
        $o = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParse($Value, $script:Inv, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$o)) { return $Value }
        $d = $o.LocalDateTime
    } else {
        return ''
    }
    return $d.ToString('yyyy-MM-dd HH:mm:ss', $script:Inv)
}

function Format-SpValue($Value) {
    if ($null -eq $Value) { return '' }
    if ($Value -is [string]) { $s = $Value }
    elseif ($Value -is [DateTime]) { $s = Format-SpDate $Value }
    elseif ($Value -is [Collections.IDictionary] -or $Value -is [Array]) { return '' }   # __deferred links etc.
    else { $s = [Convert]::ToString($Value, $script:Inv) }
    if ($s.IndexOf([char]0) -ge 0) { $s = $s.Replace([string][char]0, '') }
    return $s
}

function Export-SpMetadata {
    <# Read every file of $Libraries and write one sheet to $Path (.xlsx or .csv). Returns stats. #>
    param([hashtable] $Sp, $Libraries, [string] $Path, [scriptblock] $Log = { param($m) Write-Host $m }, [scriptblock] $Progress)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $xlsx = [IO.Path]::GetExtension($Path).ToLowerInvariant() -eq '.xlsx'
    if ($xlsx -and -not (Get-Module ImportExcel)) {
        if (-not (Get-Module -ListAvailable ImportExcel)) { throw 'Saving .xlsx needs the ImportExcel module (or choose .csv).' }
        Import-Module ImportExcel
    }
    $columns = New-Object 'Collections.Generic.List[string]'
    $colIndex = New-Object 'Collections.Generic.Dictionary[string,int]' ([StringComparer]::Ordinal)
    foreach ($c in $script:FrontColumns) { $colIndex[$c] = $columns.Count; $columns.Add($c) }
    $rows = New-Object 'Collections.Generic.List[string[]]'
    $stats = [ordered]@{ libraries = 0; files = 0; folders_skipped = 0; columns = 0 }

    foreach ($lib in $Libraries) {
        $lsw = [Diagnostics.Stopwatch]::StartNew()
        $name = "$($lib.WebTitle) / $($lib.Title)"
        $listApi = "$($lib.WebUrl)/_api/web/lists(guid'$($lib.Id)')"
        $authority = ([Uri]$lib.WebUrl).GetLeftPart([UriPartial]::Authority)

        # fields of this library -> sheet columns (InternalName)
        $fields = (Invoke-SpGet $Sp "$listApi/fields?`$select=InternalName,TypeAsString,Hidden")['results']
        $pick = New-Object 'Collections.Generic.List[object]'
        $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($f in $fields) {
            $n = [string]$f['InternalName']
            if ($colIndex.ContainsKey($n) -and $script:FrontColumns -ccontains $n) { continue }
            $use = ($script:AlwaysFields -ccontains $n) -or (-not $f['Hidden'] -and $f['TypeAsString'] -ne 'Computed')
            if (-not $use -or -not $seen.Add($n)) { continue }
            if (-not $colIndex.ContainsKey($n)) { $colIndex[$n] = $columns.Count; $columns.Add($n) }
            $pick.Add(@{
                Index = $colIndex[$n]
                Text  = $n.Replace('_', '_x005f_')                                     # key in FieldValuesAsText
                Raw   = $(if ($n.StartsWith('_')) { 'OData_' + $n } else { $n })        # key of the raw value
                Date  = $f['TypeAsString'] -eq 'DateTime'
            })
        }

        $url = "$listApi/items?`$select=*,FileRef,FileLeafRef,FileDirRef,UniqueId,FSObjType,FieldValuesAsText" +
               "&`$expand=FieldValuesAsText&`$top=$script:PageSize"
        $files = 0; $folders = 0
        while ($url) {
            $d = Invoke-SpGet $Sp $url
            foreach ($it in $d['results']) {
                $t = $it['FieldValuesAsText']
                if ("$($it['FSObjType'])" -eq '1') { $folders++; continue }
                $row = New-Object 'string[]' $columns.Count
                $fileRef = Format-SpValue $it['FileRef']
                $row[0] = $authority + $fileRef
                $row[1] = Format-SpValue $it['UniqueId']
                $row[2] = $fileRef
                $row[3] = Format-SpValue $it['FileLeafRef']
                $row[4] = Format-SpValue $it['FileDirRef']
                $row[5] = Format-SpValue $it['FSObjType']
                foreach ($p in $pick) {
                    if ($p.Date -and $it.ContainsKey($p.Raw)) { $v = Format-SpDate $it[$p.Raw] }
                    elseif ($t -and $t.ContainsKey($p.Text)) { $v = Format-SpValue $t[$p.Text] }
                    else { $v = Format-SpValue $it[$p.Raw] }
                    if ($xlsx -and $v.Length -gt $script:ExcelMaxText) { $v = $v.Substring(0, $script:ExcelMaxText) }
                    $row[$p.Index] = $v
                }
                $rows.Add($row)
                $files++
            }
            if ($Progress) { & $Progress ($rows.Count) $name }
            $url = $d['__next']
        }
        $stats.libraries++
        $stats.files += $files
        $stats.folders_skipped += $folders
        & $Log ("${name}: $($files.ToString('N0', $script:Inv)) files" +
                $(if ($folders) { " ($($folders.ToString('N0', $script:Inv)) folders left out)" } else { '' }) +
                "  [$($lsw.Elapsed.TotalSeconds.ToString('0.0', $script:Inv))s]")
    }

    # ---- write -------------------------------------------------------------------
    $width = $columns.Count
    $stats.columns = $width
    $dir = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
    if ($dir) { [void](New-Item -ItemType Directory -Force -Path $dir) }
    if ($xlsx) {
        if ($rows.Count + 1 -gt $script:ExcelMaxRows) {
            throw "$($rows.Count.ToString('N0', $script:Inv)) files do not fit in one Excel sheet (max 1,048,575 rows): save as .csv instead."
        }
        $data = New-Object 'Collections.Generic.List[object[]]' ($rows.Count + 1)
        $data.Add([object[]]$columns.ToArray())
        foreach ($r in $rows) {
            $a = New-Object 'object[]' $width
            [Array]::Copy($r, $a, $r.Length)
            $data.Add($a)
        }
        $pkg = New-Object OfficeOpenXml.ExcelPackage
        try {
            $ws = $pkg.Workbook.Worksheets.Add('Metadata')
            [void]$ws.Cells['A1'].LoadFromArrays($data)
            $ws.Cells[1, 1, 1, $width].Style.Font.Bold = $true
            $ws.Cells[1, 1, 1, $width].AutoFilter = $true
            $ws.View.FreezePanes(2, 1)
            $pkg.SaveAs((New-Object IO.FileInfo($Path)))
        } finally {
            $pkg.Dispose()
        }
    } else {
        $w = New-Object IO.StreamWriter($Path, $false, (New-Object Text.UTF8Encoding($true)))
        try {
            $q = { param($s) '"' + "$s".Replace('"', '""') + '"' }
            $w.WriteLine((($columns | ForEach-Object { & $q $_ }) -join ','))
            $line = New-Object Text.StringBuilder
            foreach ($r in $rows) {
                [void]$line.Clear()
                for ($i = 0; $i -lt $width; $i++) {
                    if ($i) { [void]$line.Append(',') }
                    $v = if ($i -lt $r.Length) { $r[$i] } else { $null }
                    [void]$line.Append('"').Append("$v".Replace('"', '""')).Append('"')
                }
                $w.WriteLine($line.ToString())
            }
        } finally {
            $w.Dispose()
        }
    }
    $stats.path = $Path
    & $Log ("Saved $($stats.files.ToString('N0', $script:Inv)) files x $width columns from $($stats.libraries) libraries " +
            "to $Path  [$($sw.Elapsed.TotalSeconds.ToString('0.0', $script:Inv))s]")
    return $stats
}


Export-ModuleMember -Function New-SpSession, Invoke-SpGet, Get-SpWebs, Get-SpLibraries, Export-SpMetadata
