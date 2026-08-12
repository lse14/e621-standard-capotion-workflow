[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ProjectRoot,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$AssetZip,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$Provenance,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSourceCommit
)

$ErrorActionPreference = 'Stop'

function Get-Sha256Hex([string]$Path) {
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Assert-NoReparseAncestor([string]$Path, [string]$Label) {
    $current = [System.IO.Path]::GetFullPath($Path)
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label must not be a reparse point or have a reparse ancestor: $Path"
            }
        }
        $parent = [System.IO.Directory]::GetParent($current)
        if ($null -eq $parent -or $parent.FullName -eq $current) { return }
        $current = $parent.FullName
    }
}

function Assert-WithinProject([string]$Project, [string]$Path, [string]$Label) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $prefix = $Project.TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must be inside the project root: $full"
    }
    Assert-NoReparseAncestor $full $Label
    return $full
}

function Read-Provenance([string]$Path) {
    try {
        $value = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    } catch {
        throw "Bootstrap provenance is not valid JSON: $Path"
    }
    $expected = @('schemaVersion', 'releaseVersion', 'sourceCommit', 'pythonVersion', 'assetFileName', 'assetSizeBytes', 'assetSha256', 'buildScriptSha256', 'offlineProbe')
    $actual = @($value.PSObject.Properties.Name)
    if ($actual.Count -ne $expected.Count -or @($actual | Where-Object { $expected -notcontains $_ }).Count -gt 0) {
        throw 'Bootstrap provenance fields are invalid'
    }
    if ([Int32]$value.schemaVersion -ne 1 -or [string]::IsNullOrWhiteSpace([string]$value.releaseVersion) -or
        [string]$value.sourceCommit -notmatch '^[0-9a-f]{40}$' -or [string]$value.pythonVersion -ne '3.11.15' -or
        [string]::IsNullOrWhiteSpace([string]$value.assetFileName) -or [string]$value.assetFileName -match '[\\/:]' -or
        [Int64]$value.assetSizeBytes -le 0 -or [string]$value.assetSha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$value.buildScriptSha256 -notmatch '^[0-9a-f]{64}$' -or [string]$value.offlineProbe -ne 'bootstrap-stdlib-ok') {
        throw 'Bootstrap provenance values are invalid'
    }
    return $value
}

function Get-SafeArchiveRelativePath([string]$Value) {
    $normalized = $Value.Replace([char]0x5c, [char]0x2f).TrimEnd('/')
    if ([string]::IsNullOrEmpty($normalized) -or $normalized.StartsWith('/') -or $normalized -match '^[A-Za-z]:' -or $normalized.Contains(':')) {
        throw "Bootstrap ZIP contains an unsafe entry: $Value"
    }
    foreach ($part in ($normalized -split '/')) {
        if ([string]::IsNullOrEmpty($part) -or $part -in @('.', '..') -or $part.EndsWith('.') -or $part.EndsWith(' ')) {
            throw "Bootstrap ZIP contains an unsafe entry: $Value"
        }
    }
    return $normalized
}

function Expand-VerifiedBootstrapZip([string]$ZipPath, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $seen = @{}
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName.EndsWith('/')) { continue }
            $mode = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ($mode -eq 0xA000) { throw "Bootstrap ZIP contains a link entry: $($entry.FullName)" }
            $relative = Get-SafeArchiveRelativePath $entry.FullName
            $key = $relative.ToLowerInvariant()
            if ($seen.ContainsKey($key)) { throw "Bootstrap ZIP contains duplicate entries: $relative" }
            $seen[$key] = $true
            $target = [System.IO.Path]::GetFullPath((Join-Path $Destination ($relative.Replace('/', [System.IO.Path]::DirectorySeparatorChar))))
            $prefix = $Destination.TrimEnd('\') + '\'
            if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Bootstrap ZIP entry escapes staging: $($entry.FullName)"
            }
            [System.IO.Directory]::CreateDirectory((Split-Path -Parent $target)) | Out-Null
            $input = $entry.Open()
            try {
                $output = [System.IO.File]::Open($target, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                try { $input.CopyTo($output) }
                finally { $output.Dispose() }
            }
            finally { $input.Dispose() }
        }
    }
    finally {
        $archive.Dispose()
    }
}

