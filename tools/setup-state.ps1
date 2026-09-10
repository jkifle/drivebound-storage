# Shared, side-effect-free definitions for guided setup and remote configuration.
# Call Enter-InstallationLock before recovery, reading mutable state, or committing.
function ConvertTo-WindowsNativeArgument {
    param([AllowEmptyString()][string]$Value)
    # ProcessStartInfo.ArgumentList is unavailable in Windows PowerShell 5.1.
    # Quote every argument using the Windows CRT backslash/quote rules.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-DriveboundNative {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [int]$TimeoutSeconds = 0
    )
    # Native stderr is not a PowerShell error. Windows PowerShell 5.1 turns
    # redirected stderr into NativeCommandError under ErrorActionPreference=Stop,
    # even for a harmless warning. Separate streams and inspect the exit code.
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = (@($Arguments | ForEach-Object { ConvertTo-WindowsNativeArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        # Read both concurrently; a full stderr pipe must not deadlock stdout.
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $finished = if ($TimeoutSeconds -gt 0) { $process.WaitForExit($TimeoutSeconds * 1000) } else { $process.WaitForExit(); $true }
        if (-not $finished) {
            try { $process.Kill(); $process.WaitForExit() } catch { }
            return [pscustomobject]@{ExitCode = -1; StdOut = ''; StdErr = ''; TimedOut = $true}
        }
        return [pscustomobject]@{ExitCode = $process.ExitCode; StdOut = $stdout.GetAwaiter().GetResult(); StdErr = $stderr.GetAwaiter().GetResult(); TimedOut = $false}
    } finally { $process.Dispose() }
}

function Get-ProtectionIndependence {
    param($Volumes)
    $protection = $Volumes.protection.physical_disk_id
    $imports = $Volumes.imports.physical_disk_id
    $managed = $Volumes.managed.physical_disk_id
    # An imported photo and its copy must also survive one source-disk failure.
    if ($protection -and (($imports -and $imports -eq $protection) -or ($managed -and $managed -eq $protection))) { return 'same_disk' }
    if (-not $protection -or -not $imports -or -not $managed) { return 'unverified' }
    return 'independent_disk'
}

function Set-DriveboundPrivateAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    # Build a DACL-only descriptor. Reapplying a descriptor read from a replaced
    # file can also request SACL/audit privileges that a normal Windows user does
    # not have. Secret protection must not require administrator privileges.
    $isDirectory = (Get-Item -LiteralPath $Path).PSIsContainer
    $acl = if ($isDirectory) { New-Object Security.AccessControl.DirectorySecurity } else { New-Object Security.AccessControl.FileSecurity }
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = if ($isDirectory) { 'ContainerInherit,ObjectInherit' } else { 'None' }
    foreach ($sid in @($identity, (New-Object Security.Principal.SecurityIdentifier('S-1-5-18')))) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', $inherit, 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    # FileSystemProvider.SetSecurityDescriptor (Set-Acl) persists all sections,
    # including auditing. The .NET setter persists only the modified DACL.
    $item = Get-Item -LiteralPath $Path
    try {
        if ($PSVersionTable.PSEdition -eq 'Core') {
            [IO.FileSystemAclExtensions]::SetAccessControl($item, $acl)
        } else { $item.SetAccessControl($acl) }
    } catch { throw 'Windows could not protect a setup or disk-identity file. Check that your Windows account may change permissions on the selected folders. This pilot needs drives supporting Windows file permissions, such as NTFS. Do not reformat a drive containing photos.' }
}

function Write-DurableText {
    param([string]$Path, [AllowEmptyString()][string]$Content)
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Content)
    $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        Set-DriveboundPrivateAcl -Path $Path
        $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true)
    } finally { $stream.Dispose() }
}

