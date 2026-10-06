# Side-effect-free PowerShell 5.1 setup diagnostics. Never read .env here.
# Callers register secrets before recording any configuration-related failure.
function Get-DriveboundDiagnosticState {
    $variable = Get-Variable -Name DriveboundDiagnostics -Scope Script -ErrorAction SilentlyContinue
    if ($variable) { return $variable.Value }
    return $null
}

function Register-DriveboundDiagnosticSecrets {
    param([hashtable]$Values = @{})
    $state = Get-DriveboundDiagnosticState
    if (-not $state) { return }
    foreach ($name in $Values.Keys) {
        if ([string]$name -notmatch '(?i)password|passwd|secret|token|key|credential|authorization|email|database_url|redis_url|smtp_user') { continue }
        foreach ($raw in @($Values[$name])) {
            if ($null -eq $raw) { continue }
            $value = [string]$raw
            if ([string]::IsNullOrWhiteSpace($value)) { continue }
            $variants = @($value, $value.Trim('"', "'"))
            foreach ($item in @($variants)) {
                try { $variants += [Uri]::EscapeDataString($item) } catch { }
                $variants += $item.Replace('$', '$$')
                $variants += $item.Replace('\', '\\').Replace('"', '\"')
            }
            foreach ($item in $variants) {
                if ($item -and -not $state.Secrets.Contains($item)) { [void]$state.Secrets.Add($item) }
            }
        }
    }
}

function Protect-DriveboundDiagnosticText {
    param([AllowNull()][AllowEmptyString()][string]$Text)
    if ($null -eq $Text) { return '' }
    $safe = $Text
    $state = Get-DriveboundDiagnosticState
    if ($state) {
        # Longest first prevents a short value from hiding part of a longer one.
        foreach ($secret in @($state.Secrets | Sort-Object Length -Descending)) {
            $safe = $safe.Replace($secret, '[REDACTED]')
        }
    }
    $safe = [regex]::Replace($safe, '(?is)-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\z)', '[REDACTED PEM]')
    # Bound regex work as well as retained output. Never clip a long secret
    # into a potentially identifiable prefix; omit oversized lines entirely.
    $lines = @($safe -split '\r?\n' | Select-Object -Last 40 | ForEach-Object {
        if ($_.Length -gt 1024) { '[Oversized diagnostic line omitted]' } else { $_ }
    })
    $safe = $lines -join [Environment]::NewLine
    $safe = [regex]::Replace($safe, '(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@', '$1[REDACTED]@')
    $safe = [regex]::Replace($safe, '(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+', '[REDACTED AUTH]')
    $safe = [regex]::Replace($safe, '(?im)(?<![\w.-])(["'']?[\w.-]{0,128}(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|encryption[_-]?key|credential|authorization|email)[\w.-]{0,128}["'']?\s*[:=]\s*)(?:"[^"\r\n]*"|''[^''\r\n]*''|[^\r\n]+)', '$1[REDACTED]')
    $safe = [regex]::Replace($safe, '(?i)([?&](?:code|key|sig|signature|credential|password|token|access_token|refresh_token|api_key)=)[^&#\s]+', '$1[REDACTED]')
    $safe = [regex]::Replace($safe, '\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED JWT]')
    $safe = [regex]::Replace($safe, '(?i)\b[A-Z0-9.!#$%&''*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[REDACTED EMAIL]')
    # Remove terminal escapes/control characters, retaining readable line breaks.
    $safe = [regex]::Replace($safe, '\x1B\[[0-?]*[ -/]*[@-~]', '')
    $safe = [regex]::Replace($safe, '[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '')
    if ($safe.Length -gt 4096) { $safe = $safe.Substring(0, 4059) + "`n[diagnostic text truncated]" }
    return $safe
}

function New-DriveboundDiagnosticFile {
    param([Parameter(Mandatory = $true)][string]$Directory, [Parameter(Mandatory = $true)][string]$Name)
    # Refuse links in the destination ancestry; diagnostics must not overwrite or
    # relax access to another installation through a junction or symlink.
    $absolute = [IO.Path]::GetFullPath($Directory)
    $cursor = $absolute
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Diagnostic destination is a linked folder.' }
        }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
    [void][IO.Directory]::CreateDirectory($absolute)
    $aclHelper = Get-Command Set-DriveboundPrivateAcl -ErrorAction SilentlyContinue
    if (-not $aclHelper) { throw 'Private file protection is unavailable.' }
    Set-DriveboundPrivateAcl -Path $absolute
    $path = Join-Path $absolute $Name
    $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { Set-DriveboundPrivateAcl -Path $path } finally { $stream.Dispose() }
    return $path
}

function Initialize-DriveboundDiagnostics {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot, [string]$Action = 'Setup')
    $Script:DriveboundDiagnosticSessionId = [Guid]::NewGuid().ToString('N')
    $Script:DriveboundDiagnosticStage = 'Starting setup'
    $Script:DriveboundDiagnosticLogPath = $null
    $Script:DriveboundDiagnostics = @{
        Secrets = New-Object 'System.Collections.Generic.List[string]'
        LogPath = $null
        Stage = $Script:DriveboundDiagnosticStage
        SessionId = $Script:DriveboundDiagnosticSessionId
        StorageNotice = ''
        Capped = $false
    }
    $state = $Script:DriveboundDiagnostics
    $fileName = 'setup-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + $state.SessionId + '.log'
    try {
        $state.LogPath = New-DriveboundDiagnosticFile -Directory (Join-Path $ProjectRoot '.drivebound/logs') -Name $fileName
    } catch {
        try {
            $temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ('drivebound-diagnostics-' + $state.SessionId)
            $state.LogPath = New-DriveboundDiagnosticFile -Directory $temporaryDirectory -Name $fileName
            $state.StorageNotice = 'The project log folder was unavailable; using a private temporary log.'
        } catch {
            $state.StorageNotice = 'A private diagnostic log could not be created. Copy the error details displayed here.'
        }
    }
    $Script:DriveboundDiagnosticLogPath = $state.LogPath
    Add-DriveboundDiagnostic -Message ('Drivebound setup diagnostics. Session: ' + $state.SessionId + '. Action: ' + $Action + '. PowerShell: ' + $PSVersionTable.PSVersion.ToString())
}