$stage = $null
try {
    $project = [System.IO.Path]::GetFullPath($ProjectRoot)
    if (-not (Test-Path -LiteralPath $project -PathType Container)) { throw "Project root does not exist: $project" }
    Assert-NoReparseAncestor $project 'Project root'
    $asset = Assert-WithinProject $project $AssetZip 'Bootstrap asset'
    $provenancePath = Assert-WithinProject $project $Provenance 'Bootstrap provenance'
    if (-not (Test-Path -LiteralPath $asset -PathType Leaf)) { throw "Bootstrap asset is missing: $asset" }
    if (-not (Test-Path -LiteralPath $provenancePath -PathType Leaf)) { throw "Bootstrap provenance is missing: $provenancePath" }
    $record = Read-Provenance $provenancePath
    if ([string]$record.sourceCommit -ne $ExpectedSourceCommit) { throw 'Bootstrap provenance source commit does not match the expected commit' }
    if ([System.IO.Path]::GetFileName($asset) -ne [string]$record.assetFileName) { throw 'Bootstrap asset file name does not match provenance' }
    if ((Get-Item -LiteralPath $asset -Force).Length -ne [Int64]$record.assetSizeBytes) { throw 'Bootstrap asset size does not match provenance' }
    if ((Get-Sha256Hex $asset) -ne [string]$record.assetSha256) { throw 'Bootstrap asset SHA-256 does not match provenance' }
    $builder = Join-Path $project 'packaging\scripts\build_bootstrap_runtime.ps1'
    if (-not (Test-Path -LiteralPath $builder -PathType Leaf)) { throw "Bootstrap builder is missing: $builder" }
    if ((Get-Sha256Hex $builder) -ne [string]$record.buildScriptSha256) { throw 'Bootstrap builder SHA-256 does not match provenance' }

    $stageRoot = Assert-WithinProject $project (Join-Path $project '.release-candidate\bootstrap-verify') 'Bootstrap verification staging'
    [System.IO.Directory]::CreateDirectory($stageRoot) | Out-Null
    $stage = Assert-WithinProject $project (Join-Path $stageRoot ([Guid]::NewGuid().ToString('N'))) 'Bootstrap verification staging'
    [System.IO.Directory]::CreateDirectory($stage) | Out-Null
    Expand-VerifiedBootstrapZip $asset $stage
    $required = @('python.exe', 'python311.dll', 'python311._pth', 'Lib')
    foreach ($relative in $required) {
        $path = Join-Path $stage $relative
        if (-not (Test-Path -LiteralPath $path)) { throw "Bootstrap ZIP is missing required entry: $relative" }
    }
    $python = Join-Path $stage 'python.exe'
    $probe = & $python -B -I -c "import ssl, sys, zipfile; assert sys.version_info[:2] == (3, 11); assert ssl.OPENSSL_VERSION; assert zipfile.is_zipfile is not None; print('bootstrap-stdlib-ok')"
    if ($LASTEXITCODE -ne 0 -or ($probe -join '') -notmatch 'bootstrap-stdlib-ok') {
        throw 'Bootstrap asset failed its offline standard-library probe'
    }
    Write-Output ("Bootstrap runtime asset verified: {0}" -f $asset)
    exit 0
} catch {
    Write-Output $_.Exception.Message
    exit 1
} finally {
    if ($null -ne $stage -and (Test-Path -LiteralPath $stage -PathType Container)) {
        try {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Output "Bootstrap verification staging cleanup failed: $stage"
        }
    }
}
