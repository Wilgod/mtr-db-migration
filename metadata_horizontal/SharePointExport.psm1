<#
SharePoint 2013 (on-premises) metadata export: every file in the chosen document
libraries -> one Excel (or CSV) sheet, one row per file, one column per field.
The sheet can be uploaded as-is with "Upload CSV/XLSX" (Metadata Horizontal).

    $sp   = New-SpSession 'https://sp.corp/sites/abc'            # signed-in Windows user
    $sp   = New-SpSession 'https://sp.corp/sites/abc' 'CORP\me' 'pw'
    $libs = Get-SpWebs $sp -Recurse | ForEach-Object { Get-SpLibraries $sp $_ }
    Export-SpMetadata $sp $libs 'C:\data\metadata.xlsx'
    Compare-SpExport 'C:\data\metadata.xlsx' 'C:\data\library_csvs' 'C:\data\compare.xlsx'

Read only. REST API (/_api, odata=verbose), which SharePoint 2013 RTM and later
support; login is NTLM/Kerberos as the Windows user, or a DOMAIN\user + password.

Columns:
    OriginalSharePointPath  https://host + FileRef (identity for Upload)
    UniqueId, FileRef, FileLeafRef, FileDirRef, FSObjType
    LibraryTitle, LibraryId title and list GUID of the file's document library
    then every field of the libraries by InternalName (the union over all
    libraries, in first-seen order), hidden and Computed fields included.
Values: the text SharePoint shows (people -> display names, lookups / managed
metadata -> labels, multi-values joined with '; '); date fields as local time
yyyy-MM-dd HH:mm:ss. Folders are left out (Upload skips them anyway).
$select=* does not return hidden fields or people names, so those fields are read in extra
passes over the library, in groups small enough for the URL and lookup limits. Small test
requests check each group first. A field that cannot be read is logged, marked Exported=No
in the Library columns sheet and left out; it does not stop the export.
Rows go to a work folder next to the output file, page by page, then are merged into the file,
so memory stays low and a stopped export can continue from its last page.

An .xlsx also gets a second sheet, "Library columns": one row per field of each exported
library, including libraries without files. Compare-SpExport uses it to see which columns
each library really has. Upload reads the first sheet only.
#>
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName Microsoft.VisualBasic

$script:Inv = [Globalization.CultureInfo]::InvariantCulture
$script:PageSize = 500
$script:FrontColumns = @('OriginalSharePointPath', 'UniqueId', 'FileRef', 'FileLeafRef', 'FileDirRef', 'FSObjType', 'LibraryTitle', 'LibraryId')
$script:AlwaysFields = @('Title', 'ContentType', 'Created', 'Modified', 'Author', 'Editor', '_UIVersionString', 'CheckoutUser')
# limits of one extra-pass request: query length (SharePoint's default is 2048) and lookups (its threshold is 8)
$script:GroupMaxChars = 1500
$script:GroupMaxExpands = 6
# hidden fields with another place to read them when the item does not return the field itself
$script:HiddenFields = [ordered]@{
    'File_x0020_Size'     = 'File/Length'
    'Created_x0020_Date'  = 'File/TimeCreated'
    'Last_x0020_Modified' = 'File/TimeLastModified'
    'Created_x0020_By'    = 'File/Author/LoginName'
    'Modified_x0020_By'   = 'File/ModifiedBy/LoginName'
}
$script:HiddenDates = @('Created_x0020_Date', 'Last_x0020_Modified')
$script:ExcelMaxRows = 1048576
$script:LibColumnsSheet = 'Library columns'
$script:LibColumnsHeader = @('SiteUrl', 'SiteTitle', 'LibraryTitle', 'LibraryFolder', 'InternalName', 'FieldTitle', 'FieldType', 'Hidden', 'Exported')
$script:ExportOnlyColumns = @('OriginalSharePointPath', 'LibraryTitle', 'LibraryId')    # written by Export-SpMetadata, not a SharePoint field
$script:ExcelMaxText = 32767
$script:RowByRowBytes = 200MB                          # Upload and Compare read an xlsx row by row from this size (unzipped); EPPlus stops near 2 GB and needs far more memory
$script:WorkFolderName = 'sharepoint_export_work'     # next to the output file, while an export runs
$script:WorkMaxDays = 7                               # an unfinished export can be continued for this long
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
    <# GET a REST URL -> the "d" object. Retries throttling / timeouts twice.
       With $Sp.RequestLog, each request logs a line when it starts (so one that hangs shows) and when it ends. #>
    $requestLog = $Sp.RequestLog
    $no = 0
    if ($requestLog) { $Sp.RequestNo = [int]$Sp.RequestNo + 1; $no = $Sp.RequestNo }
    for ($attempt = 1; ; $attempt++) {
        $wc = New-Object Net.WebClient
        $clock = [Diagnostics.Stopwatch]::StartNew()
        try {
            $wc.Encoding = [Text.Encoding]::UTF8
            if ($Sp.Credential) { $wc.Credentials = $Sp.Credential } else { $wc.UseDefaultCredentials = $true }
            $wc.Headers['Accept'] = 'application/json;odata=verbose'
            if ($requestLog) { & $requestLog "#$no$(if ($attempt -gt 1) { " try $attempt" }) start: $Url" }
            $text = $wc.DownloadString($Url)
            if ($requestLog) { & $requestLog ("#$no ok {0:0.0}s, {1:N0} KB" -f $clock.Elapsed.TotalSeconds, ($text.Length / 1KB)) }
            return (ConvertFrom-SpJson $text)['d']
        } catch {
            $err = Get-SpErrorText $_
            $retry = $attempt -lt 3 -and ($err.Timeout -or $err.Code -in 429, 502, 503, 504)
            if ($requestLog) {
                & $requestLog ("#$no failed after {0:0.0}s: {1}{2}" -f $clock.Elapsed.TotalSeconds, "$($err.Message)".Split("`n")[0].TrimEnd('.'), $(if ($retry) { ". Trying again in $(5 * $attempt * $attempt)s" }))
            }
            if ($retry) {
                Start-Sleep -Seconds (5 * $attempt * $attempt)
                continue
            }
            throw "SharePoint: $($err.Message)`n  (GET $Url)"
        } finally {
            $wc.Dispose()
        }
    }
}

function New-SpRequestLog([string] $Dir, [scriptblock] $Log) {
    <# -> what $Sp.RequestLog needs: every line goes in full to a new file in $Dir, and shortened to $Log. #>
    $file = $null
    try {
        [void][IO.Directory]::CreateDirectory($Dir)
        $file = Join-Path $Dir ('sharepoint_requests_{0:yyyyMMdd_HHmmss}.log' -f (Get-Date))
        if ($Log) { & $Log "Every SharePoint request is also logged in $file" }
    } catch { $file = $null }
    {
        param($Line)
        if ($file) { try { [IO.File]::AppendAllText($file, ('[{0:yyyy-MM-dd HH:mm:ss}] {1}{2}' -f (Get-Date), $Line, "`r`n")) } catch { } }
        if ($Log) { & $Log $(if ($Line.Length -gt 220) { $Line.Substring(0, 220) + '...' } else { $Line }) }
    }.GetNewClosure()
}

function New-SpSession([string] $SiteUrl, [string] $UserName, [string] $Password, [scriptblock] $RequestLog) {
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
    $sp = @{ Credential = $cred; Url = $url; RequestLog = $RequestLog }
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
    $sp.Url = Use-SpHost $sp $web['Url']
    $sp.Title = $web['Title']
    return $sp
}

function Get-SpWebs([hashtable] $Sp, [string] $WebUrl, [switch] $Recurse, [scriptblock] $Log = { param($m) Write-Host $m }) {
    <# The web (default: the session's site) and, with -Recurse, every subsite at every level the login can open. #>
    if (-not $WebUrl) { $WebUrl = $Sp.Url }
    $out = New-Object 'Collections.Generic.List[hashtable]'
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $web = Invoke-SpGet $Sp "$WebUrl/_api/web?`$select=Url,Title"
    $out.Add(@{ Url = (Use-SpHost $Sp $web['Url']); Title = $web['Title'] })
    [void]$seen.Add($out[0].Url)
    if ($Recurse) {
        for ($i = 0; $i -lt $out.Count; $i++) {
            $url = $out[$i].Url
            # /webs needs "Enumerate subsites" on the parent; the filtered call lists the
            # subsites the login can open even without it
            $subs = $null
            $errs = @()
            foreach ($api in "webs?`$select=Url,Title", 'getsubwebsfilteredforcurrentuser(nwebtemplatefilter=-1,nconfigurationfilter=0)') {
                try { $subs = (Invoke-SpGet $Sp "$url/_api/web/$api")['results']; break }
                catch { $errs += $_.Exception.Message.Split("`n")[0] }
            }
            if ($null -eq $subs) { & $Log "Subsites of $url skipped: $($errs -join ' / ')"; continue }
            $n = 0
            foreach ($s in $subs) {
                $u = Use-SpHost $Sp ([string]$s['Url'])
                if ($u -and $seen.Add($u)) { $out.Add(@{ Url = $u; Title = $s['Title'] }); $n++ }
            }
            if ($n) { & $Log "$url : $n subsite(s)" }
        }
    }
    return , $out
}

function Use-SpHost([hashtable] $Sp, [string] $Url) {
    <# SharePoint returns URLs of its default zone (alternate access mapping), which may be a host
       this PC cannot reach or log in to: keep the scheme + host the user typed. #>
    if ($Url -notmatch '^https?://[^/]+(.*)$') { return $Url }
    $path = $Matches[1]
    if ($Sp.Url -notmatch '^(https?://[^/]+)') { return $Url }
    return $Matches[1] + $path
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
    elseif ($Value -is [Collections.IDictionary]) {
        if ($Value.ContainsKey('StringValue')) { return [string]$Value['StringValue'] }  # ContentTypeId
        return ''                                                                        # __deferred links etc.
    }
    elseif ($Value -is [Array]) { return '' }
    else { $s = [Convert]::ToString($Value, $script:Inv) }
    if ($s.IndexOf([char]0) -ge 0) { $s = $s.Replace([string][char]0, '') }
    return $s
}

function Get-SpPathValue($Item, [string[]] $Path) {
    <# Value at Path ('Author','Title') in an item; expanded multi-values are joined with '; '. #>
    $v = $Item
    foreach ($k in $Path) {
        if ($v -isnot [Collections.IDictionary]) { return $null }
        if ($v.ContainsKey('results')) {
            $parts = foreach ($e in $v['results']) { if ($e -is [Collections.IDictionary] -and $e.ContainsKey($k)) { Format-SpValue $e[$k] } }
            return (@($parts) -join '; ')
        }
        if (-not $v.ContainsKey($k)) { return $null }
        $v = $v[$k]
    }
    if ($v -is [Collections.IDictionary] -and $v.ContainsKey('results')) { return (@($v['results'] | ForEach-Object { Format-SpValue $_ }) -join '; ') }
    return $v
}

function New-SpFieldOption([string] $Path) {
    <# 'File/Author/LoginName' -> what to select, what to expand, where the value is. #>
    $i = $Path.LastIndexOf('/')
    return @{ Select = $Path; Expand = $(if ($i -gt 0) { $Path.Substring(0, $i) }); Path = [string[]]$Path.Split('/'); Label = $Path }
}

function Get-SpGroupUrl([string] $ListApi, $Options, [int] $Top) {
    $sel = @('Id') + @($Options | ForEach-Object { $_.Select })
    $exp = @($Options | Where-Object { $_.Expand } | ForEach-Object { $_.Expand } | Select-Object -Unique)
    $u = "$ListApi/items?`$select=$($sel -join ',')"
    if ($exp.Count) { $u += "&`$expand=$($exp -join ',')" }
    return "$u&`$top=$Top"
}

function Test-SpItemQuery([hashtable] $Sp, [string] $ListApi, $Options) {
    <# Can this library return these fields together? Asks for one item only. #>
    try { [void](Invoke-SpGet $Sp (Get-SpGroupUrl $ListApi $Options 1)); return $true }
    catch { return $false }
}

function Test-SpGroupFits($Options) {
    $chars = 0; $expands = 0
    foreach ($o in $Options) {
        $chars += $o.Select.Length + 1
        if ($o.Expand) { $chars += $o.Expand.Length + 1; if (-not $o.Expand.StartsWith('File')) { $expands++ } }
    }
    return ($chars -le $script:GroupMaxChars -and $expands -le $script:GroupMaxExpands)
}

function Group-SpFieldOptions([hashtable] $Sp, [string] $ListApi, $Wanted, [hashtable] $Remember) {
    <# Wanted: @{ Name; Options; Sticky } -> list of groups, each a list of @{ Name; Option }, that the
       library answers together. Remember keeps how each Sticky field is read (-1 = it cannot be), so
       later libraries normally need one test request per group. #>
    $groups = New-Object 'Collections.Generic.List[object]'
    $retry = New-Object 'Collections.Generic.List[object]'
    $checked = $false
    $flush = {
        param($chunk)
        if (-not $chunk.Count) { return }
        if (Test-SpItemQuery $Sp $ListApi @($chunk | ForEach-Object { $_.Option })) { $groups.Add($chunk) }
        else {
            if (-not $checked) {       # a plain query failing too is not about a field: report it
                [void](Invoke-SpGet $Sp (Get-SpGroupUrl $ListApi @() 1))
                Set-Variable -Name checked -Value $true -Scope 1
            }
            foreach ($c in $chunk) { $retry.Add($c.Want) }
        }
    }
    $chunk = New-Object 'Collections.Generic.List[object]'
    foreach ($w in $Wanted) {
        $k = if ($Remember.ContainsKey($w.Name)) { [int]$Remember[$w.Name] } else { 0 }
        if ($k -lt 0) { continue }
        $item = @{ Name = $w.Name; Option = $w.Options[$k]; Want = $w }
        $opts = @($chunk | ForEach-Object { $_.Option }) + @($item.Option)
        if (-not (Test-SpGroupFits $opts)) {
            & $flush $chunk
            $chunk = New-Object 'Collections.Generic.List[object]'
        }
        $chunk.Add($item)
    }
    & $flush $chunk

    # a group failed: find each field's way alone, then pack again, a new group whenever they do not fit together
    $chunk = New-Object 'Collections.Generic.List[object]'
    foreach ($w in $retry) {
        $found = -1
        for ($i = 0; $i -lt $w.Options.Count; $i++) {
            if (Test-SpItemQuery $Sp $ListApi @($w.Options[$i])) { $found = $i; break }
        }
        if ($w.Sticky) { $Remember[$w.Name] = $found }
        if ($found -lt 0) { continue }
        $item = @{ Name = $w.Name; Option = $w.Options[$found]; Want = $w }
        $opts = @($chunk | ForEach-Object { $_.Option }) + @($item.Option)
        if (-not (Test-SpGroupFits $opts) -or -not (Test-SpItemQuery $Sp $ListApi $opts)) {
            if ($chunk.Count) { $groups.Add($chunk) }
            $chunk = New-Object 'Collections.Generic.List[object]'
        }
        $chunk.Add($item)
    }
    if ($chunk.Count) { $groups.Add($chunk) }
    return , $groups
}

# ---------------------------------------------------------------------------
# Fast part of the export, compiled when the first export starts. PowerShell takes about 50 microseconds
# per value here, far too slow for 500,000 files x 190 columns. Keep to C# 5: Windows PowerShell 5.1 compiles it.
#
# Each step over a library (a "pass") is written to its own file in the work folder, page by page, with a
# checkpoint after each page. At the end the passes are merged by item id, one row at a time, into the csv or
# xlsx. So memory stays low whatever the size, and a stopped export continues from its last page.
# ---------------------------------------------------------------------------
$script:SpEngineSource = @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Xml;

namespace MetadataHorizontal {

public sealed class SpPick {
    public int Index; public string Text; public string Raw; public string One; public string[] Path; public bool Date;
}

public sealed class SpLibSpool {
    public string Main; public string[] Extras; public int[] Map;
    public SpLibSpool(string main, string[] extras, int[] map) { Main = main; Extras = extras; Map = map; }
}

// One pass over one library. Record: id, payload bytes, value count, then (column, text) for each value that is not empty.
public sealed class SpPass : IDisposable {
    internal static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, false);   // a broken character must not stop the export
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    readonly string path, chk;
    readonly FileStream fs;
    readonly BinaryWriter w;
    readonly MemoryStream recBytes = new MemoryStream(8192);
    readonly BinaryWriter rec;
    public long Length, Records, Files, Folders, Items;
    public int LastId;
    public bool Ascending = true, Done;
    public string NextUrl = "";
    internal readonly List<int> Cols = new List<int>();
    internal readonly List<string> Vals = new List<string>();

    public SpPass(string file) {
        path = file;
        chk = file + ".chk";
        string[] s = ReadChk(file);
        if (s != null) {
            Length = long.Parse(s[0], Inv); Records = long.Parse(s[1], Inv); Files = long.Parse(s[2], Inv); Folders = long.Parse(s[3], Inv);
            Items = long.Parse(s[4], Inv); LastId = int.Parse(s[5], Inv); Ascending = s[6] == "1"; Done = s[7] == "1"; NextUrl = s[8];
        }
        fs = new FileStream(path, FileMode.OpenOrCreate, FileAccess.Write, FileShare.Read, 262144);
        fs.SetLength(Length);                      // anything after the last checkpoint was never finished
        fs.Seek(Length, SeekOrigin.Begin);
        w = new BinaryWriter(fs, Utf8, true);
        rec = new BinaryWriter(recBytes, Utf8, true);
    }

    internal static string[] ReadChk(string file) {
        string c = file + ".chk";
        if (!File.Exists(c)) return null;
        string[] lines = File.ReadAllLines(c);
        return lines.Length >= 9 ? lines : null;
    }

    internal void Add(int id) {
        recBytes.SetLength(0);
        rec.Write(Cols.Count);
        for (int i = 0; i < Cols.Count; i++) { rec.Write(Cols[i]); rec.Write(Vals[i]); }
        rec.Flush();
        w.Write(id);
        w.Write((int)recBytes.Length);
        w.Write(recBytes.GetBuffer(), 0, (int)recBytes.Length);
        if (id <= LastId) Ascending = false;
        LastId = id;
        Records++;
        Cols.Clear(); Vals.Clear();
    }

    public void Checkpoint(string nextUrl) {
        w.Flush();
        fs.Flush(true);
        Length = fs.Position;
        NextUrl = nextUrl ?? "";
        Done = NextUrl.Length == 0;
        string tmp = chk + ".tmp";
        File.WriteAllLines(tmp, new string[] {
            Length.ToString(Inv), Records.ToString(Inv), Files.ToString(Inv), Folders.ToString(Inv), Items.ToString(Inv),
            LastId.ToString(Inv), Ascending ? "1" : "0", Done ? "1" : "0", NextUrl });
        for (int attempt = 1; ; attempt++) {       // antivirus can hold the file for a moment
            try {
                if (File.Exists(chk)) File.Replace(tmp, chk, null); else File.Move(tmp, chk);
                return;
            } catch (IOException) { if (attempt >= 5) throw; Thread.Sleep(200 * attempt); }
            catch (UnauthorizedAccessException) { if (attempt >= 5) throw; Thread.Sleep(200 * attempt); }
        }
    }

    public void Dispose() { w.Dispose(); rec.Dispose(); fs.Dispose(); recBytes.Dispose(); }
}

internal sealed class SpReader : IDisposable {
    readonly FileStream fs;
    readonly BinaryReader r;
    readonly long limit;
    long next;
    KeyValuePair<int, long>[] index;       // only when the server did not answer in id order
    int pos;
    public bool Has;
    public int Id;

