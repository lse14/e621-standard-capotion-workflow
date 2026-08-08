[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')),
    [switch]$SkipManifestRefresh
)

$ErrorActionPreference = 'Stop'
$PreviewLimit = 100

function Test-SamePath([string]$Left, [string]$Right) {
    return [System.StringComparer]::OrdinalIgnoreCase.Equals(
        [System.IO.Path]::GetFullPath($Left).TrimEnd([char[]]@('\', '/')),
        [System.IO.Path]::GetFullPath($Right).TrimEnd([char[]]@('\', '/'))
    )
}

function Test-PathWithin([string]$Root, [string]$Candidate) {
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    $candidatePath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd([char[]]@('\', '/'))
    return (Test-SamePath $rootPath $candidatePath) -or $candidatePath.StartsWith(
        $rootPath + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-SafeRoot([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) { throw "project root must be a directory: $($item.FullName)" }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "reparse point is not allowed: $($item.FullName)" }
    return $item.FullName
}

function Assert-SafePath([string]$Root, [string]$Path, [string]$Label, [bool]$Directory) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($Directory -and -not $item.PSIsContainer) { throw "$Label must be a directory: $($item.FullName)" }
    if (-not $Directory -and $item.PSIsContainer) { throw "$Label must be a file: $($item.FullName)" }
    if (-not (Test-PathWithin $Root $item.FullName)) { throw "$Label escapes project root: $($item.FullName)" }
    $current = $item.FullName
    while ($true) {
        $currentItem = Get-Item -LiteralPath $current -Force
        if (($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "reparse point is not allowed: $($currentItem.FullName)" }
        if (Test-SamePath $current $Root) { break }
        $parent = [System.IO.Path]::GetDirectoryName($current)
        if (-not $parent -or (Test-SamePath $parent $current)) { throw "$Label escapes project root: $($item.FullName)" }
        $current = $parent
    }
    return $item.FullName
}

function Assert-PlannedTarget([string]$Root, [string]$Target) {
    $full = [System.IO.Path]::GetFullPath($Target)
    if (-not (Test-PathWithin $Root $full)) { throw "target escapes project root: $full" }
    if (Test-Path -LiteralPath $full) { Assert-SafePath $Root $full 'planned assembled module' $false | Out-Null; return $full }
    $current = $full
    while (-not (Test-Path -LiteralPath $current)) {
        $parent = [System.IO.Path]::GetDirectoryName($current)
        if (-not $parent -or (Test-SamePath $parent $current)) { throw "target does not have a project-local ancestor: $full" }
        $current = $parent
    }
    Assert-SafePath $Root $current 'target ancestor' $true | Out-Null
    return $full
}

function Get-SafePythonFiles([string]$Root, [string]$Directory) {
    Assert-SafePath $Root $Directory 'package directory' $true | Out-Null
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push((Get-Item -LiteralPath $Directory -Force).FullName)
    $files = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        Assert-SafePath $Root $current 'package directory' $true | Out-Null
        foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "reparse point is not allowed: $($child.FullName)" }
            if ($child.PSIsContainer) { $stack.Push($child.FullName) }
            elseif ($child.Extension -ieq '.py') { [void]$files.Add($child) }
        }
    }
    return @($files)
}

function Get-RelativePath([string]$Root, [string]$Path) {
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    if (-not (Test-PathWithin $rootPath $Path) -or (Test-SamePath $rootPath $Path)) { throw "path is not a child of package root: $Path" }
    return [System.IO.Path]::GetFullPath($Path).Substring($rootPath.Length).TrimStart([char[]]@('\', '/'))
}

function Test-FileContentEqual([string]$Source, [string]$Target) {
    if ((Get-Item -LiteralPath $Source -Force).Length -ne (Get-Item -LiteralPath $Target -Force).Length) { return $false }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Source).Hash -eq (Get-FileHash -Algorithm SHA256 -LiteralPath $Target).Hash
}

$scriptProjectRoot = Assert-SafeRoot (Join-Path $PSScriptRoot '..\..')
$project = Assert-SafeRoot $ProjectRoot
if ($SkipManifestRefresh -and (Test-SamePath $project $scriptProjectRoot)) { throw 'SkipManifestRefresh is allowed only for an isolated non-project fixture' }

$trees = @(
    @{ Source = 'workers\export\src\anima_export_worker'; Target = '.runtime-build\runtimes\export\Lib\site-packages\anima_export_worker'; Label = 'Export owner' },
    @{ Source = 'shared\anima_caption_format\anima_caption_format'; Target = '.runtime-build\runtimes\export\Lib\site-packages\anima_caption_format'; Label = 'Export shared caption format' }
)
$changes = @()
foreach ($tree in $trees) {
    $sourcePackage = Assert-SafePath $project (Join-Path $project $tree.Source) "$($tree.Label) source package" $true
    $targetPackagePath = Join-Path $project $tree.Target
    if (Test-Path -LiteralPath $targetPackagePath) {
        $targetPackage = Assert-SafePath $project $targetPackagePath "$($tree.Label) assembled package" $true
    }
    else {
        Assert-PlannedTarget $project $targetPackagePath | Out-Null
        $targetPackage = $targetPackagePath
    }
    $sourceByRelative = @{}
    foreach ($source in Get-SafePythonFiles $project $sourcePackage) { $sourceByRelative[(Get-RelativePath $sourcePackage $source.FullName)] = $source }
    $targetByRelative = @{}
    if (Test-Path -LiteralPath $targetPackage) {
        foreach ($target in Get-SafePythonFiles $project $targetPackage) { $targetByRelative[(Get-RelativePath $targetPackage $target.FullName)] = $target }
    }
    foreach ($relative in $sourceByRelative.Keys) {
        $source = $sourceByRelative[$relative]; $target = Assert-PlannedTarget $project (Join-Path $targetPackage $relative)
        if (-not $targetByRelative.ContainsKey($relative)) { $changes += [pscustomobject][ordered]@{Action='Add';Source=$source.FullName;Target=$target;Bytes=[Int64]$source.Length} }
        elseif (-not (Test-FileContentEqual $source.FullName $target)) { $changes += [pscustomobject][ordered]@{Action='Update';Source=$source.FullName;Target=$target;Bytes=[Int64]$source.Length} }
    }
    foreach ($relative in $targetByRelative.Keys) {
        if (-not $sourceByRelative.ContainsKey($relative)) { $target = $targetByRelative[$relative]; $changes += [pscustomobject][ordered]@{Action='Remove';Source=$null;Target=$target.FullName;Bytes=[Int64]$target.Length} }
    }
}

$orderedChanges = @($changes | Sort-Object Target)
foreach ($change in $orderedChanges | Select-Object -First $PreviewLimit) { Write-Output $change }
$addCount = @($orderedChanges | Where-Object Action -eq 'Add').Count
$updateCount = @($orderedChanges | Where-Object Action -eq 'Update').Count
$removeCount = @($orderedChanges | Where-Object Action -eq 'Remove').Count
$plannedBytes = [Int64]0; foreach ($change in $orderedChanges) { $plannedBytes += $change.Bytes }
Write-Host ("Export runtime sync plan: {0} add, {1} update, {2} remove, {3} bytes." -f $addCount, $updateCount, $removeCount, $plannedBytes)
if (-not $Apply) { return }

foreach ($change in $orderedChanges) {
    if ($change.Action -eq 'Remove') { Assert-SafePath $project $change.Target 'stale assembled module' $false | Out-Null; Remove-Item -LiteralPath $change.Target -Force; continue }
    $source = Assert-SafePath $project $change.Source 'Export source module' $false
    $target = Assert-PlannedTarget $project $change.Target
    $directory = [System.IO.Path]::GetDirectoryName($target)
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    Assert-SafePath $project $directory 'assembled package directory' $true | Out-Null
    if (Test-Path -LiteralPath $target) { Assert-SafePath $project $target 'assembled Export module' $false | Out-Null }
    [System.IO.File]::Copy($source, $target, $true)
}
if ($SkipManifestRefresh) { return }

$installRoot = Assert-SafePath $project (Join-Path $project '.runtime-build') 'runtime install root' $true
$requirementsRoot = Assert-SafePath $project (Join-Path $project 'packaging\requirements') 'requirements root' $true
$toolchainPython = Assert-SafePath $project (Join-Path $project '.toolchains\Python-3.11.15\PCbuild\amd64\python.exe') 'toolchain Python' $false
$manifestGenerator = Assert-SafePath $project (Join-Path $project 'packaging\scripts\generate_runtime_manifests.py') 'runtime manifest generator' $false
$corePython = Assert-SafePath $project (Join-Path $installRoot 'runtimes\core\python.exe') 'embedded Core Python' $false
$driftTest = Assert-SafePath $project (Join-Path $project 'tests\contract\test_assembled_tree_drift.py') 'assembled-tree drift test' $false
& $toolchainPython -B -I $manifestGenerator --install-root $installRoot --requirements-root $requirementsRoot --runtime-id export
if ($LASTEXITCODE -ne 0) { throw "export runtime manifest generation failed with exit code $LASTEXITCODE" }
& $corePython -B -I $driftTest
if ($LASTEXITCODE -ne 0) { throw "assembled Export drift verification failed with exit code $LASTEXITCODE" }
