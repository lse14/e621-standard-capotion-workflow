[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BaseRuntime,
    [Parameter(Mandatory = $true)][string]$OutputZip,
    [Parameter(Mandatory = $true)][string]$ProvenanceOutput,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ReleaseVersion
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

function Assert-RegularTree([string]$Root) {
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Root)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $current -Force) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Base runtime contains a reparse point: $($item.FullName)"
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }
}

$base = (Resolve-Path -LiteralPath $BaseRuntime).Path
$output = [System.IO.Path]::GetFullPath($OutputZip)
$provenanceOutput = [System.IO.Path]::GetFullPath($ProvenanceOutput)
Assert-NoReparseAncestor $base 'Base runtime'
Assert-NoReparseAncestor (Split-Path -Parent $output) 'Bootstrap ZIP output'
Assert-NoReparseAncestor (Split-Path -Parent $provenanceOutput) 'Provenance output'
if (-not (Get-Item -LiteralPath $base -Force).PSIsContainer) { throw "Base runtime must be a directory: $base" }
if (Test-Path -LiteralPath $output) { throw "Bootstrap ZIP output already exists: $output" }
if (Test-Path -LiteralPath $provenanceOutput) { throw "Provenance output already exists: $provenanceOutput" }

$python = Join-Path $base 'python.exe'
$required = @(
    $python,
    (Join-Path $base 'python311.dll'),
    (Join-Path $base 'python311._pth'),
    (Join-Path $base 'Lib')
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Bootstrap base is missing required artifact: $path" }
}
$pth = Get-Content -LiteralPath (Join-Path $base 'python311._pth') -Raw -Encoding UTF8
if ($pth -notmatch '(?m)^Lib$' -or $pth -notmatch '(?m)^Lib/site-packages$') {
    throw 'Bootstrap python311._pth does not enable the bundled standard library'
}
Assert-RegularTree $base

$probe = & $python -B -I -c "import ssl, sys, zipfile; assert sys.version_info[:3] == (3, 11, 15); assert ssl.OPENSSL_VERSION; assert zipfile.is_zipfile is not None; print('bootstrap-stdlib-ok')"
if ($LASTEXITCODE -ne 0 -or ($probe -join '') -notmatch 'bootstrap-stdlib-ok') {
    throw 'Bootstrap base failed its offline standard-library probe'
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$temporary = "$output.part"
$archive = [System.IO.Compression.ZipFile]::Open($temporary, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    $prefix = $base.TrimEnd([char[]]@('\', '/')) + [System.IO.Path]::DirectorySeparatorChar
    $files = Get-ChildItem -LiteralPath $base -File -Recurse -Force | Sort-Object { $_.FullName.Substring($prefix.Length).Replace('\', '/') }
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($prefix.Length).Replace('\', '/')
        $entry = $archive.CreateEntry($relative, [System.IO.Compression.CompressionLevel]::Optimal)
        $entry.LastWriteTime = [System.DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [System.TimeSpan]::Zero)
        $input = [System.IO.File]::OpenRead($file.FullName)
        try {
            $outputStream = $entry.Open()
            try { $input.CopyTo($outputStream) }
            finally { $outputStream.Dispose() }
        }
        finally { $input.Dispose() }
    }
}
catch {
    if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary -Force }
    throw
}
finally {
    $archive.Dispose()
}
Move-Item -LiteralPath $temporary -Destination $output

$provenance = [ordered]@{
    schemaVersion = 1
    releaseVersion = $ReleaseVersion
    sourceCommit = $SourceCommit
    pythonVersion = '3.11.15'
    assetFileName = [System.IO.Path]::GetFileName($output)
    assetSizeBytes = (Get-Item -LiteralPath $output -Force).Length
    assetSha256 = Get-Sha256Hex $output
    buildScriptSha256 = Get-Sha256Hex $PSCommandPath
    offlineProbe = 'bootstrap-stdlib-ok'
}
$provenanceTemporary = "$provenanceOutput.part"
$provenance | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $provenanceTemporary -Encoding UTF8
Move-Item -LiteralPath $provenanceTemporary -Destination $provenanceOutput