    public SpReader(string file) {
        string[] s = SpPass.ReadChk(file);
        if (s == null) return;
        limit = long.Parse(s[0], CultureInfo.InvariantCulture);
        if (limit == 0) return;
        fs = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.Read, 262144);
        r = new BinaryReader(fs, SpPass.Utf8, true);
        if (s[6] != "1") BuildIndex();
        MoveNext();
    }

    void BuildIndex() {
        List<KeyValuePair<int, long>> list = new List<KeyValuePair<int, long>>();
        long at = 0;
        while (at < limit) {
            fs.Position = at;
            int id = r.ReadInt32();
            int len = r.ReadInt32();
            list.Add(new KeyValuePair<int, long>(id, at));
            at += 8 + len;
        }
        list.Sort(delegate(KeyValuePair<int, long> a, KeyValuePair<int, long> b) {
            int c = a.Key.CompareTo(b.Key);
            return c != 0 ? c : a.Value.CompareTo(b.Value);
        });
        List<KeyValuePair<int, long>> last = new List<KeyValuePair<int, long>>(list.Count);
        for (int i = 0; i < list.Count; i++) {     // an item seen twice: its latest answer
            if (i + 1 < list.Count && list[i + 1].Key == list[i].Key) continue;
            last.Add(list[i]);
        }
        index = last.ToArray();
    }

    public void MoveNext() {
        if (index != null) {
            if (pos >= index.Length) { Has = false; return; }
            fs.Position = index[pos++].Value;
            Id = r.ReadInt32();
            r.ReadInt32();
            Has = true;
            return;
        }
        if (next >= limit) { Has = false; return; }
        fs.Position = next;
        Id = r.ReadInt32();
        int len = r.ReadInt32();
        next += 8 + len;
        Has = true;
    }

    public void AdvanceTo(int id) { while (Has && Id < id) MoveNext(); }

    public void Apply(string[] cells, int[] map) {
        int n = r.ReadInt32();
        for (int i = 0; i < n; i++) { int c = r.ReadInt32(); cells[map[c]] = r.ReadString(); }
    }

    public void Dispose() { if (r != null) r.Dispose(); if (fs != null) fs.Dispose(); }
}

// Reads one sheet of an .xlsx row by row, straight from the zip, so a sheet of any size fits. Gives the same text
// as Upload and Compare get from EPPlus: numbers as stored (a date cell is its serial number), whole numbers without decimals, TRUE as True.
public sealed class SpCompareColumns {
    public string[] Ids; public string[] Refs; public string[] Types; public bool[][] Has; public List<int>[] Rows; public long[][] Keys;
}

// One cell value as a 64-bit key, so the values of a big workbook fit in memory:
//   0                  empty
//   DateFlag | ticks   a date/time (the text as written, no time zone applied)
//   bit 63 | canonical hash (30 bits) << 32 | exact hash (32 bits)   anything else
// "canonical" ignores what two scanners write differently for the same value: case, spaces,
// True/Yes, 1.0/1, claims prefixes (i:0#.w|), lookup ids (12;#Name) and ;# separators.
public static class SpValue {
    public const int Same = 0, SameFormat = 1, SameTimeZone = 2, BlankXlsx = 3, BlankCsv = 4, Different = 5, Kinds = 6;
    public static readonly string[] Names = { "Same", "Same, other format", "Same time, other time zone", "Blank in xlsx", "Blank in CSV", "Different" };
    const long DateFlag = 1L << 62;
    const long TickMask = DateFlag - 1;
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    static readonly string[] DateFormats = {
        "yyyy-MM-dd HH:mm:ss", "yyyy-MM-ddTHH:mm:ss", "yyyy-MM-dd HH:mm", "yyyy-MM-ddTHH:mm", "yyyy-MM-dd",
        "yyyy/MM/dd HH:mm:ss", "yyyy/MM/dd H:mm:ss", "yyyy/MM/dd HH:mm", "yyyy/MM/dd",
        "yyyy-MM-dd HH:mm:ss.fff", "yyyy-MM-ddTHH:mm:ss.fff", "yyyy-MM-ddTHH:mm:ss.fffffff",
        "M/d/yyyy h:mm:ss tt", "M/d/yyyy H:mm:ss", "M/d/yyyy H:mm", "M/d/yyyy", "d/M/yyyy H:mm:ss" };
    static readonly Regex Claims = new Regex(@"^i:0[#.][a-z+]\|", RegexOptions.CultureInvariant | RegexOptions.IgnoreCase);
    static readonly Regex LookupId = new Regex(@"(^|;#)\d+;#", RegexOptions.CultureInvariant);
    static readonly Regex Spaces = new Regex(@"\s+", RegexOptions.CultureInvariant);
    static readonly Regex Thousands = new Regex(@"^-?\d{1,3}(,\d{3})+(\.\d+)?$", RegexOptions.CultureInvariant);

    public static long Key(string s) {
        if (s == null) return 0;
        string t = s.Trim();
        if (t.Length == 0) return 0;
        long ticks;
        if (TryDate(t, out ticks)) return DateFlag | ticks;
        uint canon = Hash(Canonical(t)) & 0x3FFFFFFF;
        return long.MinValue | ((long)canon << 32) | (long)Hash(t);
    }

    public static long[] Keys(string[] values) {
        long[] k = new long[values.Length];
        for (int i = 0; i < values.Length; i++) k[i] = Key(values[i]);
        return k;
    }

    static bool TryDate(string t, out long ticks) {
        ticks = 0;
        if (t.Length < 8 || t.Length > 30 || !char.IsDigit(t[0])) return false;
        string u = t.EndsWith("Z") || t.EndsWith("z") ? t.Substring(0, t.Length - 1) : t;
        DateTime d;
        if (!DateTime.TryParseExact(u, DateFormats, Inv, DateTimeStyles.None, out d)) return false;
        ticks = d.Ticks;
        return true;
    }

    static string Canonical(string t) {
        string c = Claims.Replace(t, "");
        if (c.IndexOf(";#", StringComparison.Ordinal) >= 0) {
            c = LookupId.Replace(c, "$1");
            string[] parts = c.Split(new string[] { ";#" }, StringSplitOptions.RemoveEmptyEntries);
            for (int i = 0; i < parts.Length; i++) parts[i] = parts[i].Trim();
            c = string.Join(";", parts);
        }
        c = Spaces.Replace(c, " ").Trim().ToLowerInvariant();
        if (c == "true" || c == "yes") return "1";
        if (c == "false" || c == "no") return "0";
        decimal num;
        if (Thousands.IsMatch(c)) c = c.Replace(",", "");               // 1,440 -> 1440
        if (c.Length < 30 && decimal.TryParse(c, NumberStyles.Float, Inv, out num)) return num.ToString("G29", Inv);
        c = c.Replace("; ", ";");
        return c;
    }

    static uint Hash(string s) {                     // FNV-1a
        uint h = 2166136261;
        for (int i = 0; i < s.Length; i++) { h ^= s[i]; h *= 16777619; }
        return h;
    }

    public static bool IsDate(long k) { return k > 0 && (k & DateFlag) != 0; }

    // how the CSV value compares with the xlsx value; -1 when both are empty. shift: xlsx minus CSV, in minutes, for SameTimeZone.
    public static int Classify(long csv, long xlsx, out int shift) {
        shift = 0;
        if (csv == 0 && xlsx == 0) return -1;
        if (csv == 0) return BlankCsv;
        if (xlsx == 0) return BlankXlsx;
        if (csv == xlsx) return Same;
        bool dc = IsDate(csv), dx = IsDate(xlsx);
        if (dc && dx) {
            long diff = (xlsx & TickMask) - (csv & TickMask);
            long quarter = TimeSpan.TicksPerMinute * 15;
            if (diff % quarter == 0 && Math.Abs(diff) <= TimeSpan.TicksPerHour * 14) { shift = (int)(diff / TimeSpan.TicksPerMinute); return SameTimeZone; }
            return Different;
        }
        if (!dc && !dx && ((csv >> 32) & 0x3FFFFFFF) == ((xlsx >> 32) & 0x3FFFFFFF)) return SameFormat;
        return Different;
    }
}

public sealed class SpValueDiff {
    public int Kind; public int Col; public int Lib; public int Raw; public long Line; public string File; public string Ref; public string Csv; public string Xlsx;
}

// The value check of one compare: counts per column and per library, and a capped list of cells that differ.
public sealed class SpValueCompare {
    public readonly string[] Names;          // the compared columns
    public readonly long[][] Wb;             // per column: the workbook key of each workbook row
    public readonly long[,] Totals;          // [column, kind]
    public readonly Dictionary<int, long[,]> ByLib = new Dictionary<int, long[,]>();
    public readonly Dictionary<int, int>[] Shifts;   // per column: minutes -> cells
    public readonly List<SpValueDiff> Diffs = new List<SpValueDiff>();
    public readonly int[] Listed;            // per column: cells in Diffs
    public long Skipped;                     // differing cells not listed (caps)
    public int MaxPerColumn = 2000, MaxTotal = 200000;

    public SpValueCompare(string[] names, long[][] wb) {
        Names = names; Wb = wb;
        Totals = new long[names.Length, SpValue.Kinds];
        Shifts = new Dictionary<int, int>[names.Length];
        for (int j = 0; j < names.Length; j++) Shifts[j] = new Dictionary<int, int>();
        Listed = new int[names.Length];
    }

    // csvIdx[j]: the position of column j in this CSV, or -1
    public void Row(string[] f, int[] csvIdx, int raw, int lib, string fileRef, string file, long line) {
        long[,] mine;
        if (!ByLib.TryGetValue(lib, out mine)) { mine = new long[Names.Length, SpValue.Kinds]; ByLib[lib] = mine; }
        for (int j = 0; j < Names.Length; j++) {
            int c = csvIdx[j];
            if (c < 0) continue;
            string text = c < f.Length ? f[c] : "";
            int shift;
            int k = SpValue.Classify(SpValue.Key(text), Wb[j][raw], out shift);
            if (k < 0) continue;
            Totals[j, k]++; mine[j, k]++;
            if (k == SpValue.SameTimeZone) { int n; Shifts[j].TryGetValue(shift, out n); Shifts[j][shift] = n + 1; }
            if (k >= SpValue.BlankXlsx) {
                if (Listed[j] < MaxPerColumn && Diffs.Count < MaxTotal) {
                    SpValueDiff d = new SpValueDiff(); d.Kind = k; d.Col = j; d.Lib = lib; d.Raw = raw; d.Line = line; d.File = file; d.Ref = fileRef; d.Csv = text;
                    Diffs.Add(d); Listed[j]++;
                } else Skipped++;
            }
        }
    }

    // "xlsx +8h (100%)": the most common time shift of a column
    public string ShiftText(int j) {
        int best = 0, bestN = 0, all = 0;
        foreach (KeyValuePair<int, int> p in Shifts[j]) { all += p.Value; if (p.Value > bestN) { best = p.Key; bestN = p.Value; } }
        if (all == 0) return "";
        string h = (best % 60 == 0) ? (best / 60).ToString("+0;-0", Inv) + "h" : (best / 60.0).ToString("+0.##;-0.##", Inv) + "h";
        return "xlsx " + h + " (" + (100.0 * bestN / all).ToString("0", Inv) + "%)";
    }
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
}

public sealed class SpSheetReader : IDisposable {
    const string MainNs = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    static readonly Regex Code = new Regex("_x([0-9A-Fa-f]{4})_", RegexOptions.CultureInvariant);
    readonly FileStream fs;
    readonly ZipArchive zip;
    readonly Stream sheetStream;
    readonly XmlReader x;
    readonly List<string> shared = new List<string>();
    readonly List<string> cells = new List<string>();
    public string SheetName;

    public SpSheetReader(string path, string sheet) {
        fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite, 262144);
        try {
            zip = new ZipArchive(fs, ZipArchiveMode.Read);
            Dictionary<string, string> rels = Relations(Find("xl/_rels/workbook.xml.rels"));
            string target = FindSheet(sheet, rels);
            foreach (KeyValuePair<string, string> r in rels) {
                if (r.Key.EndsWith("/sharedStrings", StringComparison.OrdinalIgnoreCase)) LoadShared(Find(PartName(r.Value)));
            }
            sheetStream = Find(PartName(target)).Open();
            x = NewXml(sheetStream);
        } catch { Dispose(); throw; }
    }

    public static bool HasSheet(string path, string sheet) {
        using (FileStream f = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        using (ZipArchive z = new ZipArchive(f, ZipArchiveMode.Read))
        using (XmlReader r = NewXml(FindIn(z, "xl/workbook.xml").Open())) {
            while (r.Read()) if (r.NodeType == XmlNodeType.Element && r.LocalName == "sheet" && r.GetAttribute("name") == sheet) return true;
        }
        return false;
    }

    static XmlReader NewXml(Stream stream) {
        XmlReaderSettings set = new XmlReaderSettings();
        set.IgnoreComments = true; set.IgnoreProcessingInstructions = true; set.CheckCharacters = false;
        set.DtdProcessing = DtdProcessing.Prohibit; set.XmlResolver = null; set.CloseInput = true;
        return XmlReader.Create(stream, set);
    }

    static ZipArchiveEntry FindIn(ZipArchive z, string name) {
        ZipArchiveEntry e = z.GetEntry(name);
        if (e != null) return e;
        foreach (ZipArchiveEntry a in z.Entries) if (string.Equals(a.FullName, name, StringComparison.OrdinalIgnoreCase)) return a;
        throw new InvalidDataException("This .xlsx has no " + name);
    }

    ZipArchiveEntry Find(string name) { return FindIn(zip, name); }

    static string PartName(string target) { return target.StartsWith("/") ? target.Substring(1) : "xl/" + target; }

    // relationship type -> target (shared strings); id -> target for sheets
    Dictionary<string, string> Relations(ZipArchiveEntry e) {
        Dictionary<string, string> rels = new Dictionary<string, string>();
        using (XmlReader r = NewXml(e.Open())) {
            while (r.Read()) {
                if (r.NodeType != XmlNodeType.Element || r.LocalName != "Relationship") continue;
                string type = r.GetAttribute("Type") ?? "", target = r.GetAttribute("Target") ?? "", id = r.GetAttribute("Id") ?? "";
                if (type.EndsWith("/worksheet", StringComparison.OrdinalIgnoreCase)) rels["id:" + id] = target;
                else rels[type] = target;
            }
        }
        return rels;
    }

    string FindSheet(string sheet, Dictionary<string, string> rels) {
        using (XmlReader r = NewXml(Find("xl/workbook.xml").Open())) {
            while (r.Read()) {
                if (r.NodeType != XmlNodeType.Element || r.LocalName != "sheet") continue;
                string name = r.GetAttribute("name");
                if (!string.IsNullOrEmpty(sheet) && name != sheet) continue;
                string target;
                string id = r.GetAttribute("id", "http://schemas.openxmlformats.org/officeDocument/2006/relationships");
                if (id == null || !rels.TryGetValue("id:" + id, out target)) throw new InvalidDataException("Sheet '" + name + "' cannot be found in this .xlsx");
                SheetName = name;
                return target;
            }
        }
        throw new InvalidDataException(string.IsNullOrEmpty(sheet) ? "This .xlsx has no sheets" : "Sheet '" + sheet + "' not found");
    }

    static string Decode(string v) {
        if (v.IndexOf("_x", StringComparison.Ordinal) < 0) return v;
        return Code.Replace(v, delegate(Match m) { return ((char)int.Parse(m.Groups[1].Value, NumberStyles.HexNumber, Inv)).ToString(); });
    }

    // text of the current element, leaving the reader on its end tag
    static string ElementText(XmlReader r) {
        if (r.IsEmptyElement) return "";
        string first = null; StringBuilder more = null;
        int depth = r.Depth;
        while (r.Read()) {
            if (r.NodeType == XmlNodeType.EndElement && r.Depth == depth) break;
            if (r.NodeType == XmlNodeType.Text || r.NodeType == XmlNodeType.CDATA || r.NodeType == XmlNodeType.Whitespace || r.NodeType == XmlNodeType.SignificantWhitespace) {
                if (first == null) first = r.Value;
                else { if (more == null) more = new StringBuilder(first); more.Append(r.Value); }
            }
        }
        return more != null ? more.ToString() : first ?? "";
    }

    // <si> or <is>: its <t>, or the <t> of each rich text run; phonetic hints are not part of the text
    static string RichText(XmlReader r) {
        if (r.IsEmptyElement) return "";
        string one = null; StringBuilder more = null;
        int depth = r.Depth;
        while (r.Read()) {
            if (r.NodeType == XmlNodeType.EndElement && r.Depth == depth) break;
            if (r.NodeType != XmlNodeType.Element) continue;
            if (r.LocalName == "rPh" && !r.IsEmptyElement) {
                int ph = r.Depth;
                while (r.Read() && !(r.NodeType == XmlNodeType.EndElement && r.Depth == ph)) { }
            } else if (r.LocalName == "t") {
                string t = ElementText(r);
                if (one == null) one = t;
                else { if (more == null) more = new StringBuilder(one); more.Append(t); }
            }
        }
        return Decode(more != null ? more.ToString() : one ?? "");
    }

    void LoadShared(ZipArchiveEntry e) {
        using (XmlReader r = NewXml(e.Open())) {
            while (r.Read()) if (r.NodeType == XmlNodeType.Element && r.LocalName == "si") shared.Add(RichText(r));
        }
    }

    static int ColumnOf(string reference) {
        int col = 0;
        foreach (char c in reference) {
            if (c >= 'A' && c <= 'Z') col = col * 26 + c - 'A' + 1;
            else if (c >= 'a' && c <= 'z') col = col * 26 + c - 'a' + 1;
            else break;
        }
        return col - 1;
    }

    static string Number(string v) {
        double d;
        if (!double.TryParse(v, NumberStyles.Float, Inv, out d)) return v;
        if (Math.Floor(d) == d && !double.IsInfinity(d)) return d.ToString("0", Inv);
        return d.ToString("R", Inv);
    }

    string CellValue() {
        string type = x.GetAttribute("t");
        if (x.IsEmptyElement) return "";
        string raw = null, rich = null;
        int depth = x.Depth;
        while (x.Read()) {
            if (x.NodeType == XmlNodeType.EndElement && x.Depth == depth) break;
            if (x.NodeType != XmlNodeType.Element) continue;
            if (x.LocalName == "v") raw = ElementText(x);
            else if (x.LocalName == "is") rich = RichText(x);
            else if (!x.IsEmptyElement) { int d = x.Depth; while (x.Read() && !(x.NodeType == XmlNodeType.EndElement && x.Depth == d)) { } }
        }
        string v;
        switch (type) {
            case "inlineStr": v = rich ?? ""; break;
            case "s": {
                int i;
                v = raw != null && int.TryParse(raw, NumberStyles.Integer, Inv, out i) && i >= 0 && i < shared.Count ? shared[i] : "";
                break;
            }
            case "str": v = Decode(raw ?? ""); break;
            case "b": v = raw == "1" ? "True" : raw == null ? "" : "False"; break;
            case "e": v = raw ?? ""; break;
            default: v = raw == null ? "" : Number(raw); break;
        }
        return v.IndexOf('\0') >= 0 ? v.Replace("\0", "") : v;
    }

    // next row with a value, as text: width cells, or as many as the row has when width is 0. Null at the end of the sheet.
    public string[] ReadRow(int width) {
        while (x.Read()) {
            if (x.NodeType != XmlNodeType.Element || x.LocalName != "row") continue;
            cells.Clear();
            bool any = false;
            if (!x.IsEmptyElement) {
                int depth = x.Depth, next = 0;
                while (x.Read()) {
                    if (x.NodeType == XmlNodeType.EndElement && x.Depth == depth) break;
                    if (x.NodeType != XmlNodeType.Element || x.LocalName != "c") continue;
                    string reference = x.GetAttribute("r");
                    int col = reference != null ? ColumnOf(reference) : next;
                    next = col + 1;
                    string v = CellValue();
                    if (v.Length == 0 || (width > 0 && col >= width)) continue;
                    while (cells.Count <= col) cells.Add("");
                    cells[col] = v;
                    any = true;
                }
            }
            if (!any) continue;
            if (width > 0) while (cells.Count < width) cells.Add("");
            return cells.ToArray();
        }
        return null;
    }

    // what Compare needs of every remaining row. keep: the columns to record which rows have a value.
    // valueCols (may be empty): the columns whose values are compared; each cell is kept as an SpValue key (8 bytes).
    public SpCompareColumns ReadCompare(int width, int iId, int iRef, int iType, int[] keep, int[] valueCols, Action<long> progress) {
        List<string> ids = new List<string>(), refs = new List<string>(), types = new List<string>();
        List<bool>[] has = new List<bool>[keep.Length];
        List<int>[] rows = new List<int>[keep.Length];
        for (int j = 0; j < keep.Length; j++) { has[j] = new List<bool>(); rows[j] = new List<int>(); }
        if (valueCols == null) valueCols = new int[0];
        List<long>[] keys = new List<long>[valueCols.Length];
        for (int j = 0; j < valueCols.Length; j++) keys[j] = new List<long>();
        int n = 0;
        string[] r;
        while ((r = ReadRow(width)) != null) {
            ids.Add(iId >= 0 ? r[iId] : null);
            refs.Add(iRef >= 0 ? r[iRef] : null);
            types.Add(iType >= 0 ? r[iType] : null);
            for (int j = 0; j < keep.Length; j++) {
                bool on = r[keep[j]].Length > 0;
                has[j].Add(on);
                if (on) rows[j].Add(n);
            }
            for (int j = 0; j < valueCols.Length; j++) keys[j].Add(SpValue.Key(r[valueCols[j]]));
            n++;
            if (progress != null && n % 5000 == 0) progress(n);
        }
        SpCompareColumns c = new SpCompareColumns();
        c.Ids = ids.ToArray(); c.Refs = refs.ToArray(); c.Types = types.ToArray(); c.Rows = rows;
        c.Has = new bool[keep.Length][];
        for (int j = 0; j < keep.Length; j++) { c.Has[j] = has[j].ToArray(); has[j] = null; }
        c.Keys = new long[valueCols.Length][];
        for (int j = 0; j < valueCols.Length; j++) { c.Keys[j] = keys[j].ToArray(); keys[j] = null; }
        return c;
    }

    // the text of some cells, read again row by row: wanted row (as counted by ReadCompare) -> values of cols.
    // Call right after the header row was read. Stops after the last wanted row.
    public Dictionary<int, string[]> ReadRowsAt(int width, ICollection<int> wanted, int[] cols) {
        Dictionary<int, string[]> out1 = new Dictionary<int, string[]>();
        HashSet<int> want = new HashSet<int>(wanted);
        int last = -1;
        foreach (int w in want) if (w > last) last = w;
        int n = 0;
        string[] r;
        while (n <= last && (r = ReadRow(width)) != null) {
            if (want.Contains(n)) {
                string[] v = new string[cols.Length];
                for (int j = 0; j < cols.Length; j++) v[j] = cols[j] >= 0 && cols[j] < r.Length ? r[cols[j]] : "";
                out1[n] = v;
            }
            n++;
        }
        return out1;
    }

    public void Dispose() {
        if (x != null) x.Dispose();
        if (sheetStream != null) sheetStream.Dispose();
        if (zip != null) zip.Dispose();
        fs.Dispose();
    }
}