function Add-DriveboundDiagnostic {
    param([AllowNull()][AllowEmptyString()][string]$Message)
    $state = Get-DriveboundDiagnosticState
    if (-not $state -or -not $state.LogPath -or $state.Capped) { return }
    try {
        $safe = Protect-DriveboundDiagnosticText -Text $Message
        $record = '[' + [DateTime]::UtcNow.ToString('o') + '] ' + $safe + [Environment]::NewLine
        $encoding = New-Object Text.UTF8Encoding($false)
        $bytes = $encoding.GetBytes($record)
        # Bound each session to 256 KiB. Do not rotate, delete or truncate logs.
        if ((Get-Item -LiteralPath $state.LogPath).Length + $bytes.Length -gt 262144) {
            $state.Capped = $true
            $state.StorageNotice = 'The diagnostic log reached its size limit; subsequent details are shown on screen only.'
            return
        }
        [IO.File]::AppendAllText($state.LogPath, $record, $encoding)
    } catch {
        $state.StorageNotice = 'Writing the diagnostic log failed; copy the error details displayed here.'
    }
}

function Set-DriveboundStage {
    param([Parameter(Mandatory = $true)][string]$Stage)
    $safe = Protect-DriveboundDiagnosticText -Text $Stage
    $Script:DriveboundDiagnosticStage = $safe
    $state = Get-DriveboundDiagnosticState
    if ($state) { $state.Stage = $safe }
    Write-Host ('[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $safe)
    Add-DriveboundDiagnostic -Message ('Stage: ' + $safe)
}

function Write-DriveboundFailure {
    param([Parameter(Mandatory = $true)][Management.Automation.ErrorRecord]$ErrorRecord)
    $state = Get-DriveboundDiagnosticState
    $stage = if ($state) { $state.Stage } else { 'Setup initialization' }
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $lines.Add('Drivebound could not complete this step.')
    $lines.Add('Stage: ' + $stage)
    $lines.Add('Error type: ' + $ErrorRecord.Exception.GetType().FullName)
    $lines.Add('Reason: ' + (Protect-DriveboundDiagnosticText -Text $ErrorRecord.Exception.Message))
    $invocation = $ErrorRecord.InvocationInfo
    if ($invocation -and $invocation.ScriptName) {
        $lines.Add('Location: ' + $invocation.ScriptName + ':' + $invocation.ScriptLineNumber)
    }
    # ErrorRecord.ToString(), PositionMessage and InvocationInfo.Line include the
    # offending source/arguments. Only extract PowerShell's structured stack form.
    $count = 0
    foreach ($frame in @($ErrorRecord.ScriptStackTrace -split '\r?\n')) {
        if ($frame -match '^at (?<function>[^,\r\n]+), (?<file>[^\r\n]+): line (?<line>\d+)$') {
            $lines.Add('Call: ' + $Matches['function'] + ' (' + $Matches['file'] + ':' + $Matches['line'] + ')')
            $count++
            if ($count -ge 8) { break }
        }
    }
    $details = Protect-DriveboundDiagnosticText -Text ($lines -join [Environment]::NewLine)
    Add-DriveboundDiagnostic -Message $details
    # Keep the support reference outside the bounded error body so even an
    # unusually long native error cannot hide where the report was saved.
    if ($state) {
        $details += [Environment]::NewLine + 'Diagnostic session: ' + $state.SessionId
        if ($state.LogPath) { $details += [Environment]::NewLine + 'Diagnostic log: ' + (Protect-DriveboundDiagnosticText -Text $state.LogPath) }
        if ($state.StorageNotice) { $details += [Environment]::NewLine + $state.StorageNotice }
    } else {
        $details += [Environment]::NewLine + 'Diagnostic logging did not initialize. Copy these details before closing.'
    }
    return $details
}