function Enter-InstallationLock {
    $directory = Join-Path $Script:ProjectRoot '.drivebound'
    [void][IO.Directory]::CreateDirectory($directory)
    Set-DriveboundPrivateAcl -Path $directory
    try { return [IO.File]::Open((Join-Path $directory 'setup.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw 'Another Drivebound setup or startup is already running. Wait for it to finish and retry.' }
}

function Get-InstallationState {
    $path = Join-Path $Script:ProjectRoot '.drivebound/installation.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { $state = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json } catch { throw 'Installation state is unreadable. Restore the configuration recovery set; setup will not replace it.' }
    if ($state.schema_version -ne 1 -or -not $state.installation_id -or -not $state.compose_project_name) {
        throw 'Installation state is unsupported or incomplete. Restore the configuration recovery set.'
    }
    return $state
}

function Get-ConfigurationTarget {
    param([string]$RelativePath)
    $allowed = @('.env', 'docker-compose.user.yml', 'docker-compose.remote.yml', '.drivebound/installation.json', '.drivebound/storage.json')
    if ($RelativePath -notin $allowed) { throw 'The configuration transaction contains an unsupported target.' }
    return Join-Path $Script:ProjectRoot $RelativePath
}

function Set-AtomicConfigurationFile {
    param([string]$Source, [string]$Target)
    $pending = "$Target.pending"
    Write-DurableText -Path $pending -Content ([IO.File]::ReadAllText($Source))
    if ([IO.File]::Exists($Target)) {
        Set-DriveboundPrivateAcl -Path $Target
        [IO.File]::Replace($pending, $Target, [Management.Automation.Language.NullString]::Value)
    }
    else { [IO.File]::Move($pending, $Target) }
    Set-DriveboundPrivateAcl -Path $Target
}

function Remove-ConfigurationTransaction {
    $directory = [IO.Path]::GetFullPath((Join-Path $Script:ProjectRoot '.drivebound/config-transaction'))
    $expected = [IO.Path]::GetFullPath((Join-Path $Script:ProjectRoot '.drivebound')) + [IO.Path]::DirectorySeparatorChar
    if (-not $directory.StartsWith($expected, [StringComparison]::OrdinalIgnoreCase) -or (Split-Path -Leaf $directory) -ne 'config-transaction') { throw 'Invalid configuration recovery location.' }
    if (Test-Path -LiteralPath $directory) { Remove-Item -LiteralPath $directory -Recurse -Force }
}

function Recover-ConfigurationTransaction {
    $directory = Join-Path $Script:ProjectRoot '.drivebound/config-transaction'
    if (-not (Test-Path -LiteralPath $directory)) { return }
    $journalPath = Join-Path $directory 'journal.json'
    if (-not (Test-Path -LiteralPath $journalPath)) { Remove-ConfigurationTransaction; return }
    try { $journal = Get-Content -Raw -LiteralPath $journalPath | ConvertFrom-Json } catch { throw 'Setup recovery journal is damaged. Keep the .drivebound folder and restore the configuration recovery set.' }
    if ($journal.version -ne 1) { throw 'Unsupported setup recovery journal.' }
    if (-not $journal.committed) {
        foreach ($entry in $journal.entries) {
            $target = Get-ConfigurationTarget $entry.target
            if ([string]$entry.index -notmatch '^\d+$') { throw 'Invalid setup recovery journal entry.' }
            if ($entry.had_original) {
                $backup = Join-Path $directory "$($entry.index).before"
                if (-not (Test-Path -LiteralPath $backup)) { throw 'A setup recovery copy is missing. Keep this installation unchanged and restore the configuration recovery set.' }
                Set-AtomicConfigurationFile -Source $backup -Target $target
            } elseif (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
        }
    }
    Remove-ConfigurationTransaction
}

function Invoke-ConfigurationTransaction {
    param([Parameter(Mandatory = $true)][hashtable]$Contents)
    Recover-ConfigurationTransaction
    $directory = Join-Path $Script:ProjectRoot '.drivebound/config-transaction'
    [void][IO.Directory]::CreateDirectory($directory)
    Set-DriveboundPrivateAcl $directory
    $entries = @()
    $index = 0
    foreach ($relative in @($Contents.Keys | Sort-Object)) {
        $target = Get-ConfigurationTarget $relative
        $hadOriginal = Test-Path -LiteralPath $target
        if ($hadOriginal) { Write-DurableText -Path (Join-Path $directory "$index.before") -Content ([IO.File]::ReadAllText($target)) }
        Write-DurableText -Path (Join-Path $directory "$index.after") -Content $Contents[$relative]
        $entries += @{index = $index; target = $relative; had_original = $hadOriginal}
        $index++
    }
    $journal = @{version = 1; committed = $false; entries = $entries}
    $journalPath = Join-Path $directory 'journal.json'
    Write-DurableText $journalPath ($journal | ConvertTo-Json -Depth 8)
    try {
        foreach ($entry in $entries) {
            Set-AtomicConfigurationFile -Source (Join-Path $directory "$($entry.index).after") -Target (Get-ConfigurationTarget $entry.target)
        }
        $journal.committed = $true
        $committedPath = Join-Path $directory 'committed.json'
        Write-DurableText $committedPath ($journal | ConvertTo-Json -Depth 8)
        [IO.File]::Replace($committedPath, $journalPath, [Management.Automation.Language.NullString]::Value)
        Remove-ConfigurationTransaction
    } catch { Recover-ConfigurationTransaction; throw }
}

function Assert-PlainLocalPath {
    param([string]$Path)
    $full = Resolve-DriveboundPath $Path
    if ($full.StartsWith('\\')) { throw 'The Windows pilot needs a locally attached disk, not a network share.' }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -Force -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Storage paths cannot contain a symbolic link, junction, or mounted-folder redirect. Select the physical drive path.'
            }
        }
        $parent = Split-Path -Parent $cursor
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
    if (-not (Test-Path -LiteralPath ([IO.Path]::GetPathRoot($full)))) { throw 'A selected drive is disconnected. Reconnect the original drive and retry.' }
}

function Get-HostVolumeIdentity {
    param([string]$Path)
    Assert-PlainLocalPath $Path
    if (-not ('DriveboundVolumeNative' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class DriveboundVolumeNative {
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 public static extern bool GetVolumeNameForVolumeMountPoint(string path, StringBuilder name, int size);
}
'@
    }
    $root = [IO.Path]::GetPathRoot((Resolve-DriveboundPath $Path))
    $buffer = New-Object Text.StringBuilder(1024)
    if (-not [DriveboundVolumeNative]::GetVolumeNameForVolumeMountPoint($root, $buffer, $buffer.Capacity)) {
        throw 'Windows could not verify the selected volume identity. Reconnect the drive and retry; setup will not create replacement storage.'
    }
    $diskId = $null
    try {
        $partition = Get-Partition -DriveLetter $root.Substring(0, 1) -ErrorAction Stop
        $disk = Get-Disk -Number $partition.DiskNumber -ErrorAction Stop
        if ($disk.UniqueId) { $diskId = $disk.UniqueId.Trim() }
    } catch { $diskId = $null }
    return @{volume_id = $buffer.ToString(); physical_disk_id = $diskId; root = $root}
}

function Assert-InstallationStorage {
    param([Parameter(Mandatory = $true)]$State)
    foreach ($group in @('imports', 'managed', 'protection')) {
        $path = $State.paths.$group
        $saved = $State.volumes.$group
        if (-not $path -or -not $saved.volume_id -or -not $saved.marker_directory -or -not $saved.marker) { throw 'Saved storage identity is incomplete. Restore the configuration recovery set.' }
        $actual = Get-HostVolumeIdentity $path
        if ($actual.volume_id -ne $saved.volume_id) { throw "The $group drive is not the approved disk. Reconnect the original disk; Drivebound will not write to this replacement." }
        if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw "The $group folder is unavailable. Reconnect its original drive and retry." }
        Assert-PlainLocalPath $saved.marker_directory
        $markerPath = Join-Path $saved.marker_directory '.drivebound-volume'
        if (-not (Test-Path -LiteralPath $markerPath) -or ([IO.File]::ReadAllText($markerPath)).Trim() -cne $saved.marker) { throw "The $group drive identity marker is missing or changed. Restore its identity marker; setup will not adopt this disk automatically." }
    }
    foreach ($path in @((Join-Path $State.paths.managed 'originals'), (Join-Path $State.paths.managed 'derivatives'), (Join-Path $State.paths.managed 'staging'), (Join-Path $State.paths.protection 'replicas'), (Join-Path $State.paths.protection 'backups'))) {
        Assert-PlainLocalPath $path
        if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw 'A managed storage folder is missing. Restore the folder from the recovery set before starting Drivebound.' }
    }
}

function Complete-InitialConfiguration {
    # A first-run draft contains the exact approved keys, folders and markers.
    # Resume only that draft; never generate a second identity after a crash.
    $draftPath = Join-Path $Script:ProjectRoot '.drivebound/initialization-started.json'
    try { $draft = [IO.File]::ReadAllText($draftPath) | ConvertFrom-Json } catch { throw 'The first-setup recovery file is damaged. Keep .drivebound and ask the host operator to recover it.' }
    if ($draft.version -ne 1 -or -not $draft.contents) { throw 'This first-setup recovery file needs operator review. No keys or storage markers were changed.' }
    $contents = @{}
    foreach ($property in $draft.contents.PSObject.Properties) { $contents[$property.Name] = [string]$property.Value }
    $state = $contents['.drivebound/installation.json'] | ConvertFrom-Json
    $currentEnv = if (Test-Path -LiteralPath $Script:EnvironmentPath) { [IO.File]::ReadAllText($Script:EnvironmentPath) } else { $null }
    if ($currentEnv -cne $draft.previous_environment) { throw 'The configuration changed after first setup was interrupted. Keep both versions and ask the host operator to review them.' }
    foreach ($group in @('imports', 'managed', 'protection')) {
        $saved = $state.volumes.$group
        $actual = Get-HostVolumeIdentity $state.paths.$group
        if ($actual.volume_id -ne $saved.volume_id) { throw "Reconnect the approved $group disk to resume setup." }
        Assert-PlainLocalPath $saved.marker_directory
        $markerPath = Join-Path $saved.marker_directory '.drivebound-volume'
        if ((Test-Path -LiteralPath $markerPath) -and [IO.File]::ReadAllText($markerPath).Trim() -cne $saved.marker) { throw 'An interrupted setup found a different disk marker. Nothing was replaced; ask the host operator to review it.' }
    }
    foreach ($group in @('imports', 'managed', 'protection')) {
        $saved = $state.volumes.$group
        [void][IO.Directory]::CreateDirectory($state.paths.$group)
        [void][IO.Directory]::CreateDirectory($saved.marker_directory)
        Set-DriveboundPrivateAcl $saved.marker_directory
        $markerPath = Join-Path $saved.marker_directory '.drivebound-volume'
        if (-not (Test-Path -LiteralPath $markerPath)) { Write-DurableText $markerPath $saved.marker }
    }
    foreach ($path in @((Join-Path $state.paths.managed 'originals'), (Join-Path $state.paths.managed 'derivatives'), (Join-Path $state.paths.managed 'staging'), (Join-Path $state.paths.protection 'replicas'), (Join-Path $state.paths.protection 'backups/account-deletion-suppressions'))) {
        Assert-PlainLocalPath $path
        [void][IO.Directory]::CreateDirectory($path)
    }
    Invoke-ConfigurationTransaction -Contents $contents
    Remove-Item -LiteralPath $draftPath -Force
}