public static class SpEngine {
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    static readonly Regex IdPrefix = new Regex(@"^\d+;#", RegexOptions.CultureInvariant);

    public static SpPick MainPick(int index, string text, string raw, bool date) {
        SpPick p = new SpPick(); p.Index = index; p.Text = text; p.Raw = raw; p.Date = date; return p;
    }
    public static SpPick ExtraPick(int index, string one, string[] path, bool date) {
        SpPick p = new SpPick(); p.Index = index; p.One = one; p.Path = path; p.Date = date; return p;
    }

    // ---- values: the text SharePoint shows
    public static string FormatDate(object value) {
        DateTime d;
        if (value is DateTime) {
            d = (DateTime)value;
            if (d.Kind != DateTimeKind.Local) d = DateTime.SpecifyKind(d, DateTimeKind.Utc).ToLocalTime();
        } else {
            string s = value as string;
            if (string.IsNullOrEmpty(s)) return "";
            DateTimeOffset o;
            if (!DateTimeOffset.TryParse(s, Inv, DateTimeStyles.AssumeUniversal, out o)) return s;
            d = o.LocalDateTime;
        }
        return d.ToString("yyyy-MM-dd HH:mm:ss", Inv);
    }

    public static string FormatValue(object value) {
        if (value == null) return "";
        string s = value as string;
        if (s == null) {
            if (value is DateTime) s = FormatDate(value);
            else {
                IDictionary dict = value as IDictionary;
                if (dict != null) return dict.Contains("StringValue") ? Convert.ToString(dict["StringValue"], Inv) ?? "" : "";   // ContentTypeId
                if (value is Array) return "";
                s = Convert.ToString(value, Inv) ?? "";
            }
        }
        return s.IndexOf('\0') >= 0 ? s.Replace("\0", "") : s;
    }

    static string JoinResults(object results, string key) {
        StringBuilder b = new StringBuilder();
        bool first = true;
        IEnumerable items = results as IEnumerable;
        if (items == null || results is string) return "";
        foreach (object e in items) {
            string v;
            if (key == null) v = FormatValue(e);
            else {
                IDictionary d = e as IDictionary;
                if (d == null || !d.Contains(key)) continue;
                v = FormatValue(d[key]);
            }
            if (!first) b.Append("; ");
            b.Append(v);
            first = false;
        }
        return b.ToString();
    }

    public static object PathValue(object item, string[] path) {
        object v = item;
        foreach (string k in path) {
            IDictionary d = v as IDictionary;
            if (d == null) return null;
            if (d.Contains("results")) return JoinResults(d["results"], k);
            if (!d.Contains(k)) return null;
            v = d[k];
        }
        IDictionary end = v as IDictionary;
        if (end != null && end.Contains("results")) return JoinResults(end["results"], null);
        return v;
    }

    static int ItemId(IDictionary it) {
        object id = it.Contains("Id") ? it["Id"] : it["ID"];
        return Convert.ToInt32(id, Inv);
    }

    static void Put(SpPass pass, int col, string v) {
        if (v.Length == 0) return;
        pass.Cols.Add(col);
        pass.Vals.Add(v);
    }

    // ---- reading: one page of the main pass
    public static void AddMainPage(SpPass pass, IEnumerable results, string authority, SpPick[] picks, int libraryCol, string libraryTitle, int libraryIdCol, string libraryId) {
        if (results == null) return;
        foreach (object o in results) {
            IDictionary it = (IDictionary)o;
            pass.Items++;
            if (Convert.ToString(it["FSObjType"], Inv) == "1") { pass.Folders++; continue; }
            IDictionary t = it["FieldValuesAsText"] as IDictionary;
            string fileRef = FormatValue(it["FileRef"]);
            Put(pass, 0, authority + fileRef);
            Put(pass, 1, FormatValue(it["UniqueId"]));
            Put(pass, 2, fileRef);
            Put(pass, 3, FormatValue(it["FileLeafRef"]));
            Put(pass, 4, FormatValue(it["FileDirRef"]));
            Put(pass, 5, FormatValue(it["FSObjType"]));
            if (libraryCol >= 0) Put(pass, libraryCol, libraryTitle);
            if (libraryIdCol >= 0) Put(pass, libraryIdCol, libraryId);
            foreach (SpPick p in picks) {
                string v;
                if (p.Date && it.Contains(p.Raw)) v = FormatDate(it[p.Raw]);
                else if (t != null && t.Contains(p.Text)) v = FormatValue(t[p.Text]);
                else v = FormatValue(it[p.Raw]);
                Put(pass, p.Index, v);
            }
            pass.Files++;
            pass.Add(ItemId(it));
        }
    }

    // ---- reading: one page of an extra pass (a folder comes back too; the merge drops it, as it has no main row)
    public static void AddExtraPage(SpPass pass, IEnumerable results, SpPick[] picks) {
        if (results == null) return;
        foreach (object o in results) {
            IDictionary it = (IDictionary)o;
            pass.Items++;
            foreach (SpPick p in picks) {
                object v = string.IsNullOrEmpty(p.One) ? PathValue(it, p.Path) : it[p.One];
                if (v == null) continue;
                IDictionary d = v as IDictionary;
                if (d != null && d.Contains("results")) v = PathValue(it, p.Path);
                string s = v as string;
                if (s != null && s.Contains(";#")) v = IdPrefix.Replace(s, "");
                Put(pass, p.Index, p.Date ? FormatDate(v) : FormatValue(v));
            }
            pass.Add(ItemId(it));
        }
    }

    // ---- merge: every library in order, each row once, with the extra passes laid over the main pass
    static long Merge(SpLibSpool[] libs, int width, Action<string[]> row, Action<long> progress) {
        string[] cells = new string[width];
        long n = 0;
        foreach (SpLibSpool lib in libs) {
            using (SpReader main = new SpReader(lib.Main)) {
                List<SpReader> extras = new List<SpReader>();
                try {
                    foreach (string e in lib.Extras) extras.Add(new SpReader(e));
                    while (main.Has) {
                        Array.Clear(cells, 0, width);
                        main.Apply(cells, lib.Map);
                        foreach (SpReader x in extras) {
                            x.AdvanceTo(main.Id);
                            if (x.Has && x.Id == main.Id) x.Apply(cells, lib.Map);
                        }
                        row(cells);
                        n++;
                        if (progress != null && n % 5000 == 0) progress(n);
                        main.MoveNext();
                    }
                } finally {
                    foreach (SpReader x in extras) x.Dispose();
                }
            }
        }
        return n;
    }

    // ---- csv: UTF-8 with BOM, every value quoted
    static void CsvLine(StreamWriter w, string[] cells) {
        for (int i = 0; i < cells.Length; i++) {
            if (i > 0) w.Write(',');
            w.Write('"');
            string v = cells[i];
            if (v != null) { if (v.IndexOf('"') >= 0) w.Write(v.Replace("\"", "\"\"")); else w.Write(v); }
            w.Write('"');
        }
        w.Write("\r\n");
    }

    public static long WriteCsv(string path, string[] columns, SpLibSpool[] libs, Action<long> progress) {
        using (StreamWriter w = new StreamWriter(path, false, new UTF8Encoding(true, false), 262144)) {
            CsvLine(w, columns);
            return Merge(libs, columns.Length, delegate(string[] c) { CsvLine(w, c); }, progress);
        }
    }

    // ---- xlsx: written straight into the zip, so no copy of the sheet is held in memory. Text is stored in each
    // cell (inline), not in a shared table, for the same reason.
    const string Head = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n";
    const string MainNs = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
    const string OdRel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";

    static string ColName(int zeroBased) {
        string s = "";
        for (int n = zeroBased + 1; n > 0; n = (n - 1) / 26) s = (char)('A' + (n - 1) % 26) + s;
        return s;
    }

    static string[] ColNames(int width) {
        string[] names = new string[width];
        for (int i = 0; i < width; i++) names[i] = ColName(i);
        return names;
    }

    static bool IsHex(char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'); }

    static void XmlText(StreamWriter w, string v) {
        int start = 0;
        for (int i = 0; i < v.Length; i++) {
            char c = v[i];
            string rep = null;
            if (c == '&') rep = "&amp;";
            else if (c == '<') rep = "&lt;";
            else if (c == '>') rep = "&gt;";
            else if (c == '\r' || c < ' ' && c != '\t' && c != '\n' || c == '\uFFFE' || c == '\uFFFF') rep = "_x" + ((int)c).ToString("X4", Inv) + "_";
            else if (c == '_' && i + 6 < v.Length && v[i + 1] == 'x' && v[i + 6] == '_' && IsHex(v[i + 2]) && IsHex(v[i + 3]) && IsHex(v[i + 4]) && IsHex(v[i + 5])) rep = "_x005F_";   // literal text that looks like an Excel code
            else if (char.IsHighSurrogate(c)) { if (i + 1 < v.Length && char.IsLowSurrogate(v[i + 1])) { i++; continue; } rep = "\uFFFD"; }
            else if (char.IsLowSurrogate(c)) rep = "\uFFFD";
            if (rep == null) continue;
            if (i > start) w.Write(v.Substring(start, i - start));
            w.Write(rep);
            start = i + 1;
        }
        if (start == 0) w.Write(v);
        else if (start < v.Length) w.Write(v.Substring(start));
    }

    static void XmlRow(StreamWriter w, string[] cells, long rowNo, string[] names, string style, int maxText) {
        string row = rowNo.ToString(Inv);
        w.Write("<row r=\""); w.Write(row); w.Write("\">");
        for (int i = 0; i < cells.Length; i++) {
            string v = cells[i];
            if (string.IsNullOrEmpty(v)) continue;
            if (v.Length > maxText) v = v.Substring(0, maxText);
            w.Write("<c r=\""); w.Write(names[i]); w.Write(row); w.Write(style); w.Write("\" t=\"inlineStr\"><is><t");
            if (char.IsWhiteSpace(v[0]) || char.IsWhiteSpace(v[v.Length - 1])) w.Write(" xml:space=\"preserve\"");
            w.Write(">");
            XmlText(w, v);
            w.Write("</t></is></c>");
        }
        w.Write("</row>");
    }

    static StreamWriter Part(ZipArchive zip, string name) {
        return new StreamWriter(zip.CreateEntry(name, CompressionLevel.Fastest).Open(), SpPass.Utf8, 262144);
    }

    static void SmallPart(ZipArchive zip, string name, string text) {
        using (StreamWriter w = Part(zip, name)) { w.Write(Head); w.Write(text); }
    }

    static void SheetStart(StreamWriter w, bool selected, string cols) {
        w.Write(Head);
        w.Write("<worksheet xmlns=\"" + MainNs + "\"><sheetViews><sheetView workbookViewId=\"0\"" + (selected ? " tabSelected=\"1\"" : "") + ">");
        w.Write("<pane ySplit=\"1\" topLeftCell=\"A2\" activePane=\"bottomLeft\" state=\"frozen\"/></sheetView></sheetViews>");
        w.Write(cols);
        w.Write("<sheetData>");
    }

    static string XmlAttr(string s) { return s.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace("\"", "&quot;"); }

    public static long WriteXlsx(string path, string[] columns, SpLibSpool[] libs, string reportSheet, string[] reportHeader, IEnumerable reportRows, int maxText, Action<long> progress) {
        using (FileStream fs = new FileStream(path, FileMode.Create, FileAccess.ReadWrite, FileShare.None, 262144))
        using (ZipArchive zip = new ZipArchive(fs, ZipArchiveMode.Create, true)) {
            SmallPart(zip, "[Content_Types].xml",
                "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">" +
                "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>" +
                "<Default Extension=\"xml\" ContentType=\"application/xml\"/>" +
                "<Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>" +
                "<Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>" +
                "<Override PartName=\"/xl/worksheets/sheet2.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>" +
                "<Override PartName=\"/xl/styles.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml\"/></Types>");
            SmallPart(zip, "_rels/.rels",
                "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">" +
                "<Relationship Id=\"rId1\" Type=\"" + OdRel + "/officeDocument\" Target=\"xl/workbook.xml\"/></Relationships>");
            SmallPart(zip, "xl/_rels/workbook.xml.rels",
                "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">" +
                "<Relationship Id=\"rId1\" Type=\"" + OdRel + "/worksheet\" Target=\"worksheets/sheet1.xml\"/>" +
                "<Relationship Id=\"rId2\" Type=\"" + OdRel + "/worksheet\" Target=\"worksheets/sheet2.xml\"/>" +
                "<Relationship Id=\"rId3\" Type=\"" + OdRel + "/styles\" Target=\"styles.xml\"/></Relationships>");
            SmallPart(zip, "xl/styles.xml",
                "<styleSheet xmlns=\"" + MainNs + "\"><fonts count=\"2\"><font><sz val=\"11\"/><name val=\"Calibri\"/></font><font><b/><sz val=\"11\"/><name val=\"Calibri\"/></font></fonts>" +
                "<fills count=\"2\"><fill><patternFill patternType=\"none\"/></fill><fill><patternFill patternType=\"gray125\"/></fill></fills>" +
                "<borders count=\"1\"><border><left/><right/><top/><bottom/><diagonal/></border></borders>" +
                "<cellStyleXfs count=\"1\"><xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\"/></cellStyleXfs>" +
                "<cellXfs count=\"2\"><xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\" xfId=\"0\"/><xf numFmtId=\"0\" fontId=\"1\" fillId=\"0\" borderId=\"0\" xfId=\"0\" applyFont=\"1\"/></cellXfs>" +
                "<cellStyles count=\"1\"><cellStyle name=\"Normal\" xfId=\"0\" builtinId=\"0\"/></cellStyles></styleSheet>");

            // sheet 1: the files
            string[] names = ColNames(columns.Length);
            long rows;
            long rowNo = 1;
            using (StreamWriter w = Part(zip, "xl/worksheets/sheet1.xml")) {
                SheetStart(w, true, "");
                XmlRow(w, columns, rowNo, names, "\" s=\"1", maxText);
                rows = Merge(libs, columns.Length, delegate(string[] c) { rowNo++; XmlRow(w, c, rowNo, names, "", maxText); }, progress);
                w.Write("</sheetData><autoFilter ref=\"A1:" + names[names.Length - 1] + (rows + 1).ToString(Inv) + "\"/></worksheet>");
            }

            // sheet 2: each library's fields
            string[] reportNames = ColNames(reportHeader.Length);
            long reportCount = 1;
            using (StreamWriter w = Part(zip, "xl/worksheets/sheet2.xml")) {
                StringBuilder cols = new StringBuilder("<cols>");
                for (int i = 0; i < reportHeader.Length; i++) {
                    string no = (i + 1).ToString(Inv);
                    cols.Append("<col min=\"" + no + "\" max=\"" + no + "\" width=\"" + Math.Max(14, reportHeader[i].Length + 4).ToString(Inv) + "\" customWidth=\"1\"/>");
                }
                cols.Append("</cols>");
                SheetStart(w, false, cols.ToString());
                XmlRow(w, reportHeader, reportCount, reportNames, "\" s=\"1", maxText);
                string[] cells = new string[reportHeader.Length];
                foreach (object r in reportRows) {
                    Array.Clear(cells, 0, cells.Length);
                    int i = 0;
                    foreach (object v in (IEnumerable)r) { if (i < cells.Length) cells[i] = Convert.ToString(v, Inv); i++; }
                    reportCount++;
                    XmlRow(w, cells, reportCount, reportNames, "", maxText);
                }
                w.Write("</sheetData><autoFilter ref=\"A1:" + reportNames[reportNames.Length - 1] + reportCount.ToString(Inv) + "\"/></worksheet>");
            }

            string sheet2 = "'" + reportSheet.Replace("'", "''") + "'";
            SmallPart(zip, "xl/workbook.xml",
                "<workbook xmlns=\"" + MainNs + "\" xmlns:r=\"" + OdRel + "\"><bookViews><workbookView/></bookViews><sheets>" +
                "<sheet name=\"Metadata\" sheetId=\"1\" r:id=\"rId1\"/><sheet name=\"" + XmlAttr(reportSheet) + "\" sheetId=\"2\" r:id=\"rId2\"/></sheets><definedNames>" +
                "<definedName name=\"_xlnm._FilterDatabase\" localSheetId=\"0\" hidden=\"1\">Metadata!$A$1:$" + names[names.Length - 1] + "$" + (rows + 1).ToString(Inv) + "</definedName>" +
                "<definedName name=\"_xlnm._FilterDatabase\" localSheetId=\"1\" hidden=\"1\">" + XmlAttr(sheet2) + "!$A$1:$" + reportNames[reportNames.Length - 1] + "$" + reportCount.ToString(Inv) + "</definedName>" +
                "</definedNames></workbook>");
            return rows;
        }
    }
}
}
'@

