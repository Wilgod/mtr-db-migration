<#
.SYNOPSIS
  Metadata Horizontal - desktop tool (PowerShell + WinForms, no Python needed).

.DESCRIPTION
  Upload a CSV/XLSX of SharePoint metadata: the fixed columns listed in
  config.json -> HorizontalMetadata.Columns go to one row per file in the
  horizontal table; every other (non-skipped) column stays in the vertical
  "Metadata" table. "Build Matrix" makes a separate table with every distinct
  MetadataKey as a column (Metadata is left unchanged). Logic lives in
  MetadataHorizontal.psm1; the database is reached through psql.exe.

  Start: double-click mh_app.cmd, or
    powershell -NoProfile -STA -ExecutionPolicy Bypass -File mh_app.ps1
#>
param([string] $ConfigPath = (Join-Path $PSScriptRoot 'config.json'))

$ErrorActionPreference = 'Stop'
if ([Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {
    # WinForms dialogs need a single-threaded apartment
    Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList @('-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$PSCommandPath`"", '-ConfigPath', "`"$ConfigPath`"")
    return
}
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$ModulePaths = @((Join-Path $PSScriptRoot 'MetadataHorizontal.psm1'), (Join-Path $PSScriptRoot 'SharePointExport.psm1'))
Import-Module $ModulePaths -Force

$ViewRows = 500
function Col([string] $Hex) { [Drawing.ColorTranslator]::FromHtml($Hex) }
$ColorDark = Col '#19222D'; $ColorPanel = Col '#111922'; $ColorOrange = Col '#FF9900'; $ColorBlue = Col '#0073BB'
$ColorWhite = Col '#F2F3F3'; $ColorError = Col '#FF4444'; $ColorGray = Col '#9AA4AE'; $ColorField = Col '#1E2B3A'
$Dot = [string][char]0x25CF

$script:AppConfig = $null
$script:Db = $null          # connection settings once Connect succeeded
$script:Job = $null         # the one running background job
$Sync = [hashtable]::Synchronized(@{ Queue = New-Object 'Collections.Concurrent.ConcurrentQueue[object]' })


# ------------------------------------------------------------------ config
function Read-AppConfig {
    try { $script:AppConfig = Read-MhConfig $ConfigPath }
    catch {
        $script:AppConfig = New-Object psobject
        Show-Message "Cannot read $ConfigPath`n`n$($_.Exception.Message)" 'config.json' 'Error'
    }
}

function Get-FreshSettings {
    <# Fresh, validated config (re-read from disk so edits apply without restart). #>
    Read-AppConfig
    Update-ConfigLabel
    try { return Get-MhSettings $script:AppConfig }
    catch { Show-Message $_.Exception.Message 'HorizontalMetadata config' 'Error'; return $null }
}

function Update-ConfigLabel([switch] $SetCheckbox) {
    try {
        $s = Get-MhSettings $script:AppConfig
        $cfgLabel.Text = "Table: $($s.Table)   |   $($s.Cols.Count) fixed columns: " + ($s.Cols -join ', ')
        $cfgLabel.ForeColor = $ColorGray
        if ($SetCheckbox) { $chkRemove.Checked = $s.RemoveMoved }
    } catch {
        $cfgLabel.Text = $_.Exception.Message.Replace("`n", ' ')
        $cfgLabel.ForeColor = $ColorError
    }
}

function Get-ConfigValue([string] $Name, $Default) {
    $p = $script:AppConfig.PSObject.Properties[$Name]
    if ($p -and $null -ne $p.Value -and "$($p.Value)" -ne '') { return $p.Value }
    return $Default
}


# ------------------------------------------------------------------ helpers
function Show-Message([string] $Text, [string] $Title, [string] $Icon = 'Information') {
    [void][Windows.Forms.MessageBox]::Show($Text, $Title, 'OK', $Icon)
}

function Confirm-Action([string] $Text, [string] $Title) {
    [Windows.Forms.MessageBox]::Show($Text, $Title, 'YesNo', 'Question') -eq 'Yes'
}

function Write-Log([string] $Status, [string] $Message) {
    $lines = $Message.Trim() -split '\r?\n'
    $text = '[{0:HH:mm:ss}] {1,-8} | {2}' -f (Get-Date), $Status, ($lines -join ("`r`n" + (' ' * 22)))
    $logBox.AppendText($text + "`r`n")
}

function Set-Status([string] $Message, $Color = $ColorGray) {
    $status.Text = $Message
    $status.ForeColor = $Color
}

$WorkerScript = {
    param($ModulePaths, $Sync, $Title, $BodyText, $Db, $Arg)
    $ErrorActionPreference = 'Stop'
    $log = { param($m) $Sync.Queue.Enqueue(@('log', $Title, [string]$m)) }.GetNewClosure()
    $setStatus = { param($m) $Sync.Queue.Enqueue(@('status', [string]$m)) }.GetNewClosure()
    try {
        Import-Module $ModulePaths
        $body = [scriptblock]::Create($BodyText)
        @{ Ok = $true; Result = (& $body $Db $Arg $log $setStatus) }
    } catch {
        @{ Ok = $false; Error = $_.Exception.Message }
    }
}.ToString()

function Start-MhJob {
    <# Run $Body { param($Db, $Arg, $Log, $Status) } in a background runspace; one job at a time.
       $OnDone / $OnFail run on the UI thread with the result / error message. #>
    param([string] $Title, [scriptblock] $Body, $Arg, [scriptblock] $OnDone, [scriptblock] $OnFail,
          [switch] $NoConnection, [switch] $Quiet)
    if (-not $NoConnection -and -not $script:Db) { Show-Message 'Connect to the database first.' 'Not connected' 'Warning'; return }
    if ($script:Job) { Show-Message 'Another job is still running.' 'Busy'; return }
    $iss = [Management.Automation.Runspaces.InitialSessionState]::CreateDefault()
    try { $iss.ExecutionPolicy = 'Bypass' } catch { }
    $rs = [Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace($iss)
    $rs.Open()
    $ps = [PowerShell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript($WorkerScript).AddArgument($ModulePaths).AddArgument($Sync).AddArgument($Title).AddArgument($Body.ToString()).AddArgument($script:Db).AddArgument($Arg)
    $script:Job = @{ PS = $ps; Runspace = $rs; Handle = $ps.BeginInvoke(); Title = $Title
                     OnDone = $OnDone; OnFail = $OnFail; Quiet = [bool]$Quiet }
    if (-not $Quiet) {
        Set-Status "${Title}: running..."
        Write-Log $Title 'started'
    }
}

function Complete-MhJob {
    $j = $script:Job
    $script:Job = $null
    try { $res = @($j.PS.EndInvoke($j.Handle)) | Select-Object -Last 1 }
    catch { $res = @{ Ok = $false; Error = $_.Exception.Message } }
    if (-not $res) { $res = @{ Ok = $false; Error = 'the job ended without a result' } }
    $j.PS.Dispose()
    $j.Runspace.Dispose()
    if (-not $res.Ok) {
        $msg = ([string]$res.Error).Trim()
        $first = ($msg -split '\r?\n')[0]
        Write-Log 'ERROR' "$($j.Title): $msg"
        Set-Status "$($j.Title) failed: $first" $ColorError
        if ($j.OnFail) { & $j.OnFail $msg }
        return
    }
    if (-not $j.Quiet) {
        $r = $res.Result
        $summary = if ($r -is [Collections.IDictionary]) {
            ($r.Keys | Where-Object { $r[$_] -is [int] -or $r[$_] -is [long] } | ForEach-Object { '{0}={1:N0}' -f $_, $r[$_] }) -join ', '
        }
        Set-Status "$($j.Title): done  $summary" $ColorWhite
    }
    if ($j.OnDone) { & $j.OnDone $res.Result }
}


# ------------------------------------------------------------------ actions
function Connect-Database {
    $port = 0
    if (-not [int]::TryParse($tbPort.Text.Trim(), [ref]$port)) { Show-Message 'Port must be a number.' 'Port' 'Error'; return }
    try { $role = Get-MhRoleName $tbRole.Text } catch { Show-Message $_.Exception.Message 'Role' 'Error'; return }
    $tbRole.Text = $role
    $values = [ordered]@{ Host = $tbHost.Text.Trim(); Port = $port; Database = $tbDatabase.Text.Trim()
                          User = $tbUser.Text.Trim(); Role = $role; Kerberos = $chkKrb.Checked }
    Read-AppConfig
    if (-not $script:AppConfig.PSObject.Properties['Connection']) {
        $script:AppConfig | Add-Member -NotePropertyName Connection -NotePropertyValue (New-Object psobject)
    }
    foreach ($k in $values.Keys) { $script:AppConfig.Connection | Add-Member -NotePropertyName $k -NotePropertyValue $values[$k] -Force }
    try { $db = New-MhConnection $script:AppConfig $tbPassword.Text }
    catch { Show-Message $_.Exception.Message 'Connect' 'Error'; return }
    $connStatus.Text = "$Dot connecting..."
    $connStatus.ForeColor = $ColorGray
    Start-MhJob 'Connect' -NoConnection -Quiet -Arg @{ Db = $db; Values = $values } -Body {
        param($Db, $Arg, $Log, $Status)
        @{ Info = (Test-MhConnection $Arg.Db); Db = $Arg.Db; Values = $Arg.Values }
    } -OnDone {
        param($r)
        $script:Db = $r.Db
        $connStatus.Text = "$Dot $($r.Db.Database)@$($r.Db.Host)"
        $connStatus.ForeColor = $ColorOrange
        Set-Status ''
        Write-Log 'Connect' "$($r.Info)  | psql $($r.Db.Psql)"
        # remember connection fields (never the password)
        try { Save-MhConnection $ConfigPath $r.Values }
        catch { Write-Log 'WARN' "Could not save connection to config.json: $($_.Exception.Message)" }
        Show-Table
    } -OnFail {
        param($msg)
        $connStatus.Text = "$Dot failed"
        $connStatus.ForeColor = $ColorError
    }
}

function Start-Sync {
    $s = Get-FreshSettings
    if (-not $s) { return }
    Start-MhJob 'Sync table' -Arg $s -Body {
        param($Db, $Arg, $Log, $Status)
        @{ added_columns = @(Initialize-MhTable $Db $Arg -Log $Log).Count }
    } -OnDone { Show-Table }
}

function Show-ImportExcelHelp {
    Show-Message ("Excel files need the ImportExcel PowerShell module.`n`nInstall it once (PowerShell):`n" +
                  "    Install-Module ImportExcel -Scope CurrentUser`n`n" +
                  "PC without internet: on another PC run`n    Save-Module ImportExcel -Path <folder>`n" +
                  "and copy <folder>\ImportExcel to`n    $PSScriptRoot\modules\ImportExcel`n`n" +
                  'Or use CSV UTF-8 instead.') 'ImportExcel needed' 'Warning'
}

function Start-Upload([string] $Path) {
    if (-not $script:Db) { Show-Message 'Connect to the database first.' 'Not connected' 'Warning'; return }
    if (-not $Path) {
        $dlg = New-Object Windows.Forms.OpenFileDialog
        $dlg.Title = 'Upload metadata (CSV or XLSX)'
        $dlg.Filter = 'Metadata files (*.csv;*.xlsx;*.xlsm)|*.csv;*.xlsx;*.xlsm|CSV files (*.csv)|*.csv|' +
                      'Excel files (*.xlsx;*.xlsm)|*.xlsx;*.xlsm|All files (*.*)|*.*'
        if ($dlg.ShowDialog($form) -ne 'OK') { return }
        $Path = $dlg.FileName
    }
    $path = $Path
    if ($path -match '\.xls[xm]$' -and -not (Get-Module -ListAvailable ImportExcel)) {
        Show-ImportExcelHelp
        return
    }
    $s = Get-FreshSettings
    if (-not $s) { return }
    $folder = [string](Get-ConfigValue 'UnmatchedReportFolder' 'logs')
    $reportDir = if ([IO.Path]::IsPathRooted($folder)) { $folder } else { Join-Path $PSScriptRoot $folder }
    Start-MhJob 'Upload' -Arg @{ S = $s; Path = $path; ReportDir = $reportDir } -Body {
        param($Db, $Arg, $Log, $Status)
        $progress = { param($n) & $Status ('Upload: {0:N0} rows read...' -f $n) }.GetNewClosure()
        Import-MhFile -Conn $Db -S $Arg.S -Path $Arg.Path -Log $Log -ReportDir $Arg.ReportDir -Progress $progress
    } -OnDone { Show-Table }
}

function Start-Convert {
    $remove = $chkRemove.Checked
    $text = ("Copy the configured keys from the vertical Metadata table into the horizontal table (one row per file)?`n`n" +
             $(if ($remove) { 'The moved keys will then be DELETED from Metadata.' } else { 'Metadata will be left unchanged.' }))
    if (-not (Confirm-Action $text 'Convert existing Metadata')) { return }
    $s = Get-FreshSettings
    if (-not $s) { return }
    Start-MhJob 'Convert' -Arg @{ S = $s; Remove = $remove } -Body {
        param($Db, $Arg, $Log, $Status)
        Convert-MhExisting $Db $Arg.S $Arg.Remove $Log
    } -OnDone { Show-Table }
}

function Start-Matrix {
    Read-AppConfig
    try { $table = Get-MhMatrixTable $script:AppConfig }
    catch { Show-Message $_.Exception.Message 'MetadataMatrix config' 'Error'; return }
    $text = ("Rebuild $table from Metadata?`n`nOne row per file, one column per distinct MetadataKey. " +
             "$table is dropped and created again; Metadata is not changed.")
    if (-not (Confirm-Action $text 'Build Metadata Matrix')) { return }
    Start-MhJob 'Matrix' -Arg @{ Config = $script:AppConfig; Table = $table } -Body {
        param($Db, $Arg, $Log, $Status)
        Update-MhMatrix $Db $Arg.Config $Log
    } -OnDone ([scriptblock]::Create("Show-Table '$table'"))
}

function Show-Table([string] $Table) {
    if (-not $script:Db) { return }
    if (-not $Table) {
        try { $Table = (Get-MhSettings $script:AppConfig).Table }
        catch { Set-Status ($_.Exception.Message -split '\r?\n')[0] $ColorError; return }
    }
    Start-MhJob 'View' -Quiet -Arg @{ Table = $Table; Limit = $ViewRows } -Body {
        param($Db, $Arg, $Log, $Status)
        @{ View = (Get-MhTableView $Db $Arg.Table $Arg.Limit); Table = $Arg.Table }
    } -OnDone {
        param($r)
        if (-not $r.View) {
            Set-Status "Table $($r.Table) does not exist yet - click Create / Sync Table, Build Matrix or upload a file"
        } else {
            Show-Grid $r.View $r.Table
        }
    }
}

function Show-MatrixTable {
    try { Show-Table (Get-MhMatrixTable $script:AppConfig) }
    catch { Set-Status ($_.Exception.Message -split '\r?\n')[0] $ColorError }
}

function Show-Grid($View, [string] $Table) {
    $grid.SuspendLayout()
    try {
        $grid.Rows.Clear()
        $grid.Columns.Clear()
        $cols = New-Object 'Collections.Generic.List[Windows.Forms.DataGridViewColumn]'
        $i = 0
        foreach ($name in $View.Columns) {
            # index names: matrix column names are arbitrary metadata keys
            $c = New-Object Windows.Forms.DataGridViewTextBoxColumn
            $c.Name = "c$i"
            $c.HeaderText = $name
            $c.FillWeight = 1                       # the sum may not exceed 65535 (matrix: up to 1593 columns)
            $c.SortMode = 'NotSortable'
            $c.Width = [Math]::Min([Math]::Max($name.Length * 8, 90), 300)
            $cols.Add($c)
            $i++
        }
        $grid.Columns.AddRange($cols.ToArray())
        foreach ($r in $View.Rows) { [void]$grid.Rows.Add([object[]]$r) }
    } finally {
        $grid.ResumeLayout()
    }
    Set-Status ('{0}: showing {1:N0} of {2:N0} row(s), {3} column(s)' -f $Table, $View.Rows.Count, $View.Total, $View.Columns.Count) $ColorWhite
}

function Show-Ddl {
    $s = Get-FreshSettings
    if (-not $s) { return }
    $win = New-Object Windows.Forms.Form
    $win.Text = "DDL - $($s.Table)"
    $win.Size = New-Object Drawing.Size(780, 560)
    $win.StartPosition = 'CenterParent'
    $box = New-Object Windows.Forms.TextBox
    $box.Multiline = $true; $box.ReadOnly = $true; $box.ScrollBars = 'Both'; $box.WordWrap = $false
    $box.Dock = 'Fill'; $box.Font = New-Object Drawing.Font('Consolas', 10)
    $box.BackColor = $ColorPanel; $box.ForeColor = $ColorWhite
    $box.Text = ((Get-MhDdl $s -WithSites) +
                 "`n`n-- `"SiteId`" (+ FK to `"Sites`" and trigger) is only added when the database`n" +
                 "-- has the Sites table from the consolidation (MtrMigration_Consolidated).`n") -replace '\r?\n', "`r`n"
    $win.Controls.Add($box)
    $win.Add_Shown({ $box.Select(0, 0) })
    [void]$win.ShowDialog($form)
}

function Invoke-ReloadConfig {
    Read-AppConfig
    Update-ConfigLabel -SetCheckbox
    Write-Log 'Config' "Reloaded $ConfigPath"
}

function Show-SharePointExport {
    <# Window: SharePoint 2013 site -> pick document libraries -> one Excel/CSV with every file's metadata. #>
    if ($script:SpForm) { $script:SpForm.Show(); $script:SpForm.Activate(); return }
    $f = New-Object Windows.Forms.Form
    $f.Text = 'Export from SharePoint 2013'
    $f.Size = New-Object Drawing.Size(900, 620)
    $f.MinimumSize = New-Object Drawing.Size(700, 420)
    $f.StartPosition = 'CenterParent'
    $f.BackColor = $ColorDark; $f.ForeColor = $ColorWhite; $f.Font = $form.Font
    $grid2 = New-Object Windows.Forms.TableLayoutPanel
    $grid2.Dock = 'Fill'; $grid2.ColumnCount = 1; $grid2.RowCount = 5
    $grid2.Padding = New-Object Windows.Forms.Padding(8)
    foreach ($st in @(@('AutoSize', 0), @('AutoSize', 0), @('AutoSize', 0), @('Percent', 100), @('AutoSize', 0))) {
        [void]$grid2.RowStyles.Add((New-Object Windows.Forms.RowStyle($st[0], $st[1])))
    }
    $f.Controls.Add($grid2)

    $row = New-Flow $ColorPanel
    $row.Controls.Add((New-Label 'Site URL'))
    $script:SpUrl = New-TextBox '' 640
    $row.Controls.Add($script:SpUrl)
    $grid2.Controls.Add($row, 0, 0)

    $row = New-Flow $ColorPanel
    $script:SpSubsites = New-Object Windows.Forms.CheckBox
    $script:SpSubsites.Text = 'Include subsites'; $script:SpSubsites.AutoSize = $true; $script:SpSubsites.Checked = $true
    $script:SpSubsites.Margin = New-Object Windows.Forms.Padding(10, 8, 16, 0)
    $row.Controls.Add($script:SpSubsites)
    $script:SpWindows = New-Object Windows.Forms.RadioButton
    $script:SpWindows.Text = 'Windows login (signed-in user)'; $script:SpWindows.AutoSize = $true; $script:SpWindows.Checked = $true
    $script:SpWindows.Margin = New-Object Windows.Forms.Padding(4, 8, 8, 0)
    $row.Controls.Add($script:SpWindows)
    $spOther = New-Object Windows.Forms.RadioButton
    $spOther.Text = 'User (DOMAIN\user)'; $spOther.AutoSize = $true
    $spOther.Margin = New-Object Windows.Forms.Padding(4, 8, 2, 0)
    $row.Controls.Add($spOther)
    $script:SpUser = New-TextBox '' 160
    $row.Controls.Add($script:SpUser)
    $row.Controls.Add((New-Label 'Password'))
    $script:SpPassword = New-TextBox '' 130
    $script:SpPassword.UseSystemPasswordChar = $true
    $row.Controls.Add($script:SpPassword)
    $toggle = { $script:SpUser.Enabled = -not $script:SpWindows.Checked; $script:SpPassword.Enabled = -not $script:SpWindows.Checked }
    $script:SpWindows.Add_CheckedChanged($toggle)
    & $toggle
    $grid2.Controls.Add($row, 0, 1)

    $row = New-Flow $ColorDark
    $row.Controls.Add((New-Button 'Load libraries' $ColorBlue { Start-SpLoad } -Bold))
    $row.Controls.Add((New-Button 'Select all' (Col '#444444') { Set-SpChecked $true }))
    $row.Controls.Add((New-Button 'Select none' (Col '#444444') { Set-SpChecked $false }))
    $script:SpCount = New-Label 'Enter the site URL, then Load libraries. System libraries (Style Library, Site Pages...) start unticked.'
    $row.Controls.Add($script:SpCount)
    $grid2.Controls.Add($row, 0, 2)

    $script:SpList = New-Object Windows.Forms.CheckedListBox
    $script:SpList.Dock = 'Fill'; $script:SpList.CheckOnClick = $true; $script:SpList.IntegralHeight = $false
    $script:SpList.BackColor = $ColorField; $script:SpList.ForeColor = $ColorWhite; $script:SpList.BorderStyle = 'None'
    $script:SpList.Add_ItemCheck({ $script:SpList.BeginInvoke([Action] { Update-SpCount }) | Out-Null })
    $grid2.Controls.Add($script:SpList, 0, 3)

    $row = New-Flow $ColorDark
    $row.Controls.Add((New-Button 'Export to Excel...' $ColorOrange { Start-SpExport } -Bold -Fore ([Drawing.Color]::Black)))
    $row.Controls.Add((New-Label 'One row per file, one column per field. Progress and results appear in the main window.'))
    $grid2.Controls.Add($row, 0, 4)

    # closing only hides the window, so the loaded library list is kept
    $f.Add_FormClosing({ param($sender, $e) if ($e.CloseReason -eq 'UserClosing') { $e.Cancel = $true; $sender.Hide() } })
    $script:SpForm = $f
    $f.Show($form)
}

function Update-SpCount {
    if (-not $script:SpLibs) { return }
    $n = 0; $items = 0
    for ($i = 0; $i -lt $script:SpList.Items.Count; $i++) {
        if ($script:SpList.GetItemChecked($i)) { $n++; $items += $script:SpLibs[$i].ItemCount }
    }
    $script:SpCount.Text = '{0} of {1} libraries ticked, about {2:N0} items' -f $n, $script:SpLibs.Count, $items
}

function Set-SpChecked([bool] $On) {
    for ($i = 0; $i -lt $script:SpList.Items.Count; $i++) { $script:SpList.SetItemChecked($i, $On) }
}

function Start-SpLoad {
    $url = $script:SpUrl.Text.Trim()
    if (-not $url) { Show-Message 'Enter the SharePoint site URL, e.g. https://sharepoint.corp/sites/abc' 'Site URL' 'Warning'; return }
    $user = if ($script:SpWindows.Checked) { '' } else { $script:SpUser.Text.Trim() }
    if (-not $script:SpWindows.Checked -and -not $user) { Show-Message 'Enter the user as DOMAIN\user.' 'User' 'Warning'; return }
    Start-MhJob 'SharePoint' -NoConnection -Arg @{ Url = $url; User = $user; Password = $script:SpPassword.Text
                                                 Recurse = $script:SpSubsites.Checked } -Body {
        param($Db, $Arg, $Log, $Status)
        $sp = New-SpSession $Arg.Url $Arg.User $Arg.Password
        & $Log "Connected to $($sp.Url) ($($sp.Title))"
        $libs = New-Object 'Collections.Generic.List[hashtable]'
        foreach ($w in (Get-SpWebs $sp -Recurse:$Arg.Recurse -Log $Log)) {
            & $Status "SharePoint: reading libraries of $($w.Url)..."
            try { $libs.AddRange((Get-SpLibraries $sp $w)) }
            catch { & $Log "Libraries of $($w.Url) skipped: $($_.Exception.Message.Split("`n")[0])" }
        }
        & $Log "$($libs.Count) document libraries found"
        @{ Session = $sp; Libs = $libs; libraries = $libs.Count }
    } -OnDone {
        param($r)
        $script:SpSession = $r.Session
        $script:SpLibs = @($r.Libs)
        $script:SpList.BeginUpdate()
        $script:SpList.Items.Clear()
        foreach ($l in $script:SpLibs) {
            [void]$script:SpList.Items.Add(('{0} / {1}   ({2:N0} items)   {3}' -f $l.WebTitle, $l.Title, $l.ItemCount, $l.Folder), (-not $l.System))
        }
        $script:SpList.EndUpdate()
        Update-SpCount
    }
}

function Start-SpExport {
    if (-not $script:SpLibs) { Show-Message 'Load the libraries first.' 'Export from SharePoint' 'Warning'; return }
    $sel = @(for ($i = 0; $i -lt $script:SpList.Items.Count; $i++) { if ($script:SpList.GetItemChecked($i)) { $script:SpLibs[$i] } })
    if (-not $sel.Count) { Show-Message 'Tick at least one library.' 'Export from SharePoint' 'Warning'; return }
    $dlg = New-Object Windows.Forms.SaveFileDialog
    $dlg.Title = 'Save SharePoint metadata'
    $dlg.Filter = 'Excel workbook (*.xlsx)|*.xlsx|CSV UTF-8 (*.csv)|*.csv'
    $dlg.FileName = "sharepoint_metadata_$(Get-Date -Format 'yyyyMMdd_HHmm').xlsx"
    if ($dlg.ShowDialog($script:SpForm) -ne 'OK') { return }
    if ($dlg.FileName -match '\.xlsx$' -and -not (Get-Module -ListAvailable ImportExcel)) { Show-ImportExcelHelp; return }
    Start-MhJob 'SP export' -NoConnection -Arg @{ Sp = $script:SpSession; Libs = $sel; Path = $dlg.FileName } -Body {
        param($Db, $Arg, $Log, $Status)
        $progress = { param($n, $lib) & $Status ('SP export: {0:N0} files read ({1})...' -f $n, $lib) }.GetNewClosure()
        Export-SpMetadata -Sp $Arg.Sp -Libraries $Arg.Libs -Path $Arg.Path -Log $Log -Progress $progress
    } -OnDone {
        param($r)
        $saved = "Saved {0:N0} files from {1} libraries to`n{2}" -f $r.files, $r.libraries, $r.path
        if ($script:Db) {
            if (Confirm-Action "$saved`n`nUpload it to $($script:Db.Database) now?" 'Export from SharePoint') { Start-Upload $r.path }
        } else {
            Show-Message "$saved`n`nConnect to the database and click Upload CSV/XLSX to load it." 'Export from SharePoint'
        }
    }
}

function Open-ConfigFile {
    try { Start-Process -FilePath $ConfigPath }                          # default editor for .json
    catch { Start-Process -FilePath notepad.exe -ArgumentList "`"$ConfigPath`"" }
}


# ------------------------------------------------------------------ layout
function New-Label([string] $Text) {
    $l = New-Object Windows.Forms.Label
    $l.Text = $Text; $l.AutoSize = $true; $l.ForeColor = $ColorGray
    $l.Margin = New-Object Windows.Forms.Padding(10, 9, 2, 0)
    $l
}

function New-TextBox([string] $Text, [int] $Width) {
    $t = New-Object Windows.Forms.TextBox
    $t.Text = $Text; $t.Width = $Width
    $t.BackColor = $ColorField; $t.ForeColor = $ColorWhite; $t.BorderStyle = 'FixedSingle'
    $t.Margin = New-Object Windows.Forms.Padding(0, 6, 4, 4)
    $t
}

function New-Button([string] $Text, $Back, [scriptblock] $OnClick, [switch] $Bold, $Fore = $ColorWhite) {
    $b = New-Object Windows.Forms.Button
    $b.Text = $Text; $b.AutoSize = $true; $b.FlatStyle = 'Flat'; $b.FlatAppearance.BorderSize = 0
    $b.BackColor = $Back; $b.ForeColor = $Fore
    $b.Padding = New-Object Windows.Forms.Padding(8, 3, 8, 3)
    $b.Margin = New-Object Windows.Forms.Padding(4, 4, 4, 4)
    if ($Bold) { $b.Font = New-Object Drawing.Font($b.Font, [Drawing.FontStyle]::Bold) }
    $b.Add_Click($OnClick)
    $b
}

function New-Flow($Back) {
    $f = New-Object Windows.Forms.FlowLayoutPanel
    $f.Dock = 'Fill'; $f.AutoSize = $true; $f.AutoSizeMode = 'GrowAndShrink'; $f.WrapContents = $true
    $f.BackColor = $Back
    $f.Margin = New-Object Windows.Forms.Padding(0, 0, 0, 4)
    $f
}

[Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object Windows.Forms.Form
$form.Text = 'Metadata Horizontal'
$form.Size = New-Object Drawing.Size(1300, 800)
$form.MinimumSize = New-Object Drawing.Size(900, 560)
$form.StartPosition = 'CenterScreen'
$form.BackColor = $ColorDark
$form.ForeColor = $ColorWhite
$form.Font = New-Object Drawing.Font('Segoe UI', 9)

$layout = New-Object Windows.Forms.TableLayoutPanel
$layout.Dock = 'Fill'; $layout.ColumnCount = 1; $layout.RowCount = 6
$layout.Padding = New-Object Windows.Forms.Padding(8)
[void]$layout.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent', 100)))
foreach ($st in @(@('AutoSize', 0), @('AutoSize', 0), @('AutoSize', 0), @('Percent', 75), @('AutoSize', 0), @('Percent', 25))) {
    [void]$layout.RowStyles.Add((New-Object Windows.Forms.RowStyle($st[0], $st[1])))
}
$form.Controls.Add($layout)

Read-AppConfig
$connCfg = $script:AppConfig.PSObject.Properties['Connection']
$connCfg = if ($connCfg) { $connCfg.Value } else { New-Object psobject }
function Get-ConnValue([string] $Name, $Default) {
    $p = $connCfg.PSObject.Properties[$Name]
    if ($p -and $null -ne $p.Value) { return [string]$p.Value }
    return [string]$Default
}

# Row 0: connection
$connBar = New-Flow $ColorPanel
$connBar.Controls.Add((New-Label 'Host'));     $tbHost = New-TextBox (Get-ConnValue 'Host' 'localhost') 160;  $connBar.Controls.Add($tbHost)
$connBar.Controls.Add((New-Label 'Port'));     $tbPort = New-TextBox (Get-ConnValue 'Port' 5432) 60;          $connBar.Controls.Add($tbPort)
$connBar.Controls.Add((New-Label 'Database')); $tbDatabase = New-TextBox (Get-ConnValue 'Database' '') 220;   $connBar.Controls.Add($tbDatabase)
$connBar.Controls.Add((New-Label 'User'));     $tbUser = New-TextBox (Get-ConnValue 'User' 'postgres') 120;   $connBar.Controls.Add($tbUser)
$connBar.Controls.Add((New-Label 'Role'));     $tbRole = New-TextBox (Get-ConnValue 'Role' '') 110;           $connBar.Controls.Add($tbRole)
$connBar.Controls.Add((New-Label 'Password')); $tbPassword = New-TextBox $env:PGPASSWORD 120;               $connBar.Controls.Add($tbPassword)
$tbPassword.UseSystemPasswordChar = $true
$chkKrb = New-Object Windows.Forms.CheckBox
$chkKrb.Text = 'Kerberos'; $chkKrb.AutoSize = $true; $chkKrb.ForeColor = $ColorWhite
$chkKrb.Margin = New-Object Windows.Forms.Padding(6, 8, 8, 0)
$chkKrb.Checked = (Get-ConnValue 'Kerberos' 'False') -eq 'True'
$chkKrb.Add_CheckedChanged({ $tbPassword.Enabled = -not $chkKrb.Checked })
$tbPassword.Enabled = -not $chkKrb.Checked
$connBar.Controls.Add($chkKrb)
$connBar.Controls.Add((New-Button 'Connect' $ColorOrange { Connect-Database } -Bold -Fore ([Drawing.Color]::Black)))
$connStatus = New-Label "$Dot not connected"
$connBar.Controls.Add($connStatus)
$layout.Controls.Add($connBar, 0, 0)

# Row 1: actions
$toolbar = New-Flow $ColorDark
$toolbar.Controls.Add((New-Button 'Export from SharePoint' (Col '#B35C00') { Show-SharePointExport }))
$toolbar.Controls.Add((New-Button 'Upload CSV/XLSX' $ColorOrange { Start-Upload } -Bold -Fore ([Drawing.Color]::Black)))
$toolbar.Controls.Add((New-Button 'Create / Sync Table' $ColorBlue { Start-Sync }))
$toolbar.Controls.Add((New-Button 'Convert existing Metadata' (Col '#1A6B5A') { Start-Convert }))
$toolbar.Controls.Add((New-Button 'Build Matrix' (Col '#6A3D8A') { Start-Matrix }))
$toolbar.Controls.Add((New-Button 'View Table' (Col '#2A4A7A') { Show-Table }))
$toolbar.Controls.Add((New-Button 'View Matrix' (Col '#2A4A7A') { Show-MatrixTable }))
$toolbar.Controls.Add((New-Button 'Show DDL' (Col '#444444') { Show-Ddl }))
$toolbar.Controls.Add((New-Button 'Reload config' (Col '#444444') { Invoke-ReloadConfig }))
$toolbar.Controls.Add((New-Button 'Open config.json' (Col '#444444') { Open-ConfigFile }))
$layout.Controls.Add($toolbar, 0, 1)

# Row 2: config summary
$cfgBar = New-Object Windows.Forms.TableLayoutPanel
$cfgBar.Dock = 'Fill'; $cfgBar.AutoSize = $true; $cfgBar.ColumnCount = 2; $cfgBar.RowCount = 1; $cfgBar.BackColor = $ColorPanel
$cfgBar.Margin = New-Object Windows.Forms.Padding(0, 0, 0, 4)
[void]$cfgBar.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent', 100)))
[void]$cfgBar.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('AutoSize')))
$cfgLabel = New-Label ''
$cfgLabel.Margin = New-Object Windows.Forms.Padding(10, 5, 10, 5)
$chkRemove = New-Object Windows.Forms.CheckBox
$chkRemove.Text = 'Convert: remove moved keys from Metadata'; $chkRemove.AutoSize = $true; $chkRemove.ForeColor = $ColorGray
$chkRemove.Checked = $true
$chkRemove.Margin = New-Object Windows.Forms.Padding(8, 5, 8, 5)
$cfgBar.Controls.Add($cfgLabel, 0, 0)
$cfgBar.Controls.Add($chkRemove, 1, 0)
$layout.Controls.Add($cfgBar, 0, 2)

# Row 3: grid
$grid = New-Object Windows.Forms.DataGridView
$grid.Dock = 'Fill'
$grid.ReadOnly = $true; $grid.AllowUserToAddRows = $false; $grid.AllowUserToDeleteRows = $false
$grid.AllowUserToResizeRows = $false; $grid.RowHeadersVisible = $false
$grid.AutoSizeColumnsMode = 'None'; $grid.ColumnHeadersHeightSizeMode = 'AutoSize'
$grid.ClipboardCopyMode = 'EnableWithAutoHeaderText'
$grid.BackgroundColor = $ColorField; $grid.BorderStyle = 'None'; $grid.GridColor = $ColorPanel
$grid.EnableHeadersVisualStyles = $false
$grid.DefaultCellStyle.BackColor = $ColorField
$grid.DefaultCellStyle.ForeColor = $ColorWhite
$grid.DefaultCellStyle.SelectionBackColor = $ColorBlue
$grid.ColumnHeadersDefaultCellStyle.BackColor = Col '#2E4A6A'
$grid.ColumnHeadersDefaultCellStyle.ForeColor = $ColorWhite
$grid.ColumnHeadersDefaultCellStyle.Font = New-Object Drawing.Font('Segoe UI', 9, [Drawing.FontStyle]::Bold)
$grid.RowTemplate.Height = 22
$layout.Controls.Add($grid, 0, 3)

# Row 4: status
$status = New-Label ''
$status.Margin = New-Object Windows.Forms.Padding(4, 4, 4, 4)
$layout.Controls.Add($status, 0, 4)

# Row 5: log
$logBox = New-Object Windows.Forms.TextBox
$logBox.Multiline = $true; $logBox.ReadOnly = $true; $logBox.ScrollBars = 'Vertical'; $logBox.Dock = 'Fill'
$logBox.BackColor = $ColorPanel; $logBox.ForeColor = $ColorWhite; $logBox.BorderStyle = 'None'
$logBox.Font = New-Object Drawing.Font('Consolas', 9)
$layout.Controls.Add($logBox, 0, 5)

$form.Add_Resize({ $cfgLabel.MaximumSize = New-Object Drawing.Size([Math]::Max(200, $form.ClientSize.Width - 360), 0) })
$cfgLabel.MaximumSize = New-Object Drawing.Size(940, 0)

# worker -> UI: log lines, status text, job completion
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 100
$timer.Add_Tick({
    $done = $script:Job -and $script:Job.Handle.IsCompleted
    $item = $null
    while ($Sync.Queue.TryDequeue([ref]$item)) {
        if ($item[0] -eq 'log') { Write-Log $item[1] $item[2] } else { Set-Status $item[1] }
    }
    if ($done) { Complete-MhJob }
})

$form.Add_FormClosing({
    param($sender, $e)
    if ($script:Job -and -not (Confirm-Action ("A job is still running ($($script:Job.Title)). If you close now it either " +
            "finishes in the background or is rolled back by the server as a whole.`n`nClose anyway?") 'Metadata Horizontal')) {
        $e.Cancel = $true
    }
})

$form.Add_Shown({
    Update-ConfigLabel -SetCheckbox
    Write-Log 'Ready' "Config: $ConfigPath"
    try { Write-Log 'Ready' "psql: $(Find-MhPsql $script:AppConfig)" } catch { Write-Log 'WARN' $_.Exception.Message }
    if (-not (Get-Module -ListAvailable ImportExcel)) {
        Write-Log 'WARN' 'ImportExcel module not found: CSV upload works, XLSX upload needs it (Install-Module ImportExcel -Scope CurrentUser).'
    }
    $timer.Start()
})

[Windows.Forms.Application]::Run($form)
$timer.Stop()