function Initialize-SpEngine {
    if ('MetadataHorizontal.SpEngine' -as [type]) { return }
    # PowerShell 7 references the whole runtime by default; 5.1 needs the zip library named
    $refs = @{}
    if ($PSVersionTable.PSVersion.Major -lt 6) { $refs.ReferencedAssemblies = 'System.IO.Compression', 'System.Xml' }
    try { Add-Type -TypeDefinition $script:SpEngineSource -ErrorAction Stop @refs }
    catch { throw "The export could not compile its fast part on this PC: $($_.Exception.Message)" }
}

function Test-SpBigXlsx([string] $Path) {
    <# Is this .xlsx big enough, once unzipped, that it must be read row by row? A file that is no zip is left for EPPlus to report. #>
    try {
        Add-Type -AssemblyName System.IO.Compression
        $f = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $z = New-Object IO.Compression.ZipArchive($f, [IO.Compression.ZipArchiveMode]::Read)
            try {
                foreach ($e in $z.Entries) {
                    if ($e.FullName -match '^xl/(worksheets/|sharedStrings\.xml$)' -and $e.Length -ge $script:RowByRowBytes) { return $true }
                }
                return $false
            } finally { $z.Dispose() }
        } finally { $f.Dispose() }
    } catch { return $false }
}

function Open-SpSheetReader([string] $Path, [string] $Sheet) {
    <# Row by row reader of one sheet (default: the first): .ReadRow(width), .SheetName, .Dispose(). #>
    Initialize-SpEngine
    New-Object MetadataHorizontal.SpSheetReader($Path, $Sheet)
}

function Get-SpWorkDir([string] $Path, $Libraries) {
    <# Work folder of an export: next to the output file, and the same for the same libraries in the same order. #>
    $key = (@($Libraries | ForEach-Object { "$($_.WebUrl)|$($_.Id)" }) -join "`n").ToLowerInvariant()
    $sha = [Security.Cryptography.SHA1]::Create()
    try { $hash = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($key))[0..7] | ForEach-Object { $_.ToString('x2') }) }
    finally { $sha.Dispose() }
    Join-Path (Join-Path ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))) $script:WorkFolderName) $hash
}

function Get-SpUnfinished([string] $Path, $Libraries) {
    <# The unfinished export of these libraries that this output folder holds -> @{ Saved; Items; Folder }, or nothing. #>
    $work = Get-SpWorkDir $Path $Libraries
    if (-not (Test-Path -LiteralPath $work)) { return }
    $marks = @(Get-ChildItem -LiteralPath $work -Filter '*.chk' -File -ErrorAction SilentlyContinue)
    if (-not $marks.Count) { return }
    $saved = ($marks | Measure-Object LastWriteTime -Maximum).Maximum
    if ($saved -lt (Get-Date).AddDays(-$script:WorkMaxDays)) { return }
    $items = [long]0
    foreach ($m in $marks) {
        if ($m.Name -notmatch '\.main\.chk$') { continue }
        $items += [long](Get-Content -LiteralPath $m.FullName -TotalCount 5)[4]
    }
    @{ Saved = $saved; Items = $items; Folder = $work }
}

function Remove-SpOldWork([string] $Root) {
    <# Work folders left by exports nobody continued. #>
    foreach ($d in @(Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue)) {
        if ($d.LastWriteTime -lt (Get-Date).AddDays(-$script:WorkMaxDays)) { Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Get-SpLibraryPlan {
    <# What to read from one library: columns (front columns first), main pass picks, extra passes, its row in Library columns. #>
    param([hashtable] $Sp, [hashtable] $Lib, [string] $Name, [hashtable] $Remember, $NotRead, [scriptblock] $Log)
    $listApi = "$($Lib.WebUrl)/_api/web/lists(guid'$($Lib.Id)')"
    $fields = (Invoke-SpGet $Sp "$listApi/fields?`$select=InternalName,Title,TypeAsString,Hidden,ReadOnlyField")['results']
    # fields $select=* gives correctly stay in the main pass; the others go in extra passes
    $listed = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $wanted = New-Object 'Collections.Generic.List[object]'
    $lookups = New-Object 'Collections.Generic.List[object]'
    foreach ($f in $fields) {
        $n = [string]$f['InternalName']
        if (-not $listed.Add($n) -or $script:FrontColumns -ccontains $n) { continue }
        $raw = if ($n.StartsWith('_')) { 'OData_' + $n } else { $n }
        $type = [string]$f['TypeAsString']
        $main = ($script:AlwaysFields -ccontains $n) -or (-not $f['Hidden'] -and $type -ne 'Computed')
        $direct = @{ Select = $raw; Expand = $null; Path = [string[]]@($raw); Label = $raw }
        if ($type -in 'User', 'UserMulti') {
            $wanted.Add(@{ Name = $n; Options = @(New-SpFieldOption "$raw/Title"); Sticky = -not $main })
        } elseif ($main -and $type -in 'Lookup', 'LookupMulti') {
            if ($f['ReadOnlyField']) { continue }
            $target = 'Title'
            try {
                $one = Invoke-SpGet $Sp "$listApi/fields/getbyinternalnameortitle('$n')?`$select=LookupField"
                if ($one['LookupField']) { $target = [string]$one['LookupField'] }
            } catch { }
            $lookups.Add(@{ Name = $n; Options = @(New-SpFieldOption "$raw/$target"); Sticky = $false })
        } elseif (-not $main) {
            $opts = @($direct)
            if ($script:HiddenFields.Contains($n)) { $opts += New-SpFieldOption $script:HiddenFields[$n] }
            elseif ($type -in 'Lookup', 'LookupMulti') { $opts += New-SpFieldOption "$raw/Title" }
            $wanted.Add(@{ Name = $n; Options = $opts; Sticky = $true })
        }
    }
    $wanted.AddRange($lookups)
    $groups = Group-SpFieldOptions $Sp $listApi $wanted $Remember
    $chosen = @{}
    foreach ($g in $groups) { foreach ($c in $g) { $chosen[$c.Name] = $c.Option } }
    foreach ($w in $wanted) {
        if (-not $chosen.ContainsKey($w.Name)) {
            if ($NotRead.Add($w.Name)) {
                & $Log ("Could not read field $($w.Name)" + $(if ($w.Sticky) { ': column left out' } else { ': it may show a number instead of a name' }) + " (first seen in $Name).")
            }
        } elseif ($chosen[$w.Name].Label -eq $script:HiddenFields[$w.Name] -and $NotRead.Add("$($w.Name)>")) {
            & $Log "Field $($w.Name) is read through $($chosen[$w.Name].Label)."
        }
    }

    # fields of this library -> this library's columns
    $columns = New-Object 'Collections.Generic.List[string]'
    $colIndex = @{}
    foreach ($c in $script:FrontColumns) { $colIndex[$c] = $columns.Count; $columns.Add($c) }
    $picks = New-Object 'Collections.Generic.List[object]'
    $fieldRows = New-Object 'Collections.Generic.List[object]'
    $extra = @{}                # field -> how an extra pass writes it
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $listed = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($f in $fields) {
        $n = [string]$f['InternalName']
        $front = $script:FrontColumns -ccontains $n
        $main = ($script:AlwaysFields -ccontains $n) -or (-not $f['Hidden'] -and $f['TypeAsString'] -ne 'Computed')
        $use = $front -or $main -or $chosen.ContainsKey($n)
        if ($listed.Add($n)) {
            $fieldRows.Add([object[]]@($Lib.WebUrl, $Lib.WebTitle, $Lib.Title, $Lib.Folder, $n, [string]$f['Title'], [string]$f['TypeAsString'],
                                       $(if ($f['Hidden']) { 'Yes' } else { 'No' }), $(if ($use) { 'Yes' } else { 'No' })))
        }
        if ($front -or -not $use -or -not $seen.Add($n)) { continue }
        if (-not $colIndex.ContainsKey($n)) { $colIndex[$n] = $columns.Count; $columns.Add($n) }
        $isDate = ($f['TypeAsString'] -eq 'DateTime') -or ($script:HiddenDates -contains $n)
        if ($chosen.ContainsKey($n)) {
            $readPath = $chosen[$n].Path
            $extra[$n] = @{ Index = $colIndex[$n]; Path = [string[]]$readPath; One = $(if ($readPath.Count -eq 1) { $readPath[0] } else { '' }); Date = [bool]$isDate }
            continue
        }
        $picks.Add(@{
            Index = $colIndex[$n]
            Text  = $n.Replace('_', '_x005f_')                                     # key in FieldValuesAsText
            Raw   = $(if ($n.StartsWith('_')) { 'OData_' + $n } else { $n })        # key of the raw value
            Date  = [bool]$isDate
        })
    }
    $extras = New-Object 'Collections.Generic.List[object]'
    foreach ($g in $groups) {
        $extras.Add(@{
            Names = [string[]]@($g | ForEach-Object { $_.Name })
            Url   = Get-SpGroupUrl $listApi @($g | ForEach-Object { $_.Option }) $script:PageSize
            Picks = @($g | ForEach-Object { $extra[$_.Name] })
        })
    }
    @{ Columns = [string[]]$columns.ToArray(); Picks = $picks.ToArray(); Extras = $extras.ToArray(); FieldRows = $fieldRows.ToArray(); ListApi = $listApi }
}

function Export-SpMetadata {
    <# Read every file of $Libraries and write one sheet to $Path (.xlsx or .csv). Returns stats.
       $Progress gets (percent, text): 0-90 reading, 90-100 writing.
       With -Resume it continues the unfinished export of the same libraries in the same output folder, if there is one. #>
    param([hashtable] $Sp, $Libraries, [string] $Path, [scriptblock] $Log = { param($m) Write-Host $m }, [scriptblock] $Progress, [switch] $Resume)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $xlsx = [IO.Path]::GetExtension($Path).ToLowerInvariant() -eq '.xlsx'
    Initialize-SpEngine
    $Path = [IO.Path]::GetFullPath($Path)
    $work = Get-SpWorkDir $Path $Libraries
    $workRoot = [IO.Path]::GetDirectoryName($work)
    Remove-SpOldWork $workRoot
    $continuing = $Resume -and (Get-SpUnfinished $Path $Libraries)
    [void][IO.Directory]::CreateDirectory($work)
    # one export at a time on one work folder: take the lock before touching anything in it
    $lockFile = Join-Path $work 'running.lock'
    try { $lock = New-Object IO.FileStream($lockFile, 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw "Another export of these libraries is still running (work folder $work)." }
    if (-not $continuing) { Get-ChildItem -LiteralPath $work -Force | Where-Object { $_.FullName -ne $lockFile } | Remove-Item -Recurse -Force }
    $stats = [ordered]@{ libraries = 0; files = 0; folders_skipped = 0; columns = 0 }
    $finished = $false
    try {
        if ($continuing) { & $Log "Continuing the unfinished export of these libraries from its last saved page ($work)." }
        $remember = @{}                                                       # hidden field -> way to read it
        $notRead = New-Object 'Collections.Generic.HashSet[string]'           # already logged

        # each library counts by its items; within it, the main pass and each extra pass count the same
        $libCount = @($Libraries).Count
        $totalItems = [double]0
        foreach ($lib in $Libraries) { $totalItems += [long]$lib.ItemCount }
        if ($xlsx -and $totalItems -ge $script:ExcelMaxRows) {
            & $Log ("These libraries hold $($totalItems.ToString('N0', $script:Inv)) items, and one Excel sheet holds 1,048,575 rows. " +
                    'Folders do not count, but if the files do not fit the export stops at the end: export again as .csv, which continues without reading everything again.')
        }
        $doneWeight = [double]0; $libWeight = [double]1; $libNo = 0; $name = ''
        $readClock = $null; $readBase = [double]0
        $report = {
            param([double] $Fraction, [string] $Detail)
            if (-not $Progress) { return }
            $reading = ($doneWeight + $libWeight * [Math]::Min(1.0, $Fraction)) / [Math]::Max(1.0, $totalItems + $libCount)
            $left = ''
            # speed counts from the first page read now: a continued export starts part way
            if ($readClock -and $reading - $readBase -ge 0.005 -and $reading -lt 1 -and $readClock.Elapsed.TotalSeconds -ge 30) {
                $s = $readClock.Elapsed.TotalSeconds * (1 - $reading) / ($reading - $readBase)
                $left = if ($s -ge 3600) { '  |  reading: about {0:0.0} h left' -f ($s / 3600) } else { '  |  reading: about {0:0} min left' -f [Math]::Ceiling($s / 60) }
            }
            & $Progress (90 * $reading) ("Export $([Math]::Floor(90 * $reading))%  |  library $libNo of $libCount`: $name  |  $Detail$left")
        }

        $plans = New-Object 'Collections.Generic.List[hashtable]'
        foreach ($lib in $Libraries) {
            $lsw = [Diagnostics.Stopwatch]::StartNew()
            $name = "$($lib.WebTitle) / $($lib.Title)"
            $libNo++
            $libItems = [Math]::Max(1.0, [double]$lib.ItemCount)
            $libWeight = [double]$lib.ItemCount + 1
            $k = $libNo - 1
            $authority = ([Uri]$lib.WebUrl).GetLeftPart([UriPartial]::Authority)

            # the plan goes to disk first and is always read back, so a continued export reads the same columns
            $planFile = Join-Path $work "lib$k.plan"
            if (-not (Test-Path -LiteralPath $planFile)) {
                & $Log "Library $libNo of $libCount`: $name, checking which fields can be read"
                & $report 0 'checking which fields can be read'
                $newPlan = Get-SpLibraryPlan -Sp $Sp -Lib $lib -Name $name -Remember $remember -NotRead $notRead -Log $Log
                $newPlan | Export-Clixml -LiteralPath "$planFile.tmp" -Depth 6
                Move-Item -LiteralPath "$planFile.tmp" -Destination $planFile -Force
            }
            $plan = Import-Clixml -LiteralPath $planFile
            $plan.Main = Join-Path $work "lib$k.main"
            $plan.ExtraFiles = [string[]]@(for ($e = 0; $e -lt @($plan.Extras).Count; $e++) { Join-Path $work "lib$k.extra$e" })
            $plans.Add($plan)
            $passes = 1 + @($plan.Extras).Count
            $itemText = $libItems.ToString('N0', $script:Inv)
            & $Log "Library $libNo of $libCount`: $name, $itemText items in $passes steps"

            # main pass
            $mainPicks = [MetadataHorizontal.SpPick[]]@(foreach ($p in @($plan.Picks)) { if ($p) { [MetadataHorizontal.SpEngine]::MainPick([int]$p.Index, [string]$p.Text, [string]$p.Raw, [bool]$p.Date) } })
            # -1 when continuing an export whose saved plan does not have these columns
            $libraryCol = [array]::IndexOf([string[]]@($plan.Columns), 'LibraryTitle')
            $libraryIdCol = [array]::IndexOf([string[]]@($plan.Columns), 'LibraryId')
            $pass = New-Object MetadataHorizontal.SpPass($plan.Main)
            try {
                $url = if ($pass.NextUrl) { $pass.NextUrl } else {
                    "$($plan.ListApi)/items?`$select=*,FileRef,FileLeafRef,FileDirRef,UniqueId,FSObjType,FieldValuesAsText" +
                    "&`$expand=FieldValuesAsText&`$top=$script:PageSize"
                }
                while (-not $pass.Done) {
                    if (-not $readClock) { $readClock = [Diagnostics.Stopwatch]::StartNew(); $readBase = ($doneWeight + $libWeight * [Math]::Min(1.0, $pass.Items / $libItems) / $passes) / [Math]::Max(1.0, $totalItems + $libCount) }
                    $d = Invoke-SpGet $Sp $url
                    [MetadataHorizontal.SpEngine]::AddMainPage($pass, $d['results'], $authority, $mainPicks, $libraryCol, [string]$lib.Title, $libraryIdCol, [string]$lib.Id)
                    $url = [string]$d['__next']
                    $pass.Checkpoint($url)
                    & $report ([Math]::Min(1.0, $pass.Items / $libItems) / $passes) "step 1 of $passes`: $($pass.Items.ToString('N0', $script:Inv)) of $itemText items"
                }
                $files = $pass.Files; $folders = $pass.Folders
            } finally { $pass.Dispose() }

            # extra passes: fields the main pass cannot get, merged into the rows by item id at the end
            if ($files) {
                $extraNo = 0
                foreach ($x in @($plan.Extras)) {
                    if (-not $x) { continue }
                    $file = $plan.ExtraFiles[$extraNo]
                    $extraNo++
                    $picks = [MetadataHorizontal.SpPick[]]@(foreach ($p in @($x.Picks)) { if ($p) { [MetadataHorizontal.SpEngine]::ExtraPick([int]$p.Index, [string]$p.One, [string[]]@($p.Path), [bool]$p.Date) } })
                    $pass = New-Object MetadataHorizontal.SpPass($file)
                    try {
                        $url = if ($pass.NextUrl) { $pass.NextUrl } else { [string]$x.Url }
                        while (-not $pass.Done) {
                            if (-not $readClock) { $readClock = [Diagnostics.Stopwatch]::StartNew(); $readBase = ($doneWeight + $libWeight * ($extraNo + [Math]::Min(1.0, $pass.Items / $libItems)) / $passes) / [Math]::Max(1.0, $totalItems + $libCount) }
                            $d = Invoke-SpGet $Sp $url
                            [MetadataHorizontal.SpEngine]::AddExtraPage($pass, $d['results'], $picks)
                            $url = [string]$d['__next']
                            $pass.Checkpoint($url)
                            & $report (($extraNo + [Math]::Min(1.0, $pass.Items / $libItems)) / $passes) "step $($extraNo + 1) of $passes (extra fields): $($pass.Items.ToString('N0', $script:Inv)) of $itemText items"
                        }
                    } catch {
                        & $Log "$name`: extra fields not complete, some values stay blank ($(@($x.Names) -join ', ')): $_"
                    } finally { $pass.Dispose() }
                }
            }
            $doneWeight += $libWeight
            $stats.libraries++
            $stats.folders_skipped += $folders
            & $Log ("${name}: $($files.ToString('N0', $script:Inv)) files" +
                    $(if ($folders) { " ($($folders.ToString('N0', $script:Inv)) folders left out)" } else { '' }) +
                    "  [$($lsw.Elapsed.TotalSeconds.ToString('0.0', $script:Inv))s]")
            $stats.files += $files
        }

        # ---- write -------------------------------------------------------------------
        $columns = New-Object 'Collections.Generic.List[string]'
        $colIndex = New-Object 'Collections.Generic.Dictionary[string,int]' ([StringComparer]::Ordinal)
        $spools = New-Object 'Collections.Generic.List[MetadataHorizontal.SpLibSpool]'
        $fieldRows = New-Object 'Collections.Generic.List[object]'
        foreach ($plan in $plans) {
            $map = New-Object 'int[]' @($plan.Columns).Count
            $i = 0
            foreach ($c in @($plan.Columns)) {
                if (-not $colIndex.ContainsKey($c)) { $colIndex[$c] = $columns.Count; $columns.Add($c) }
                $map[$i] = $colIndex[$c]
                $i++
            }
            $spools.Add((New-Object MetadataHorizontal.SpLibSpool($plan.Main, $plan.ExtraFiles, $map)))
            foreach ($r in @($plan.FieldRows)) { if ($r) { $fieldRows.Add($r) } }
        }
        $width = $columns.Count
        $stats.columns = $width
        if ($xlsx -and $stats.files + 1 -gt $script:ExcelMaxRows) {
            throw "$($stats.files.ToString('N0', $script:Inv)) files do not fit in one Excel sheet (max 1,048,575 rows): export the same libraries again as .csv in this folder, and it continues without reading everything again."
        }
        $dir = [IO.Path]::GetDirectoryName($Path)
        if ($dir) { [void][IO.Directory]::CreateDirectory($dir) }
        $rowTotal = [Math]::Max(1, $stats.files)
        $rowText = $rowTotal.ToString('N0', $script:Inv)
        $writeProgress = [Action[long]] {
            param([long] $rowsDone)
            if ($Progress) { & $Progress (90 + 10 * $rowsDone / $rowTotal) "Export: writing $($rowsDone.ToString('N0', $script:Inv)) of $rowText rows" }
        }
        if ($Progress) { & $Progress 90 "Export: writing 0 of $rowText rows" }
        # to a temporary name first: a failure must not leave a file that looks complete, or spoil one being replaced
        $writing = "$Path.writing"
        try {
            if ($xlsx) {
                $stats.files = [MetadataHorizontal.SpEngine]::WriteXlsx($writing, [string[]]$columns.ToArray(), $spools.ToArray(), $script:LibColumnsSheet,
                    [string[]]$script:LibColumnsHeader, $fieldRows, $script:ExcelMaxText, $writeProgress)
            } else {
                & $Log 'A .csv export has no Library columns sheet: save as .xlsx to let Compare see each library''s columns.'
                $stats.files = [MetadataHorizontal.SpEngine]::WriteCsv($writing, [string[]]$columns.ToArray(), $spools.ToArray(), $writeProgress)
            }
            Move-Item -LiteralPath $writing -Destination $Path -Force
        } finally {
            if (Test-Path -LiteralPath $writing) { Remove-Item -LiteralPath $writing -Force -ErrorAction SilentlyContinue }
        }
        $finished = $true
        $stats.path = $Path
        & $Log ("Saved $($stats.files.ToString('N0', $script:Inv)) files x $width columns from $($stats.libraries) libraries " +
                "to $Path  [$($sw.Elapsed.TotalSeconds.ToString('0.0', $script:Inv))s]")
        return $stats
    } catch {
        & $Log "The export stopped. What it has read so far is kept for $($script:WorkMaxDays) days in $work. Export the same libraries to this folder again to continue."
        throw
    } finally {
        $lock.Dispose()
        if ($finished) {
            Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
            if (-not (Get-ChildItem -LiteralPath $workRoot -Force -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $workRoot -Force -ErrorAction SilentlyContinue }
        }
    }
}


# ---------------------------------------------------------------------------
# Compare: is every file of the per-library CSV scans in the combined export?
# ---------------------------------------------------------------------------

function Open-SpCsvParser([string] $Path) {
    # FileShare.ReadWrite: the file may still be open in Excel
    $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
        $parser = New-Object Microsoft.VisualBasic.FileIO.TextFieldParser($reader)
        $parser.TextFieldType = [Microsoft.VisualBasic.FileIO.FieldType]::Delimited
        $parser.SetDelimiters(',')
        $parser.HasFieldsEnclosedInQuotes = $true
        $parser.TrimWhiteSpace = $false
        return $parser
    } catch {
        $stream.Dispose()
        throw
    }
}

function Get-SpHeaderIndex([string[]] $Headers) {
    <# Column name -> position (first occurrence; names are case-insensitive). #>
    $ix = @{}
    for ($j = 0; $j -lt $Headers.Length; $j++) {
        $n = $Headers[$j].Trim()
        if ($n -and -not $ix.ContainsKey($n)) { $ix[$n] = $j }
    }
    return $ix
}

function Format-SpCompareId([string] $Value) { $Value.Trim().Trim('{', '}') }

function Read-SpSheetColumn($Ws, [int] $Col, [int] $FirstRow, [int] $LastRow) {
    <# One sheet column as string[]; reading only the needed columns keeps a wide sheet fast. #>
    $n = $LastRow - $FirstRow + 1
    $out = New-Object 'string[]' ([Math]::Max($n, 0))
    if ($n -le 0) { return , $out }
    $v = $Ws.Cells[$FirstRow, $Col, $LastRow, $Col].Value
    if ($v -isnot [Array]) { $out[0] = [Convert]::ToString($v, $script:Inv); return , $out }
    for ($i = 0; $i -lt $n; $i++) {
        $c = $v[$i, 0]
        $out[$i] = if ($c -is [string]) { $c } else { [Convert]::ToString($c, $script:Inv) }
    }
    return , $out
}

function Read-SpSheetPresence($Ws, [int] $Col, [int] $FirstRow, [int] $LastRow) {
    <# Which rows of one sheet column have a value -> @{ Has = bool[]; Rows = list of row positions }. #>
    $n = [Math]::Max($LastRow - $FirstRow + 1, 0)
    $has = New-Object 'bool[]' $n
    $rows = New-Object 'Collections.Generic.List[int]'
    if ($n -gt 0) {
        $v = $Ws.Cells[$FirstRow, $Col, $LastRow, $Col].Value
        if ($v -isnot [Array]) { $one = New-Object 'object[,]' 1, 1; $one[0, 0] = $v; $v = $one }
        for ($i = 0; $i -lt $n; $i++) {
            $c = $v[$i, 0]
            if ($null -ne $c -and ($c -isnot [string] -or $c.Length)) { $has[$i] = $true; $rows.Add($i) }
        }
    }
    return @{ Has = $has; Rows = $rows }
}

function ConvertTo-SpMatchName([string] $Text) { [regex]::Replace($Text, '[^\p{L}\p{N}]', '').ToLowerInvariant() }

function Read-SpLibColumns($Ws) {
    <# The "Library columns" sheet -> list of libraries @{ Folder; Title; WebPath; All; Exported; MatchName; Used }. #>
    $head = $Ws.Dimension.Start.Row
    $last = $Ws.Dimension.End.Row
    $headers = [string[]]@(for ($c = 1; $c -le $Ws.Dimension.End.Column; $c++) { [string]$Ws.Cells[$head, $c].Value })
    $ix = Get-SpHeaderIndex $headers
    foreach ($n in 'SiteUrl', 'LibraryTitle', 'LibraryFolder', 'InternalName', 'Exported') {
        if (-not $ix.ContainsKey($n)) { throw "Sheet '$($Ws.Name)' has no $n column" }
    }
    $cols = @{}
    foreach ($n in 'SiteUrl', 'LibraryTitle', 'LibraryFolder', 'InternalName', 'Exported') { $cols[$n] = Read-SpSheetColumn $Ws ($ix[$n] + 1) ($head + 1) $last }
    return Group-SpLibColumns $cols
}

function Group-SpLibColumns([hashtable] $Cols) {
    <# Columns of the Library columns sheet -> list of libraries, longest folder first. #>
    $cols = $Cols
    $byFolder = @{}
    for ($i = 0; $i -lt $cols['LibraryFolder'].Length; $i++) {
        $folder = $cols['LibraryFolder'][$i]
        if (-not $folder) { continue }
        if (-not $byFolder.ContainsKey($folder)) {
            $webPath = [Uri]::UnescapeDataString(([Uri]$cols['SiteUrl'][$i]).AbsolutePath).TrimEnd('/')
            $rel = if ($folder.StartsWith("$webPath/", [StringComparison]::OrdinalIgnoreCase)) { $folder.Substring($webPath.Length + 1) } else { $folder }
            $byFolder[$folder] = @{ Folder = $folder; Title = $cols['LibraryTitle'][$i]; WebPath = $webPath; Used = $false
                                    All = (New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase))
                                    Exported = (New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase))
                                    MatchName = (ConvertTo-SpMatchName "$webPath$($cols['LibraryTitle'][$i])$rel") }
        }
        $name = $cols['InternalName'][$i]
        [void]$byFolder[$folder].All.Add($name)
        if ($cols['Exported'][$i] -eq 'Yes') { [void]$byFolder[$folder].Exported.Add($name) }
    }
    return @($byFolder.Values | Sort-Object { $_.Folder.Length } -Descending)
}

function Find-SpLibByRef($Libraries, [string] $Ref) {
    <# The library whose folder holds this FileRef; $Libraries is sorted longest folder first. #>
    foreach ($l in $Libraries) { if ($Ref.StartsWith("$($l.Folder)/", [StringComparison]::OrdinalIgnoreCase)) { return $l } }
    return $null
}

function Read-SpBigCompareWorkbook([string] $Path, [scriptblock] $Progress, [string[]] $ValueColumns) {
    <# Read-SpCompareWorkbook for a big .xlsx: row by row, no copy of the sheet in memory.
       Values are kept as 8-byte keys only; Get-SpWorkbookText reads the text of the cells that differ. #>
    $need = 'UniqueId or FileRef'
    $r = Open-SpSheetReader $Path ''
    try {
        $headers = $r.ReadRow(0)
        if ($null -eq $headers) { throw "Sheet '$($r.SheetName)' is empty" }
        $ix = Get-SpHeaderIndex $headers
        if (-not ($ix.ContainsKey('UniqueId') -or $ix.ContainsKey('FileRef'))) { throw "Sheet '$($r.SheetName)' has no $need column" }
        $names = [string[]]@($ix.Keys)
        $keep = [int[]]@($names | ForEach-Object { $ix[$_] })
        $vNames = [string[]]@($ValueColumns | Where-Object { $_ -and $ix.ContainsKey($_) })
        $vCols = [int[]]@($vNames | ForEach-Object { $ix[$_] })
        $at = { param($n) if ($ix.ContainsKey($n)) { [int]$ix[$n] } else { -1 } }
        $tick = [Action[long]] { param([long] $rowsDone) if ($Progress) { & $Progress $rowsDone 'workbook' } }
        $d = $r.ReadCompare($headers.Length, (& $at 'UniqueId'), (& $at 'FileRef'), (& $at 'FSObjType'), $keep, $vCols, $tick)
        $out = @{ Columns = @($names); Ids = $d.Ids; Refs = $d.Refs; Types = $d.Types; Cells = @{}
                  ValueNames = $vNames; ValueKeys = $d.Keys; ValueText = $null; Path = $Path; Width = $headers.Length; ValueCols = $vCols }
        for ($j = 0; $j -lt $names.Length; $j++) { $out.Cells[$names[$j]] = @{ Has = $d.Has[$j]; Rows = $d.Rows[$j] } }
    } finally { $r.Dispose() }
    if ([MetadataHorizontal.SpSheetReader]::HasSheet($Path, $script:LibColumnsSheet)) {
        $r = Open-SpSheetReader $Path $script:LibColumnsSheet
        try {
            $headers = $r.ReadRow(0)
            if ($null -ne $headers) {
                $ix = Get-SpHeaderIndex $headers
                $wantCols = 'SiteUrl', 'LibraryTitle', 'LibraryFolder', 'InternalName', 'Exported'
                foreach ($n in $wantCols) { if (-not $ix.ContainsKey($n)) { throw "Sheet '$($r.SheetName)' has no $n column" } }
                $lists = @{}
                foreach ($n in $wantCols) { $lists[$n] = New-Object 'Collections.Generic.List[string]' }
                while ($null -ne ($row = $r.ReadRow($headers.Length))) { foreach ($n in $wantCols) { $lists[$n].Add($row[$ix[$n]]) } }
                $cols = @{}
                foreach ($n in $wantCols) { $cols[$n] = $lists[$n].ToArray() }
                $out.LibColumns = Group-SpLibColumns $cols
            }
        } finally { $r.Dispose() }
    }
    return $out
}

function Read-SpCompareWorkbook([string] $Path, [scriptblock] $Progress, [string[]] $ValueColumns) {
    <# The combined export (.xlsx/.xlsm/.csv) -> @{ Columns; Ids; Refs; Types; Cells } with parallel arrays;
       Cells: column name -> which rows have a value (see Read-SpSheetPresence).
       ValueColumns: columns whose values are compared -> ValueNames, ValueKeys (SpValue keys per row) and
       ValueText (the text, for a workbook small enough to keep it; else $null). #>
    $need = 'UniqueId or FileRef'
    $ext = [IO.Path]::GetExtension($Path).ToLowerInvariant()
    Initialize-SpEngine
    if ($ext -eq '.csv') {
        $ids = New-Object 'Collections.Generic.List[string]'
        $refs = New-Object 'Collections.Generic.List[string]'
        $types = New-Object 'Collections.Generic.List[string]'
        $p = Open-SpCsvParser $Path
        try {
            if ($p.EndOfData) { throw "$Path is empty" }
            $ix = Get-SpHeaderIndex $p.ReadFields()
            if (-not ($ix.ContainsKey('UniqueId') -or $ix.ContainsKey('FileRef'))) { throw "$Path has no $need column" }
            $flags = @{}
            $lists = @{}
            foreach ($n in $ix.Keys) { $flags[$n] = New-Object 'Collections.Generic.List[bool]'; $lists[$n] = New-Object 'Collections.Generic.List[int]' }
            $vNames = [string[]]@($ValueColumns | Where-Object { $_ -and $ix.ContainsKey($_) })
            $vText = @{}
            foreach ($n in $vNames) { $vText[$n] = New-Object 'Collections.Generic.List[string]' }
            $row = 0
            while (-not $p.EndOfData) {
                $f = $p.ReadFields()
                if ($null -eq $f) { continue }
                $ids.Add($(if ($ix.ContainsKey('UniqueId') -and $ix['UniqueId'] -lt $f.Length) { $f[$ix['UniqueId']] }))
                $refs.Add($(if ($ix.ContainsKey('FileRef') -and $ix['FileRef'] -lt $f.Length) { $f[$ix['FileRef']] }))
                $types.Add($(if ($ix.ContainsKey('FSObjType') -and $ix['FSObjType'] -lt $f.Length) { $f[$ix['FSObjType']] }))
                foreach ($n in $ix.Keys) {
                    $on = $ix[$n] -lt $f.Length -and $f[$ix[$n]].Length -gt 0
                    $flags[$n].Add($on)
                    if ($on) { $lists[$n].Add($row) }
                }
                foreach ($n in $vNames) { $vText[$n].Add($(if ($ix[$n] -lt $f.Length) { $f[$ix[$n]] } else { '' })) }
                $row++
            }
        } finally { $p.Dispose() }
        $cells = @{}
        foreach ($n in $ix.Keys) { $cells[$n] = @{ Has = $flags[$n].ToArray(); Rows = $lists[$n] } }
        $text = @{}
        foreach ($n in $vNames) { $text[$n] = $vText[$n].ToArray() }
        return @{ Columns = @($ix.Keys); Ids = $ids.ToArray(); Refs = $refs.ToArray(); Types = $types.ToArray(); Cells = $cells
                  ValueNames = $vNames; ValueKeys = [long[][]]@(foreach ($n in $vNames) { , [MetadataHorizontal.SpValue]::Keys($text[$n]) }); ValueText = $text }
    }
    if ($ext -ne '.xlsx' -and $ext -ne '.xlsm') { throw "Unsupported file type: $ext (use .xlsx or .csv)" }
    # row by row for a big sheet, and whenever values are compared: the same row numbering for keys and text
    if ($ValueColumns -or (Test-SpBigXlsx $Path)) { return Read-SpBigCompareWorkbook $Path $Progress $ValueColumns }
    if (-not (Get-Module ImportExcel)) {
        if (-not (Get-Module -ListAvailable ImportExcel)) { throw 'Reading .xlsx needs the ImportExcel module (or use the .csv export).' }
        Import-Module ImportExcel
    }
    $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $pkg = New-Object OfficeOpenXml.ExcelPackage($stream)
        try {
            $ws = $pkg.Workbook.Worksheets | Select-Object -First 1
            $dim = $ws.Dimension
            if (-not $dim) { throw "Sheet '$($ws.Name)' is empty" }
            $head = $dim.Start.Row
            $last = $dim.End.Row
            $headers = [string[]]@(for ($c = 1; $c -le $dim.End.Column; $c++) { [string]$ws.Cells[$head, $c].Value })
            $ix = Get-SpHeaderIndex $headers
            if (-not ($ix.ContainsKey('UniqueId') -or $ix.ContainsKey('FileRef'))) { throw "Sheet '$($ws.Name)' has no $need column" }
            $out = @{ Columns = @($ix.Keys) }
            foreach ($pair in @(@('Ids', 'UniqueId'), @('Refs', 'FileRef'), @('Types', 'FSObjType'))) {
                $out[$pair[0]] = if ($ix.ContainsKey($pair[1])) { Read-SpSheetColumn $ws ($ix[$pair[1]] + 1) ($head + 1) $last }
                                 else { , (New-Object 'string[]' ($last - $head)) }
            }
            $out.Cells = @{}
            foreach ($n in $ix.Keys) { $out.Cells[$n] = Read-SpSheetPresence $ws ($ix[$n] + 1) ($head + 1) $last }
            $sheet = $pkg.Workbook.Worksheets[$script:LibColumnsSheet]
            if ($sheet -and $sheet.Dimension) { $out.LibColumns = Read-SpLibColumns $sheet }
            return $out
        } finally { $pkg.Dispose() }
    } finally { $stream.Dispose() }
}

function Get-SpWorkbookText($Wb, $Diffs) {
    <# Fill .Xlsx of each SpValueDiff with the workbook's text: from memory for a .csv workbook, else one more pass over the sheet. #>
    if (-not $Diffs.Count) { return }
    if ($Wb.ValueText) {
        foreach ($d in $Diffs) { $d.Xlsx = $Wb.ValueText[$Wb.ValueNames[$d.Col]][$d.Raw] }
        return
    }
    $rows = New-Object 'Collections.Generic.HashSet[int]'
    foreach ($d in $Diffs) { [void]$rows.Add($d.Raw) }
    $r = Open-SpSheetReader $Wb.Path ''
    try {
        [void]$r.ReadRow(0)
        $text = $r.ReadRowsAt($Wb.Width, $rows, [int[]]$Wb.ValueCols)
    } finally { $r.Dispose() }
    foreach ($d in $Diffs) { $v = $null; if ($text.TryGetValue($d.Raw, [ref]$v)) { $d.Xlsx = $v[$d.Col] } }
}

function Get-SpExportSites([string] $Path) {
    <# The site URLs an .xlsx export covers, from its Library columns sheet (subsites included). Empty when it has none. #>
    if ([IO.Path]::GetExtension($Path) -notmatch '^\.xls[xm]$') { return @() }
    Initialize-SpEngine
    if (-not [MetadataHorizontal.SpSheetReader]::HasSheet($Path, $script:LibColumnsSheet)) { return @() }
    $sites = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $r = Open-SpSheetReader $Path $script:LibColumnsSheet
    try {
        $headers = $r.ReadRow(0)
        if ($null -eq $headers) { return @() }
        $ix = Get-SpHeaderIndex $headers
        if (-not $ix.ContainsKey('SiteUrl')) { return @() }
        while ($null -ne ($row = $r.ReadRow($headers.Length))) { if ($row[$ix['SiteUrl']]) { [void]$sites.Add($row[$ix['SiteUrl']].TrimEnd('/')) } }
    } finally { $r.Dispose() }
    return @($sites | Sort-Object)
}

function Find-SpCompareCsvs([string[]] $Sites, [string] $CsvFolder) {
    <# The per-library CSVs below $CsvFolder that belong to these sites: a CSV belongs to a site when its folder path ends with
       <host name>/<site path> (e.g. ...\ishare\partner\aro for http://ishare.mtr.com.hk/partner/ARO), else with <site path>.
       Only the site's own folder counts, so a subsite exported to another file is left out.
       -> @{ Files = FileInfo[]; Folders = matched folders; NoCsv = sites with no folder } #>
    $all = @(Get-ChildItem -LiteralPath $CsvFolder -Recurse -File -Filter '*.csv')
    $byDir = @{}
    foreach ($f in $all) {
        $k = $f.DirectoryName.Replace('\', '/').TrimEnd('/').ToLowerInvariant()
        if (-not $byDir.ContainsKey($k)) { $byDir[$k] = New-Object 'Collections.Generic.List[object]' }
        $byDir[$k].Add($f)
    }
    $files = New-Object 'Collections.Generic.List[object]'
    $folders = New-Object 'Collections.Generic.List[string]'
    $noCsv = New-Object 'Collections.Generic.List[string]'
    foreach ($s in $Sites) {
        $u = [Uri]$s
        $path = [Uri]::UnescapeDataString($u.AbsolutePath).Trim('/').ToLowerInvariant()
        $hostKey = (($u.Host.Split('.')[0] + '/' + $path).TrimEnd('/'))
        $hit = @($byDir.Keys | Where-Object { $_ -eq $hostKey -or $_.EndsWith("/$hostKey") })
        if (-not $hit -and $path) { $hit = @($byDir.Keys | Where-Object { $_.EndsWith("/$path") }) }
        if (-not $hit) { $noCsv.Add($s); continue }
        foreach ($k in $hit) {
            if ($folders.Contains($k)) { continue }
            $folders.Add($k)
            foreach ($f in $byDir[$k]) { if ($f.Name -notlike 'inventory_*') { $files.Add($f) } }
        }
    }
    return @{ Files = $files.ToArray(); Folders = $folders.ToArray(); NoCsv = $noCsv.ToArray() }
}

function Test-SpSystemCsvName([string] $Name) {
    <# Scan file name "<library title>_<folder>": is it one of SharePoint's own libraries? #>
    if ($Name.Contains('__catalogs_')) { return $true }
    foreach ($s in $script:SystemFolders) { if ($Name.EndsWith("_$s", [StringComparison]::OrdinalIgnoreCase)) { return $true } }
    return $false
}

function Add-SpReportSheet($Package, [string] $Name, [string[]] $Header, $Rows) {
    $ws = $Package.Workbook.Worksheets.Add($Name)
    $data = New-Object 'Collections.Generic.List[object[]]' ($Rows.Count + 1)
    $data.Add([object[]]$Header)
    foreach ($r in $Rows) {
        $r = [object[]]$r
        if ($r.Length -lt $Header.Length) { $p = New-Object 'object[]' $Header.Length; [Array]::Copy($r, $p, $r.Length); $r = $p }   # rows without the last columns
        $data.Add($r)
    }
    [void]$ws.Cells['A1'].LoadFromArrays($data)
    $ws.Cells[1, 1, 1, $Header.Length].Style.Font.Bold = $true
    $ws.Cells[1, 1, 1, $Header.Length].AutoFilter = $true
    $ws.View.FreezePanes(2, 1)
    for ($c = 1; $c -le $Header.Length; $c++) { $ws.Column($c).Width = [Math]::Max(14, $Header[$c - 1].Length + 4) }
}

function Add-SpLibColumn([hashtable] $Lib, [hashtable] $Columns, [string] $Name, [int] $CsvValues, [int] $CsvOnly, [int] $WbOnly) {
    <# Add one CSV's counts for one column, to its library and to the overall totals. #>
    foreach ($t in $Lib.Cols, $Columns) {
        if (-not $t.ContainsKey($Name)) { $t[$Name] = @{ Name = $Name; Csvs = 0; Values = 0; CsvOnly = 0; WbOnly = 0 } }
        $c = $t[$Name]
        $c.Csvs++; $c.Values += $CsvValues; $c.CsvOnly += $CsvOnly; $c.WbOnly += $WbOnly
    }
}

function Compare-SpExport {
    <# Every *.csv below $CsvFolder (one per library, from the other scanner) against the combined
       export $WorkbookPath. A file matches on UniqueId, else on FileRef; folders do not count.
       A library scanned more than once counts each file once. Writes the report to $ReportPath.
       Columns are checked both ways. The workbook lists the columns of all libraries together, so
       a column counts as missing from a CSV only if the workbook has a value for one of its files.
       Values: for every file in both and every column in both, the CSV value is compared with the xlsx value
       (Value check sheets), unless -NoValues. $CsvFiles: only these CSVs (below $CsvFolder) instead of all of them. #>
    param([string] $WorkbookPath, [string] $CsvFolder, [string] $ReportPath,
          [scriptblock] $Log = { param($m) Write-Host $m }, [scriptblock] $Progress, [object[]] $CsvFiles, [switch] $NoValues)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $inv = $script:Inv
    $nf = { param($n) ([long]$n).ToString('N0', $inv) }.GetNewClosure()
    $root = [IO.Path]::GetFullPath($CsvFolder).TrimEnd('\')
    $self = [IO.Path]::GetFullPath($WorkbookPath)
    $csvFiles = if ($CsvFiles) { @($CsvFiles | ForEach-Object { if ($_ -is [IO.FileInfo]) { $_ } else { Get-Item -LiteralPath $_ } } | Sort-Object FullName) }
                else { @(Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.csv' | Where-Object { $_.FullName -ne $self } | Sort-Object FullName) }
    if (-not $csvFiles.Count) { throw "No .csv files found below $root" }
    if (-not (Get-Module ImportExcel)) {
        if (-not (Get-Module -ListAvailable ImportExcel)) { throw 'Writing the report needs the ImportExcel module.' }
        Import-Module ImportExcel
    }

    # columns whose values are compared: in the CSVs (header rows only) and not the match keys
    $valueColumns = @()
    if (-not $NoValues) {
        $csvHeads = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        foreach ($file in $csvFiles) {
            $p = $null
            try { $p = Open-SpCsvParser $file.FullName; if (-not $p.EndOfData) { foreach ($h in $p.ReadFields()) { if ($h.Trim()) { [void]$csvHeads.Add($h.Trim()) } } } }
            catch { } finally { if ($p) { $p.Dispose() } }
        }
        foreach ($n in @('UniqueId', 'FileRef', 'FSObjType') + $script:ExportOnlyColumns) { [void]$csvHeads.Remove($n) }
        $valueColumns = [string[]]@($csvHeads)
    }

    # ---- combined export -----------------------------------------------------------
    & $Log "Reading $WorkbookPath ..."
    $wb = Read-SpCompareWorkbook $WorkbookPath $Progress $valueColumns
    $vc = $null
    if (-not $NoValues -and $wb.ValueNames -and $wb.ValueNames.Count) {
        $vc = New-Object MetadataHorizontal.SpValueCompare(([string[]]$wb.ValueNames), ([long[][]]$wb.ValueKeys))
    }
    $byId = New-Object 'Collections.Generic.Dictionary[string,int]' ([StringComparer]::OrdinalIgnoreCase)
    $byRef = New-Object 'Collections.Generic.Dictionary[string,int]' ([StringComparer]::OrdinalIgnoreCase)
    $wbIds = New-Object 'Collections.Generic.List[string]'
    $wbRefs = New-Object 'Collections.Generic.List[string]'
    $wbRow = New-Object 'Collections.Generic.List[int]'        # file -> its row in $wb arrays
    $rowLib = New-Object 'int[]' $wb.Ids.Length                # workbook row -> library number, -1 = no CSV has it
    for ($i = 0; $i -lt $rowLib.Length; $i++) { $rowLib[$i] = -1 }
    $wbRepeated = 0
    for ($i = 0; $i -lt $wb.Ids.Length; $i++) {
        if ($wb.Types[$i] -eq '1') { continue }
        $id = if ($wb.Ids[$i]) { Format-SpCompareId $wb.Ids[$i] } else { '' }
        $ref = if ($wb.Refs[$i]) { $wb.Refs[$i].Trim() } else { '' }
        if (-not $id -and -not $ref) { continue }
        if (($id -and $byId.ContainsKey($id)) -or ($ref -and $byRef.ContainsKey($ref))) { $wbRepeated++; continue }
        $k = $wbIds.Count
        $wbIds.Add($id); $wbRefs.Add($ref); $wbRow.Add($i)
        if ($id) { $byId[$id] = $k }
        if ($ref) { $byRef[$ref] = $k }
    }
    $found = New-Object 'bool[]' $wbIds.Count
    $wbColumns = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($c in $wb.Columns) { [void]$wbColumns.Add($c) }
    $spLibs = @(if ($wb.LibColumns) { $wb.LibColumns })
    & $Log "Workbook: $(& $nf $wbIds.Count) files, $($wb.Columns.Count) columns, column list of $($spLibs.Count) libraries. Reading $($csvFiles.Count) CSV files ..."

    # ---- per-library CSVs ----------------------------------------------------------
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $libs = [ordered]@{}
    $columns = @{}
    $missing = New-Object 'Collections.Generic.List[object[]]'
    $changed = New-Object 'Collections.Generic.List[object[]]'
    $problems = New-Object 'Collections.Generic.List[object[]]'
    $repeated = 0; $folders = 0; $rowsRead = 0; $badRows = 0; $badFiles = 0
    $problemMax = 1000
    foreach ($file in $csvFiles) {
        $rel = $file.FullName.Substring($root.Length).TrimStart('\')
        $name = $file.Name -replace '(_\d{8}_\d{6})?\.csv$', ''
        $dir = [string][IO.Path]::GetDirectoryName($rel)
        $libKey = "$dir\$name"
        if (-not $libs.Contains($libKey)) {
            $libs[$libKey] = @{ Site = $dir; Name = $name; System = (Test-SpSystemCsvName $name); Number = $libs.Count; Csvs = 0; Folders = 0; Files = 0; Found = 0; Missing = 0; Problems = 0; NoWbCol = 0; NoCsvCol = 0; BlankCols = 0; HasHeaders = $false; SpLib = $null; SpTried = $false
                                NoCsvNames = (New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase))
                                Headers = (New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)); Cols = @{} }
        }
        $lib = $libs[$libKey]
        $lib.Csvs++
        $p = $null
        try {
            $p = Open-SpCsvParser $file.FullName
            if ($p.EndOfData) { $badFiles++; $lib.Problems++; $problems.Add([object[]]@($rel, 'Empty file (no header row)')); continue }
            $headers = $p.ReadFields()
            $ix = Get-SpHeaderIndex $headers
            if (-not $ix.ContainsKey('UniqueId') -and -not $ix.ContainsKey('FileRef')) {
                $badFiles++; $lib.Problems++; $problems.Add([object[]]@($rel, 'Skipped: no UniqueId or FileRef column')); continue
            }
            $iId = if ($ix.ContainsKey('UniqueId')) { [int]$ix['UniqueId'] } else { -1 }
            $iRef = if ($ix.ContainsKey('FileRef')) { [int]$ix['FileRef'] } else { -1 }
            $iType = if ($ix.ContainsKey('FSObjType')) { [int]$ix['FSObjType'] } else { -1 }
            $width = $headers.Length
            # value check: position of each compared column in this CSV (-1 = not in it)
            $vIdx = $null
            if ($vc) { $vIdx = [int[]]@(foreach ($n in $vc.Names) { if ($ix.ContainsKey($n)) { [int]$ix[$n] } else { -1 } }) }
            # values are only counted for columns the workbook lacks: far faster than all of them
            $extra = @(foreach ($n in $ix.Keys) { if (-not $wbColumns.Contains($n)) { @{ Name = $n; Index = [int]$ix[$n]; Values = 0 } } })
            $sNames = [string[]]@($ix.Keys | Where-Object { $wbColumns.Contains($_) })
            $sCount = $sNames.Length
            $sIdx = New-Object 'int[]' $sCount
            $sHas = New-Object 'bool[][]' $sCount
            $sCsv = New-Object 'int[]' $sCount
            $sCsvOnly = New-Object 'int[]' $sCount
            $sWbOnly = New-Object 'int[]' $sCount
            for ($j = 0; $j -lt $sCount; $j++) { $sIdx[$j] = $ix[$sNames[$j]]; $sHas[$j] = $wb.Cells[$sNames[$j]].Has }
            # a library scanned more than once: a column counts as present only if every scan has it
            if ($lib.HasHeaders) { $lib.Headers.IntersectWith([string[]]@($ix.Keys)) }
            else { foreach ($n in $ix.Keys) { [void]$lib.Headers.Add($n) }; $lib.HasHeaders = $true }
            while (-not $p.EndOfData) {
                $line = $p.LineNumber            # the line this record starts on (header = line 1)
                try { $f = $p.ReadFields() }
                catch {
                    $badRows++; $lib.Problems++
                    if ($problems.Count -lt $problemMax) { $problems.Add([object[]]@($rel, "Unreadable row: $($_.Exception.Message.Split("`n")[0])")) }
                    continue
                }
                if ($null -eq $f) { continue }
                if ($f.Length -ne $width) {
                    $badRows++; $lib.Problems++
                    if ($problems.Count -lt $problemMax) { $problems.Add([object[]]@($rel, "Row before line $($p.LineNumber) has $($f.Length) fields, the header has $width")) }
                    continue
                }
                $rowsRead++
                if (($rowsRead % 5000) -eq 0 -and $Progress) { & $Progress $rowsRead $rel }
                if ($spLibs.Count -and -not $lib.SpTried -and $iRef -ge 0 -and $f[$iRef]) {
                    $lib.SpTried = $true
                    $lib.SpLib = Find-SpLibByRef $spLibs $f[$iRef]
                    if ($lib.SpLib) { $lib.SpLib.Used = $true }
                }
                if ($iType -ge 0 -and $f[$iType] -eq '1') { $folders++; $lib.Folders++; continue }
                $id = if ($iId -ge 0) { Format-SpCompareId $f[$iId] } else { '' }
                $ref = if ($iRef -ge 0) { $f[$iRef].Trim() } else { '' }
                if (-not $id -and -not $ref) { continue }
                if (-not $seen.Add($(if ($id) { $id } else { $ref }))) { $repeated++; continue }
                $lib.Files++
                foreach ($e in $extra) { if ($f[$e.Index]) { $e.Values++ } }
                $k = -1
                $raw = -1
                if ($id -and $byId.TryGetValue($id, [ref]$k)) {
                    $raw = $wbRow[$k]
                    if ($ref -and $wbRefs[$k] -and -not $ref.Equals($wbRefs[$k], [StringComparison]::OrdinalIgnoreCase)) {
                        $changed.Add([object[]]@('Moved or renamed (same UniqueId, other path)', $ref, $wbRefs[$k], $id, $wbIds[$k], $rel))
                    }
                } elseif ($ref -and $byRef.TryGetValue($ref, [ref]$k)) {
                    $raw = $wbRow[$k]
                    if ($id -and $wbIds[$k]) {
                        $changed.Add([object[]]@('Replaced (same path, other UniqueId)', $ref, $wbRefs[$k], $id, $wbIds[$k], $rel))
                    }
                } else {
                    $lib.Missing++
                    $missing.Add([object[]]@($(if ($lib.System) { 'System' } else { 'Content' }), $dir, $name, $ref, $id, $rel, [long]$line))
                }
                if ($raw -ge 0) {
                    $found[$k] = $true
                    $lib.Found++
                    $rowLib[$raw] = $lib.Number
                    if ($vc) { $vc.Row($f, $vIdx, $raw, $lib.Number, $(if ($ref) { $ref } else { $wbRefs[$k] }), $rel, [long]$line) }
                }
                for ($j = 0; $j -lt $sCount; $j++) {
                    if ($f[$sIdx[$j]].Length) {
                        $sCsv[$j]++
                        if ($raw -ge 0 -and -not $sHas[$j][$raw]) { $sCsvOnly[$j]++ }
                    } elseif ($raw -ge 0 -and $sHas[$j][$raw]) { $sWbOnly[$j]++ }
                }
            }
            foreach ($e in $extra) { Add-SpLibColumn $lib $columns $e.Name $e.Values 0 0 }
            for ($j = 0; $j -lt $sCount; $j++) { Add-SpLibColumn $lib $columns $sNames[$j] $sCsv[$j] $sCsvOnly[$j] $sWbOnly[$j] }
        } catch {
            $badFiles++; $lib.Problems++
            $problems.Add([object[]]@($rel, "Not read completely: $($_.Exception.Message.Split("`n")[0])"))
        } finally {
            if ($p) { $p.Dispose() }
        }
    }

    # ---- result --------------------------------------------------------------------
    $onlyWb = New-Object 'Collections.Generic.List[object[]]'
    for ($i = 0; $i -lt $found.Length; $i++) {
        if (-not $found[$i]) { $onlyWb.Add([object[]]@($wbRefs[$i], $wbIds[$i])) }
    }
    $sortedMissing = New-Object 'Collections.Generic.List[object[]]'
    foreach ($m in $missing) { if ($m[0] -eq 'Content') { $sortedMissing.Add($m) } }
    $missingContent = $sortedMissing.Count
    foreach ($m in $missing) { if ($m[0] -ne 'Content') { $sortedMissing.Add($m) } }
    $missingSystem = $missing.Count - $missingContent
    $uniqueFiles = $seen.Count
    $matched = $uniqueFiles - $missing.Count
    $problemCount = $badRows + $badFiles

    # for each library: columns the workbook has values for, but its CSV does not have
    $libList = @($libs.Values)
    $wbValues = @{}
    $csvGaps = @{}
    $byLib = New-Object 'Collections.Generic.List[object[]]'
    foreach ($n in $wb.Cells.Keys) {
        $list = $wb.Cells[$n].Rows
        $wbValues[$n] = $list.Count
        if ($script:ExportOnlyColumns -contains $n) { continue }
        $counts = New-Object 'int[]' $libList.Count
        $firstRow = New-Object 'int[]' $libList.Count
        foreach ($row in $list) { $l = $rowLib[$row]; if ($l -ge 0) { if (-not $counts[$l]) { $firstRow[$l] = $row }; $counts[$l]++ } }
        for ($l = 0; $l -lt $counts.Length; $l++) {
            if ($counts[$l] -and $libList[$l].HasHeaders -and -not $libList[$l].Headers.Contains($n)) {
                $byLib.Add([object[]]@('CSV', 'Column not in this CSV', $n, $(if ($libList[$l].System) { 'System' } else { 'Content' }), $libList[$l].Site, $libList[$l].Name, [int]$counts[$l], [long]($firstRow[$l] + 2)))
                $libList[$l].NoCsvCol++
                $csvGaps[$n] = 1 + [int]$csvGaps[$n]
                [void]$libList[$l].NoCsvNames.Add($n)
            }
        }
    }
    # a CSV without rows has no FileRef to go by: match its file name, "<title>_<folder>", to the library
    foreach ($l in $libList) {
        if ($l.SpTried -or -not $spLibs.Count) { continue }
        $csvName = ConvertTo-SpMatchName "$($l.Site)$($l.Name)"
        $hit = $spLibs | Where-Object { $csvName.EndsWith($_.MatchName) } | Sort-Object { $_.MatchName.Length } -Descending | Select-Object -First 1
        if ($hit) { $l.SpLib = $hit; $hit.Used = $true }
    }
    # columns the library has in SharePoint but its CSV lacks, even if every value is empty
    foreach ($l in $libList) {
        if (-not $l.SpLib -or -not $l.HasHeaders) { continue }
        foreach ($n in $l.SpLib.Exported) {
            if ($l.Headers.Contains($n) -or $l.NoCsvNames.Contains($n) -or $script:ExportOnlyColumns -contains $n) { continue }
            $byLib.Add([object[]]@('CSV', 'Column in the SharePoint library, not in this CSV', $n, $(if ($l.System) { 'System' } else { 'Content' }), $l.Site, $l.Name, 0))
            $l.NoCsvCol++
            $csvGaps[$n] = 1 + [int]$csvGaps[$n]
        }
    }
    foreach ($l in $libList) {
        $type = if ($l.System) { 'System' } else { 'Content' }
        foreach ($c in $l.Cols.Values) {
            if (-not $wbColumns.Contains($c.Name)) {
                $why = if (-not $l.SpLib) { 'Column not in the xlsx' }
                       elseif ($l.SpLib.All.Contains($c.Name)) { 'Column in the SharePoint library, not exported to the xlsx' }
                       else { 'Column not in the xlsx, and not a field of this SharePoint library' }
                if ($c.Values) { $l.NoWbCol++ } else { $why += ' (empty in this CSV)' }
                $byLib.Add([object[]]@('xlsx', $why, $c.Name, $type, $l.Site, $l.Name, [int]$c.Values))
                continue
            }
            if ($c.CsvOnly) { $byLib.Add([object[]]@('xlsx', 'Column in both, value blank in the xlsx', $c.Name, $type, $l.Site, $l.Name, [int]$c.CsvOnly)); $l.BlankCols++ }
            if ($c.WbOnly) { $byLib.Add([object[]]@('CSV', 'Column in both, value blank in the CSV', $c.Name, $type, $l.Site, $l.Name, [int]$c.WbOnly)); $l.BlankCols++ }
        }
    }
    # where each missing column is, for the Column check sheet. CSVs directly in the chosen folder have no subfolder name.
    $topFolder = "(top folder: $([IO.Path]::GetFileName($root)))"
    $missingAt = @{}
    foreach ($b in $byLib) {
        if (-not $b[4]) { $b[4] = $topFolder }
        if ($b[1] -like 'Column in both*') { continue }
        if (-not $missingAt.ContainsKey($b[2])) { $missingAt[$b[2]] = New-Object 'Collections.Generic.List[string]' }
        $missingAt[$b[2]].Add("$($b[4]) | $($b[5])")
    }
    $byLib = @($byLib | Sort-Object { $_[0] }, { $_[1] }, { $_[2] }, { $_[4] }, { $_[5] })

    $colRows = New-Object 'Collections.Generic.List[object[]]'
    $noWb = 0; $noWbValues = 0; $noCsv = 0; $noCsvValues = 0; $noCsvSome = 0; $blankWb = 0; $blankCsv = 0
    $names = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($n in $columns.Keys) { [void]$names.Add($n) }
    foreach ($n in $wbColumns) { [void]$names.Add($n) }
    foreach ($n in $names) {
        $inCsv = $columns.ContainsKey($n)
        $inWb = $wbColumns.Contains($n)
        $c = if ($inCsv) { $columns[$n] } else { @{ Csvs = 0; Values = 0; CsvOnly = 0; WbOnly = 0 } }
        $wv = if ($inWb) { [int]$wbValues[$n] } else { 0 }
        if ($script:ExportOnlyColumns -contains $n) { $where = 'Added by the export' }
        elseif (-not $inWb) { $where = 'Missing in xlsx'; $noWb++; if ($c.Values) { $noWbValues++ } }
        elseif (-not $inCsv) { $where = 'Missing in CSV'; $noCsv++; if ($wv) { $noCsvValues++ } }
        elseif ($csvGaps.ContainsKey($n)) { $where = 'Missing in CSV (some)'; $noCsvSome++ }
        else { $where = 'In both' }
        if ($inWb -and $inCsv) { if ($c.CsvOnly) { $blankWb++ }; if ($c.WbOnly) { $blankCsv++ } }
        $at = if ($where -like 'Missing*' -and $missingAt.ContainsKey($n)) { @($missingAt[$n] | Sort-Object) } else { @() }
        $atText = $at -join '; '
        # one Excel cell holds 32,767 characters; every location is still listed in Column check by CSV
        if ($atText.Length -gt 32000) { $atText = $atText.Substring(0, 32000) + ' ... (see Column check by CSV for the rest)' }
        $colRows.Add([object[]]@($where, $n, [int]$c.Csvs, $(if ($inWb) { 'Yes' } else { 'No' }), [int]$c.Values, $wv, [int]$c.CsvOnly, [int]$c.WbOnly, [int]$at.Count, $atText))
    }
    $colRows = @($colRows | Sort-Object { $_[0] }, @{ Expression = { [Math]::Max($_[4], $_[5]) }; Descending = $true }, { $_[1] })
    $gapLibs = @($libList | Where-Object { $_.NoCsvCol }).Count
    $columnResult = "Missing in xlsx: $noWb columns ($noWbValues with values). Missing in CSV: $noCsv columns in all CSVs ($noCsvValues with values), $noCsvSome columns in some CSVs ($gapLibs libraries). " +
                    "In both but blank on some files: $blankWb in the xlsx, $blankCsv in the CSV."

    $noCsvLibs = @($spLibs | Where-Object { -not $_.Used })
    if ($spLibs.Count) { $columnResult += " Exported libraries with no CSV: $($noCsvLibs.Count)." }
    else { $columnResult += ' This xlsx has no Library columns sheet, so a column that is empty in the xlsx cannot be checked: export it again to add the sheet.' }
    $libHeader = [string[]]@('Status', 'Type', 'Site folder', 'Library (CSV name)', 'CSV files', 'Folders', 'Files', 'In workbook', 'Missing',
                             'Columns missing in xlsx', 'Columns missing in CSV', 'Columns with blanks')
    $libRows = New-Object 'Collections.Generic.List[object[]]'
    $sortedLibs = @($libs.Values | Sort-Object @{ Expression = { $_.Problems -gt 0 }; Descending = $true }, @{ Expression = { $_.NoCsvCol -gt 0 }; Descending = $true }, @{ Expression = { $_.System } }, @{ Expression = { $_.Missing }; Descending = $true }, @{ Expression = { $_.Site } }, @{ Expression = { $_.Name } })
    foreach ($l in $sortedLibs) {
        $state = if ($l.Problems) { 'Not fully read (see Problems)' } elseif ($l.NoCsvCol) { 'Column missing in CSV' } elseif ($l.Missing -eq 0) { 'OK' } elseif ($l.Found -eq 0) { 'Library not in workbook' } else { 'Some files missing' }
        $libRows.Add([object[]]@($state, $(if ($l.System) { 'System' } else { 'Content' }), $l.Site, $l.Name, [int]$l.Csvs, [int]$l.Folders, [int]$l.Files, [int]$l.Found, [int]$l.Missing,
                                [int]$l.NoWbCol, [int]$l.NoCsvCol, [int]$l.BlankCols))
    }
    foreach ($l in $noCsvLibs) {
        $libRows.Insert(0, [object[]]@('No CSV for this library', '', $l.WebPath, $l.Title, 0, 0, 0, 0, 0, 0, 0, 0))
    }
    $verdict = if ($problemCount) { 'NOT CONCLUSIVE: some CSV rows or files could not be read (see Problems)' }
               elseif ($missingContent) { 'FILES MISSING: content library files are not in the workbook (see Missing from workbook)' }
               elseif ($missingSystem) { 'All content library files are in the workbook; only SharePoint system library files are missing' }
               else { 'All files are in the workbook' }
    # ---- value check ---------------------------------------------------------------
    $valRows = New-Object 'Collections.Generic.List[object[]]'
    $valLibRows = New-Object 'Collections.Generic.List[object[]]'
    $valDiffRows = New-Object 'Collections.Generic.List[object[]]'
    $valSame = 0; $valFormat = 0; $valDiffer = 0; $valCells = [long]0; $valBlank = [long]0
    $valueResult = if ($NoValues) { 'Values not compared (-NoValues).' } else { 'Values not compared: no column is in both.' }
    if ($vc) {
        & $Log "Value check: reading the xlsx text of $(& $nf $vc.Diffs.Count) listed cells ..."
        Get-SpWorkbookText $wb $vc.Diffs
        $K = [MetadataHorizontal.SpValue]
        $example = @{}
        foreach ($d in $vc.Diffs) {
            $l = $libList[$d.Lib]
            $valDiffRows.Add([object[]]@($K::Names[$d.Kind], $vc.Names[$d.Col], $(if ($l.System) { 'System' } else { 'Content' }), $l.Site, $l.Name, $d.Ref,
                                         [long]($d.Raw + 2), $d.File, [long]$d.Line, $d.Csv, $d.Xlsx))
            $key = "$($d.Col)"
            if (-not $example.ContainsKey($key) -or ($d.Kind -eq $K::Different -and $example[$key].Kind -ne $K::Different)) { $example[$key] = $d }
        }
        for ($j = 0; $j -lt $vc.Names.Length; $j++) {
            $t = @(for ($c = 0; $c -lt $K::Kinds; $c++) { [long]$vc.Totals[$j, $c] })
            $files = ($t | Measure-Object -Sum).Sum
            if (-not $files) { continue }
            $res = if ($t[5]) { 'Different values' } elseif ($t[3] -or $t[4]) { 'Blank on one side' }
                   elseif ($t[1] -or $t[2]) { 'Same value, other format / time zone' } else { 'All the same' }
            switch ($res) { 'All the same' { $valSame++ } 'Same value, other format / time zone' { $valFormat++ } default { $valDiffer++ } }
            $valCells += $t[5]; $valBlank += $t[3] + $t[4]
            $ex = $example["$j"]
            $valRows.Add([object[]]@($res, $vc.Names[$j], [long]$files, $t[0], $t[1], $t[2], $vc.ShiftText($j), $t[3], $t[4], $t[5],
                                     $(if ($ex) { $K::Names[$ex.Kind] } else { '' }), $(if ($ex) { $ex.Csv } else { '' }), $(if ($ex) { $ex.Xlsx } else { '' }), $(if ($ex) { $ex.Ref } else { '' }),
                                     $(if ($ex) { [long]($ex.Raw + 2) } else { '' }), $(if ($ex) { $ex.File } else { '' }), $(if ($ex) { [long]$ex.Line } else { '' })))
        }
        $order = @{ 'Different values' = 0; 'Blank on one side' = 1; 'Same value, other format / time zone' = 2; 'All the same' = 3 }
        $valRows = @($valRows | Sort-Object { $order[$_[0]] }, @{ Expression = { $_[9] + $_[7] + $_[8] }; Descending = $true }, { $_[1] })
        foreach ($libNo in $vc.ByLib.Keys) {
            $m = $vc.ByLib[$libNo]
            $l = $libList[$libNo]
            for ($j = 0; $j -lt $vc.Names.Length; $j++) {
                $t = @(for ($c = 0; $c -lt $K::Kinds; $c++) { [long]$m[$j, $c] })
                if (-not ($t[1] + $t[2] + $t[3] + $t[4] + $t[5])) { continue }
                $valLibRows.Add([object[]]@($vc.Names[$j], $(if ($l.System) { 'System' } else { 'Content' }), $l.Site, $l.Name, [long]($t | Measure-Object -Sum).Sum, $t[0], $t[1], $t[2], $t[3], $t[4], $t[5]))
            }
        }
        $valLibRows = @($valLibRows | Sort-Object { $_[0] }, @{ Expression = { $_[10] }; Descending = $true }, { $_[2] }, { $_[3] })
        if ($vc.Skipped) { $valDiffRows.Add([object[]]@('', "Only the first $($vc.MaxPerColumn) cells per column are listed; $(& $nf $vc.Skipped) more are counted in Value check", '', '', '', '', '', '', '', '', '')) }
        $valueResult = "Values of $($valRows.Count) columns in both: $valSame all the same, $valFormat same apart from format or time zone, $valDiffer with blanks or different values " +
                       "($(& $nf $valCells) cells different, $(& $nf $valBlank) blank on one side)."
    }

    $summary = New-Object 'Collections.Generic.List[object[]]'
    foreach ($row in @(
            @('Result', $verdict),
            @('Column result', $columnResult),
            @('Value result', $valueResult),
            @('Workbook', $WorkbookPath),
            @('CSV folder', $root),
            @('CSV files read', [int]$csvFiles.Count),
            @('Libraries in the CSV folder', [int]$libs.Count),
            @('CSV rows read', [int]$rowsRead),
            @('  Folder rows (not compared)', [int]$folders),
            @('  Repeated files (same file in an earlier CSV)', [int]$repeated),
            @('Unique files in the CSVs', [int]$uniqueFiles),
            @('Unique files in the workbook', [int]$wbIds.Count),
            @('Repeated rows in the workbook', [int]$wbRepeated),
            @('In both', [int]$matched),
            @('Missing from workbook: content libraries', [int]$missingContent),
            @('Missing from workbook: system libraries', [int]$missingSystem),
            @('Only in workbook', [int]$onlyWb.Count),
            @('In both, but path or UniqueId differs', [int]$changed.Count),
            @('Columns missing in xlsx', [int]$noWb),
            @('Libraries in the Library columns sheet', [int]$spLibs.Count),
            @('Exported libraries with no CSV', [int]$noCsvLibs.Count),
            @('Columns missing in all CSVs', [int]$noCsv),
            @('Columns missing in some CSVs', [int]$noCsvSome),
            @('Libraries with a column missing in their CSV', [int]$gapLibs),
            @('Problems (unreadable rows or files)', [int]$problemCount),
            @('Value check: columns compared', [int]$valRows.Count),
            @('  All values the same', [int]$valSame),
            @('  Same apart from format or time zone', [int]$valFormat),
            @('  Blank on one side or different', [int]$valDiffer),
            @('Cells with different values', [long]$valCells),
            @('Cells blank on one side', [long]$valBlank))) {
        $summary.Add([object[]]$row)
    }
    if ($problemCount -gt $problems.Count) {
        $problems.Add([object[]]@('', "Only the first $problemMax problems are listed; $problemCount in total"))
    }

    $outDir = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($ReportPath))
    if ($outDir) { [void](New-Item -ItemType Directory -Force -Path $outDir) }
    $pkg = New-Object OfficeOpenXml.ExcelPackage
    try {
        Add-SpReportSheet $pkg 'Summary' @('Item', 'Value') $summary
        $sheet = $pkg.Workbook.Worksheets['Summary']
        $sheet.Column(1).Width = 46
        $sheet.Column(2).Width = 90
        $sheet.Column(2).Style.HorizontalAlignment = 'Left'
        Add-SpReportSheet $pkg 'Libraries' $libHeader $libRows
        Add-SpReportSheet $pkg 'Missing from workbook' @('Type', 'Site folder', 'Library (CSV name)', 'FileRef', 'UniqueId', 'CSV file', 'CSV line') $sortedMissing
        Add-SpReportSheet $pkg 'Only in workbook' @('FileRef', 'UniqueId') $onlyWb
        Add-SpReportSheet $pkg 'Path or Id differs' @('What', 'CSV FileRef', 'Workbook FileRef', 'CSV UniqueId', 'Workbook UniqueId', 'CSV file') $changed
        Add-SpReportSheet $pkg 'Column check' @('Result', 'Column', 'CSV files with it', 'In xlsx', 'Files with value in CSV', 'Files with value in xlsx', 'Value only in CSV', 'Value only in xlsx', 'Libraries missing it', 'Where missing (site | library)') $colRows
        $pkg.Workbook.Worksheets['Column check'].Column(10).Width = 90
        Add-SpReportSheet $pkg 'Column check by CSV' @('Missing in', 'Problem', 'Column', 'Type', 'Site folder', 'Library (CSV name)', 'Files affected', 'First xlsx row with a value') $byLib
        Add-SpReportSheet $pkg 'Value check' @('Result', 'Column', 'Files compared', 'Same', 'Same, other format', 'Same time, other time zone', 'Time shift',
                                               'Blank in xlsx', 'Blank in CSV', 'Different', 'Example', 'Example CSV value', 'Example xlsx value', 'Example FileRef',
                                               'Example xlsx row', 'Example CSV file', 'Example CSV line') $valRows
        foreach ($c in 12, 13, 14) { $pkg.Workbook.Worksheets['Value check'].Column($c).Width = 50 }
        Add-SpReportSheet $pkg 'Value check by CSV' @('Column', 'Type', 'Site folder', 'Library (CSV name)', 'Files compared', 'Same', 'Same, other format',
                                                      'Same time, other time zone', 'Blank in xlsx', 'Blank in CSV', 'Different') $valLibRows
        Add-SpReportSheet $pkg 'Value differences' @('Result', 'Column', 'Type', 'Site folder', 'Library (CSV name)', 'FileRef', 'xlsx row', 'CSV file', 'CSV line', 'CSV value', 'xlsx value') $valDiffRows
        foreach ($c in 6, 8, 10, 11) { $pkg.Workbook.Worksheets['Value differences'].Column($c).Width = 50 }
        Add-SpReportSheet $pkg 'Problems' @('CSV file', 'Problem') $problems
        $pkg.SaveAs((New-Object IO.FileInfo($ReportPath)))
    } finally {
        $pkg.Dispose()
    }

    $viewRows = New-Object 'Collections.Generic.List[string[]]'
    foreach ($r in $libRows) { $viewRows.Add([string[]]@($r | ForEach-Object { [Convert]::ToString($_, $inv) })) }
    & $Log ("$verdict`nIn both $(& $nf $matched); missing from workbook $(& $nf $missingContent) content + $(& $nf $missingSystem) system; " +
            "only in workbook $(& $nf $onlyWb.Count); problems $(& $nf $problemCount)`n$columnResult`n$valueResult`nReport: $ReportPath  [$($sw.Elapsed.TotalSeconds.ToString('0.0', $inv))s]")
    return [ordered]@{
        in_both = [int]$matched; missing_content = [int]$missingContent; missing_system = [int]$missingSystem
        only_in_workbook = [int]$onlyWb.Count; differs = [int]$changed.Count; problems = [int]$problemCount
        value_columns = [int]$valRows.Count; value_columns_same = [int]$valSame; value_columns_format = [int]$valFormat
        value_columns_differ = [int]$valDiffer; value_cells_different = [long]$valCells; value_cells_blank = [long]$valBlank
        Verdict = $verdict; ColumnResult = $columnResult; ValueResult = $valueResult; Path = $ReportPath
        ColumnRows = $colRows; ColumnByCsv = $byLib; ValueRows = $valRows; ValueDiffRows = $valDiffRows; MissingRows = $sortedMissing
        View = @{ Columns = $libHeader; Rows = $viewRows; Total = $viewRows.Count }
    }
}


# ---------------------------------------------------------------------------
# File names and scheduled exports (Windows Task Scheduler)
# ---------------------------------------------------------------------------
$script:TaskPrefix = 'MetadataHorizontal SP export - '

function Invoke-SpCompareSet {
    <# One or more combined exports against one CSV folder. Each export is compared only with the CSVs of its own sites
       (Find-SpCompareCsvs, from its Library columns sheet); a single export whose sites match no folder uses every CSV.
       One report per export in $ReportFolder; with more than one export also sharepoint_compare_summary_<time>.xlsx.
       -> @{ Rows; Path (summary or the one report); View; Single (Compare-SpExport result when one export) } #>
    param([string[]] $WorkbookPaths, [string] $CsvFolder, [string] $ReportFolder,
          [scriptblock] $Log = { param($m) Write-Host $m }, [scriptblock] $Progress, [switch] $NoValues)
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $header = [string[]]@('Workbook', 'Result', 'Sites', 'CSV folders', 'CSV files', 'Sites with no CSV folder', 'In both', 'Missing from xlsx (content)',
                          'Missing from xlsx (system)', 'Only in xlsx', 'Problems', 'Columns compared', 'Columns all same', 'Columns same apart from format/time zone',
                          'Columns blank or different', 'Cells different', 'Cells blank on one side', 'Column result', 'Report', 'Matched folders')
    $rows = New-Object 'Collections.Generic.List[object[]]'
    $colSheet = New-Object 'Collections.Generic.List[object[]]'     # every column with a problem, per export
    $diffSheet = New-Object 'Collections.Generic.List[object[]]'    # cells, with xlsx row and CSV line
    $gapSheet = New-Object 'Collections.Generic.List[object[]]'     # column problems per CSV
    $missSheet = New-Object 'Collections.Generic.List[object[]]'    # files only in the CSVs, with CSV line
    $perColumn = 100; $maxRows = 1000000
    $single = $null
    $n = 0
    foreach ($wbPath in $WorkbookPaths) {
        $n++
        $base = [IO.Path]::GetFileNameWithoutExtension($wbPath)
        & $Log "[$n/$($WorkbookPaths.Count)] $base"
        $sites = @(Get-SpExportSites $wbPath)
        $set = $null
        if ($sites.Count) { $set = Find-SpCompareCsvs $sites $CsvFolder }
        $files = if ($set -and $set.Files.Count) { $set.Files } else { $null }
        if (-not $files) {
            if ($WorkbookPaths.Count -gt 1) {
                $why = if ($sites.Count) { 'No CSV folder for its sites' } else { 'Not compared: no Library columns sheet, so its sites are unknown' }
                & $Log "    $why"
                $rows.Add([object[]]@($base, $why, $sites.Count, 0, 0, $(if ($set) { $set.NoCsv -join '; ' } else { '' }), '', '', '', '', '', '', '', '', '', '', '', '', '', ''))
                continue
            }
            & $Log '    No CSV folder matches its sites: comparing with every CSV in the folder'
        } else {
            & $Log "    $($sites.Count) site(s) -> $($set.Folders.Count) CSV folder(s), $($files.Count) CSV file(s)$(if ($set.NoCsv.Count) { "; no CSV folder for $($set.NoCsv -join ', ')" })"
        }
        $report = Join-Path $ReportFolder "sharepoint_compare_${base}_$stamp.xlsx"
        try {
            $r = Compare-SpExport -WorkbookPath $wbPath -CsvFolder $CsvFolder -ReportPath $report -Log $Log -Progress $Progress -CsvFiles $files -NoValues:$NoValues
            if ($WorkbookPaths.Count -eq 1) { $single = $r }
            # one row per column with a problem: the column check and the value check side by side
            $cols = [ordered]@{}
            foreach ($c in $r.ColumnRows) {
                if ($c[0] -eq 'In both' -or $c[0] -eq 'Added by the export') { continue }
                $cols[$c[1]] = [object[]]@($base, $c[1], $c[0], '', '', '', '', '', '', '', '', '', '', '', '', '', $c[9])
            }
            foreach ($v in $r.ValueRows) {
                if ($v[0] -eq 'All the same') { continue }
                $row = if ($cols.Contains($v[1])) { $cols[$v[1]] } else { [object[]]@($base, $v[1], 'In both', '', '', '', '', '', '', '', '', '', '', '', '', '', '') }
                $row[3] = $v[0]; $row[4] = $v[2]; $row[5] = $v[3]; $row[6] = $v[4]; $row[7] = $v[5]; $row[8] = $v[6]; $row[9] = $v[7]; $row[10] = $v[8]; $row[11] = $v[9]
                $row[12] = $v[14]; $row[13] = $v[15]; $row[14] = $v[16]; $row[15] = "CSV: $($v[11]) | xlsx: $($v[12])"
                $cols[$v[1]] = $row
            }
            foreach ($c in $cols.Values) { $colSheet.Add($c) }
            $perCol = @{}
            foreach ($d in $r.ValueDiffRows) {
                if ($diffSheet.Count -ge $maxRows) { break }
                $perCol[$d[1]] = 1 + [int]$perCol[$d[1]]
                if ($perCol[$d[1]] -le $perColumn) { $diffSheet.Add([object[]](@($base) + $d)) }
            }
            foreach ($g in $r.ColumnByCsv) { if ($gapSheet.Count -lt $maxRows) { $gapSheet.Add([object[]](@($base) + $g)) } }
            foreach ($m in $r.MissingRows) { if ($missSheet.Count -lt $maxRows) { $missSheet.Add([object[]](@($base) + $m)) } }
            $rows.Add([object[]]@($base, $r.Verdict, $sites.Count, $(if ($set) { $set.Folders.Count } else { '' }), $(if ($files) { $files.Count } else { 'all' }),
                                  $(if ($set) { $set.NoCsv -join '; ' } else { '' }), $r.in_both, $r.missing_content, $r.missing_system, $r.only_in_workbook, $r.problems,
                                  $r.value_columns, $r.value_columns_same, $r.value_columns_format, $r.value_columns_differ, $r.value_cells_different, $r.value_cells_blank,
                                  $r.ColumnResult, $report, $(if ($set) { $set.Folders -join '; ' } else { '' })))
        } catch {
            & $Log "    ERROR: $($_.Exception.Message)"
            $rows.Add([object[]]@($base, "ERROR: $($_.Exception.Message)", $sites.Count, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', ''))
        }
        [GC]::Collect()
    }
    $path = if ($single) { $single.Path } else { $null }
    if ($WorkbookPaths.Count -gt 1) {
        $path = Join-Path $ReportFolder "sharepoint_compare_summary_$stamp.xlsx"
        $pkg = New-Object OfficeOpenXml.ExcelPackage
        try {
            Add-SpReportSheet $pkg 'Summary' $header $rows
            Add-SpReportSheet $pkg 'Columns' @('Workbook', 'Column', 'Column check', 'Value check', 'Files compared', 'Same', 'Same, other format',
                                               'Same time, other time zone', 'Time shift', 'Blank in xlsx', 'Blank in CSV', 'Different',
                                               'Example xlsx row', 'Example CSV file', 'Example CSV line', 'Example values', 'Where missing (site | library)') $colSheet
            Add-SpReportSheet $pkg 'Value differences' @('Workbook', 'Result', 'Column', 'Type', 'Site folder', 'Library (CSV name)', 'FileRef',
                                                         'xlsx row', 'CSV file', 'CSV line', 'CSV value', 'xlsx value') $diffSheet
            Add-SpReportSheet $pkg 'Column check by CSV' @('Workbook', 'Missing in', 'Problem', 'Column', 'Type', 'Site folder', 'Library (CSV name)',
                                                           'Files affected', 'First xlsx row with a value') $gapSheet
            Add-SpReportSheet $pkg 'Missing from workbook' @('Workbook', 'Type', 'Site folder', 'Library (CSV name)', 'FileRef', 'UniqueId', 'CSV file', 'CSV line') $missSheet
            $pkg.SaveAs((New-Object IO.FileInfo($path)))
        } finally { $pkg.Dispose() }
        & $Log "Summary: $path"
    }
    $viewRows = New-Object 'Collections.Generic.List[string[]]'
    foreach ($r in $rows) { $viewRows.Add([string[]]@($r | ForEach-Object { [Convert]::ToString($_, $script:Inv) })) }
    return @{ Rows = $rows; Path = $path; Single = $single; View = @{ Columns = $header; Rows = $viewRows; Total = $viewRows.Count } }
}

function Expand-SpFileName([string] $Name, [datetime] $When = (Get-Date)) {
    <# {date} -> yyyyMMdd, {time} -> HHmm; adds .xlsx unless the name already ends in .xlsx or .csv. #>
    $n = $Name.Trim() -ireplace '\{date\}', $When.ToString('yyyyMMdd') -ireplace '\{time\}', $When.ToString('HHmm')
    if (-not $n) { throw 'Enter a file name.' }
    if ($n.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) { throw "A file name cannot contain any of  \ / : * ? `" < > |  ($n)" }
    if ($n -notmatch '\.(xlsx|csv)$') { $n += '.xlsx' }
    $n
}

function Get-SpFreePath([string] $Path) {
    <# $Path, or name_2, name_3... if it exists: an unattended run never overwrites a file. #>
    if (-not (Test-Path -LiteralPath $Path)) { return $Path }
    $stem = Join-Path ([IO.Path]::GetDirectoryName($Path)) ([IO.Path]::GetFileNameWithoutExtension($Path))
    $ext = [IO.Path]::GetExtension($Path)
    for ($i = 2; ; $i++) { $p = "${stem}_$i$ext"; if (-not (Test-Path -LiteralPath $p)) { return $p } }
}

function Save-SpSchedule {
    <# Write the job file and create (or replace) its task. The password is encrypted for this Windows user on this PC.
       $Execute + $ArgumentPrefix start this app; the job file path is added behind them. #>
    param([Parameter(Mandatory)][hashtable] $Sp, [Parameter(Mandatory)] $Libraries, [Parameter(Mandatory)][string] $Name,
          [Parameter(Mandatory)][string] $Folder, [Parameter(Mandatory)][string] $FileName,
          [Parameter(Mandatory)][ValidateSet('Once', 'Daily', 'Weekly')][string] $Repeat, [Parameter(Mandatory)][datetime] $At,
          [Parameter(Mandatory)][string] $JobDir, [Parameter(Mandatory)][string] $Execute, [string] $ArgumentPrefix)
    if ($Name -notmatch '^[\w][\w \-]*$') { throw 'A schedule name can only use letters, numbers, spaces, - and _.' }
    [void](Expand-SpFileName $FileName)
    $user = ''; $secret = ''
    if ($Sp.Credential) {
        $c = $Sp.Credential
        $user = if ($c.Domain) { "$($c.Domain)\$($c.UserName)" } else { $c.UserName }
        $secret = ConvertFrom-SecureString $c.SecurePassword
    }
    $job = [ordered]@{
        Name = $Name; SiteUrl = $Sp.Url; User = $user; Password = $secret; Folder = $Folder; FileName = $FileName; Repeat = $Repeat
        Libraries = @($Libraries | ForEach-Object { [ordered]@{ WebUrl = $_.WebUrl; WebTitle = $_.WebTitle; Id = $_.Id; Title = $_.Title } })
    }
    [void][IO.Directory]::CreateDirectory($JobDir)
    $path = Join-Path $JobDir "$Name.json"
    $tmp = "$path.tmp"
    [IO.File]::WriteAllText($tmp, ($job | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    try {
        $trigger = switch ($Repeat) {
            'Once'   { New-ScheduledTaskTrigger -Once -At $At }
            'Daily'  { New-ScheduledTaskTrigger -Daily -At $At }
            'Weekly' { New-ScheduledTaskTrigger -Weekly -DaysOfWeek $At.DayOfWeek -At $At }
        }
        $action = New-ScheduledTaskAction -Execute $Execute -Argument "$ArgumentPrefix-RunSchedule `"$path`"" -WorkingDirectory ([IO.Path]::GetDirectoryName($Execute))
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
        # no time limit: a big library takes hours. Priority 5 = normal; the default (7) would slow it down.
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -DontStopOnIdleEnd -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Priority 5
        [void](Register-ScheduledTask -TaskName ($script:TaskPrefix + $Name) -Trigger $trigger -Action $action -Principal $principal -Settings $settings -Force)
        Move-Item -LiteralPath $tmp -Destination $path -Force
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    }
    $path
}

function Get-SpSchedules([string] $JobDir) {
    <# One object per scheduled export: name, repeat, file, next and last run, last result in words. #>
    foreach ($t in @(Get-ScheduledTask -TaskName "$($script:TaskPrefix)*" -ErrorAction SilentlyContinue)) {
        $name = $t.TaskName.Substring($script:TaskPrefix.Length)
        $info = $t | Get-ScheduledTaskInfo
        $job = $null
        try { $job = Get-Content -LiteralPath (Join-Path $JobDir "$name.json") -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
        $result = switch ([long]$info.LastTaskResult) {
            0       { 'OK'; break }
            1       { 'Failed: see the log'; break }
            2       { 'Saved, but libraries are missing: see the log'; break }
            267009  { 'Running now'; break }
            267011  { 'Not run yet'; break }
            default { 'Code 0x{0:X}' -f [long]$info.LastTaskResult }
        }
        if ($t.State -eq 'Running') { $result = 'Running now' }
        [pscustomobject]@{
            Name = $name
            Repeat = if ($job) { $job.Repeat } else { '?' }
            File = if ($job) { Join-Path $job.Folder $job.FileName } else { 'job file missing' }
            Libraries = if ($job) { @($job.Libraries).Count } else { 0 }
            NextRun = if ($info.NextRunTime) { $info.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { '' }
            LastRun = if ($info.LastRunTime -and $info.LastRunTime.Year -gt 2000) { $info.LastRunTime.ToString('yyyy-MM-dd HH:mm') } else { '' }
            Result = $result
        }
    }
}

function Start-SpSchedule([string] $Name) { Start-ScheduledTask -TaskName ($script:TaskPrefix + $Name) }

function Remove-SpSchedule([string] $Name, [string] $JobDir) {
    Unregister-ScheduledTask -TaskName ($script:TaskPrefix + $Name) -Confirm:$false
    $p = Join-Path $JobDir "$Name.json"
    if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }
}

function Set-SpKeepAwake {
    <# An export can take hours: keep the PC from going to sleep while this thread runs. #>
    Add-Type -Namespace MetadataHorizontal -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);'
    [void][MetadataHorizontal.Power]::SetThreadExecutionState([uint32]2147483649)
}

function Invoke-SpJobExport {
    <# One saved export (a schedule's job file or a queue item): log in, find its libraries again by id, export to a free name.
       Item counts and titles come fresh from the server. Returns the export's stats plus missing = libraries that could not be read.
       Continues the unfinished export of the same libraries in the same folder, if there is one. #>
    param([Parameter(Mandatory)] $Job, [Parameter(Mandatory)][scriptblock] $Log, [scriptblock] $RequestLog, [scriptblock] $Progress)
    $password = ''
    if ($Job.Password) {
        try { $password = (New-Object Net.NetworkCredential('', (ConvertTo-SecureString $Job.Password))).Password }
        catch { throw "The saved password can only be read by the Windows login and PC that saved it: add this export again. ($($_.Exception.Message))" }
    }
    $sp = New-SpSession $Job.SiteUrl $Job.User $password $RequestLog
    & $Log "Connected to $($sp.Url) ($($sp.Title)) as $(if ($Job.User) { $Job.User } else { [Security.Principal.WindowsIdentity]::GetCurrent().Name })"

    $found = New-Object 'Collections.Generic.Dictionary[string,hashtable]'
    foreach ($webGroup in @($Job.Libraries | Group-Object WebUrl)) {
        try {
            $web = @{ Url = $webGroup.Name; Title = $webGroup.Group[0].WebTitle }
            foreach ($l in (Get-SpLibraries $sp $web)) { $found[$l.Id] = $l }
        } catch { & $Log "Libraries of $($webGroup.Name) could not be read: $($_.Exception.Message.Split("`n")[0])" }
    }
    $libs = New-Object 'Collections.Generic.List[hashtable]'
    $missing = 0
    foreach ($l in $Job.Libraries) {
        if ($found.ContainsKey($l.Id)) { $libs.Add($found[$l.Id]) }
        else { $missing++; & $Log "MISSING: library $($l.WebTitle) / $($l.Title) was not found ($($l.WebUrl))" }
    }
    if (-not $libs.Count) { throw 'None of the chosen libraries could be read.' }

    [void][IO.Directory]::CreateDirectory($Job.Folder)
    $final = Get-SpFreePath (Join-Path $Job.Folder (Expand-SpFileName $Job.FileName))
    $r = Export-SpMetadata -Sp $sp -Libraries $libs -Path $final -Log $Log -Progress $Progress -Resume
    $r.missing = $missing
    & $Log "DONE: $($r.files) files from $($r.libraries) libraries saved to $($r.path)"
    if ($missing) { & $Log "WARNING: $missing of $(@($Job.Libraries).Count) libraries are not in this file." }
    return $r
}

function Invoke-SpScheduledExport {
    <# The unattended run: job file -> export -> log file. Returns the exit code: 0 ok, 1 failed, 2 saved but libraries missing.
       A run that failed part way is continued by the next run of the same schedule. #>
    param([string] $JobPath, [string] $LogDir)
    [void][IO.Directory]::CreateDirectory($LogDir)
    $schedLogFile = Join-Path $LogDir ('sharepoint_schedule_{0}_{1:yyyyMMdd_HHmmss}.log' -f [IO.Path]::GetFileNameWithoutExtension($JobPath), (Get-Date))
    $schedLog = { param($m) [IO.File]::AppendAllText($schedLogFile, ('[{0:yyyy-MM-dd HH:mm:ss}] {1}{2}' -f (Get-Date), $m, "`r`n")) }
    try {
        & $schedLog "Scheduled export: $JobPath"
        try { Set-SpKeepAwake } catch { & $schedLog "Could not stop the PC from sleeping: $($_.Exception.Message)" }
        $job = Get-Content -LiteralPath $JobPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $schedWatch = [Diagnostics.Stopwatch]::StartNew()
        $schedProgress = { param($percent, $text) if ($schedWatch.Elapsed.TotalSeconds -ge 60) { & $schedLog $text; $schedWatch.Restart() } }
        $r = Invoke-SpJobExport -Job $job -Log $schedLog -RequestLog $schedLog -Progress $schedProgress
        if ($r.missing) { return 2 }
        return 0
    } catch {
        & $schedLog "FAILED: $($_.Exception.Message)"
        return 1
    }
}


# ---------------------------------------------------------------------------
# Export queue: the list the app keeps, so it survives closing the app
# ---------------------------------------------------------------------------
function Read-SpQueueFile([string] $Path) {
    <# -> @{ Folder; Items }. An export that was running when the app closed waits again, and continues from its last saved page.
       A file that cannot be read is kept as .bad, and the error goes to the caller. #>
    if (-not (Test-Path -LiteralPath $Path)) { return @{ Folder = ''; Items = @() } }
    try { $d = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch {
        Move-Item -LiteralPath $Path -Destination "$Path.bad" -Force
        throw "The export queue could not be read, so it starts empty. The old list is kept in $Path.bad. ($($_.Exception.Message))"
    }
    $items = @($d.Items | Where-Object { $_ })
    foreach ($i in $items) { if ($i.State -eq 'Running') { $i.State = 'Waiting'; $i.Detail = 'Continues from its last saved page' } }
    @{ Folder = [string]$d.Folder; Items = $items }
}

function Save-SpQueueFile([string] $Path, [string] $Folder, $Items) {
    <# To a temporary name first: a crash while saving must not lose the list. Passwords in it are already encrypted for this Windows login. #>
    $tmp = "$Path.tmp"
    $text = [ordered]@{ Folder = $Folder; Items = @($Items) } | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText($tmp, $text, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}


Export-ModuleMember -Function New-SpSession, Invoke-SpGet, Get-SpWebs, Get-SpLibraries, Export-SpMetadata, New-SpRequestLog, Get-SpUnfinished, Compare-SpExport, Invoke-SpCompareSet, Get-SpExportSites, Find-SpCompareCsvs, Test-SpBigXlsx, Open-SpSheetReader,
    Expand-SpFileName, Save-SpSchedule, Get-SpSchedules, Start-SpSchedule, Remove-SpSchedule, Invoke-SpScheduledExport,
    Set-SpKeepAwake, Invoke-SpJobExport, Read-SpQueueFile, Save-SpQueueFile
