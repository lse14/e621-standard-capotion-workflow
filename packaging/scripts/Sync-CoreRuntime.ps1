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
    $normalizedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    $normalizedCandidate = [System.IO.Path]::GetFullPath($Candidate).TrimEnd([char[]]@('\', '/'))
    if ([System.StringComparer]::OrdinalIgnoreCase.Equals($normalizedRoot, $normalizedCandidate)) {
        return $true
    }
    $prefix = $normalizedRoot + [System.IO.Path]::DirectorySeparatorChar
    return $normalizedCandidate.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-SafeProjectRoot([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) {
        throw "project root must be a directory: $($item.FullName)"
    }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "reparse point is not allowed: $($item.FullName)"
    }
    return $item.FullName
}

function Assert-ExistingSafePath([string]$Root, [string]$Path, [string]$Label, [bool]$Directory) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($Directory -and -not $item.PSIsContainer) {
        throw "$Label must be a directory: $($item.FullName)"
    }
    if (-not $Directory -and $item.PSIsContainer) {
        throw "$Label must be a file: $($item.FullName)"
    }
    if (-not (Test-PathWithin $Root $item.FullName)) {
        throw "$Label escapes project root: $($item.FullName)"
    }
    $current = $item.FullName
    while ($true) {
        $currentItem = Get-Item -LiteralPath $current -Force
        if (($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "reparse point is not allowed: $($currentItem.FullName)"
        }
        if (Test-SamePath $current $Root) {
            break
        }
        $parent = [System.IO.Path]::GetDirectoryName($current)
        if (-not $parent -or (Test-SamePath $parent $current)) {
            throw "$Label escapes project root: $($item.FullName)"
        }
        $current = $parent
    }
    return $item.FullName
}

function Assert-PlannedTarget([string]$Root, [string]$Target) {
    $fullTarget = [System.IO.Path]::GetFullPath($Target)
    if (-not (Test-PathWithin $Root $fullTarget)) {
        throw "target escapes project root: $fullTarget"
    }
    if (Test-Path -LiteralPath $fullTarget) {
        Assert-ExistingSafePath $Root $fullTarget 'planned assembled module' $false | Out-Null
        return $fullTarget
    }
    $current = $fullTarget
    while (-not (Test-Path -LiteralPath $current)) {
        $parent = [System.IO.Path]::GetDirectoryName($current)
        if (-not $parent -or (Test-SamePath $parent $current)) {
            throw "target does not have a project-local ancestor: $fullTarget"
        }
        $current = $parent
    }
    Assert-ExistingSafePath $Root $current 'target ancestor' $true | Out-Null
    return $fullTarget
}

function Get-SafePythonFiles([string]$Root, [string]$Directory) {
    Assert-ExistingSafePath $Root $Directory 'package directory' $true | Out-Null
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push((Get-Item -LiteralPath $Directory -Force).FullName)
    $files = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        Assert-ExistingSafePath $Root $current 'package directory' $true | Out-Null
        foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "reparse point is not allowed: $($child.FullName)"
            }
            if ($child.PSIsContainer) {
                $stack.Push($child.FullName)
            }
            elseif ($child.Extension -ieq '.py') {
                [void]$files.Add($child)
            }
        }
    }
    return @($files)
}

function Get-RelativePath([string]$Root, [string]$Path) {
    if ((-not (Test-PathWithin $Root $Path)) -or (Test-SamePath $Root $Path)) {
        throw "path is not a child of package root: $Path"
    }
    return [System.IO.Path]::GetFullPath($Path).Substring(
        [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/')).Length
    ).TrimStart([char[]]@('\', '/'))
}

function Test-FileContentEqual([string]$Source, [string]$Target) {
    $sourceItem = Get-Item -LiteralPath $Source -Force
    $targetItem = Get-Item -LiteralPath $Target -Force
    if ($sourceItem.Length -ne $targetItem.Length) {
        return $false
    }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Source).Hash -eq (
        Get-FileHash -Algorithm SHA256 -LiteralPath $Target
    ).Hash
}

$scriptProjectRoot = Assert-SafeProjectRoot (Join-Path $PSScriptRoot '..\..')
$project = Assert-SafeProjectRoot $ProjectRoot
if ($SkipManifestRefresh -and (Test-SamePath $project $scriptProjectRoot)) {
    throw 'SkipManifestRefresh is allowed only for an isolated non-project fixture'
}

$trees = @(
    @{ Source = 'core\src\anima_core'; Target = '.runtime-build\runtimes\core\Lib\site-packages\anima_core'; Label = 'Core owner' },
    @{ Source = 'shared\anima_caption_format\anima_caption_format'; Target = '.runtime-build\runtimes\core\Lib\site-packages\anima_caption_format'; Label = 'Core shared caption format' }
)
$controlledFiles = @(
    @{ Source = 'core\src\anima_core\benchmark_baseline_v1.json'; Target = '.runtime-build\runtimes\core\Lib\site-packages\anima_core\benchmark_baseline_v1.json'; Label = 'Core baseline asset' }
)

if (-not $SkipManifestRefresh) {
    $installRoot = Assert-ExistingSafePath $project (Join-Path $project '.runtime-build') 'runtime install root' $true
    $requirementsRoot = Assert-ExistingSafePath $project (Join-Path $project 'packaging\requirements') 'requirements root' $true
    $manifestRoot = Assert-ExistingSafePath $project (Join-Path $installRoot 'manifests\runtimes') 'runtime manifest root' $true
    $toolchainPython = Assert-ExistingSafePath $project (
        Join-Path $project '.toolchains\Python-3.11.15\PCbuild\amd64\python.exe'
    ) 'toolchain Python' $false
    $manifestGenerator = Assert-ExistingSafePath $project (
        Join-Path $project 'packaging\scripts\generate_runtime_manifests.py'
    ) 'runtime manifest generator' $false
    $corePython = Assert-ExistingSafePath $project (
        Join-Path $installRoot 'runtimes\core\python.exe'
    ) 'embedded Core Python' $false
    $driftTest = Assert-ExistingSafePath $project (
        Join-Path $project 'tests\contract\test_assembled_tree_drift.py'
    ) 'assembled-tree drift test' $false
}

$changes = @()
foreach ($tree in $trees) {
    $sourcePackage = Assert-ExistingSafePath $project (Join-Path $project $tree.Source) "$($tree.Label) source package" $true
    $targetPackagePath = Join-Path $project $tree.Target
    if (Test-Path -LiteralPath $targetPackagePath) {
        $targetPackage = Assert-ExistingSafePath $project $targetPackagePath "$($tree.Label) assembled package" $true
    }
    else {
        Assert-PlannedTarget $project $targetPackagePath | Out-Null
        $targetPackage = $targetPackagePath
    }
    $sourceByRelative = @{}
    foreach ($source in Get-SafePythonFiles $project $sourcePackage) {
        $sourceByRelative[(Get-RelativePath $sourcePackage $source.FullName)] = $source
    }
    $targetByRelative = @{}
    if (Test-Path -LiteralPath $targetPackage) {
        foreach ($target in Get-SafePythonFiles $project $targetPackage) {
            $targetByRelative[(Get-RelativePath $targetPackage $target.FullName)] = $target
        }
    }
    foreach ($relative in $sourceByRelative.Keys) {
        $source = $sourceByRelative[$relative]
        $target = Assert-PlannedTarget $project (Join-Path $targetPackage $relative)
        if (-not $targetByRelative.ContainsKey($relative)) {
            $changes += [pscustomobject][ordered]@{ Action = 'Add'; Source = $source.FullName; Target = $target; Bytes = [Int64]$source.Length }
        }
        elseif (-not (Test-FileContentEqual $source.FullName $target)) {
            $changes += [pscustomobject][ordered]@{ Action = 'Update'; Source = $source.FullName; Target = $target; Bytes = [Int64]$source.Length }
        }
    }
    foreach ($relative in $targetByRelative.Keys) {
        if (-not $sourceByRelative.ContainsKey($relative)) {
            $target = $targetByRelative[$relative]
            $changes += [pscustomobject][ordered]@{ Action = 'Remove'; Source = $null; Target = $target.FullName; Bytes = [Int64]$target.Length }
        }
    }
}

foreach ($controlledFile in $controlledFiles) {
    $source = Assert-ExistingSafePath $project (Join-Path $project $controlledFile.Source) "$($controlledFile.Label) source file" $false
    $target = Assert-PlannedTarget $project (Join-Path $project $controlledFile.Target)
    if (-not (Test-Path -LiteralPath $target)) {
        $changes += [pscustomobject][ordered]@{ Action = 'Add'; Source = $source; Target = $target; Bytes = [Int64](Get-Item -LiteralPath $source -Force).Length }
    }
    elseif (-not (Test-FileContentEqual $source $target)) {
        $changes += [pscustomobject][ordered]@{ Action = 'Update'; Source = $source; Target = $target; Bytes = [Int64](Get-Item -LiteralPath $source -Force).Length }
    }
}

$orderedChanges = @($changes | Sort-Object Target)
foreach ($change in $orderedChanges | Select-Object -First $PreviewLimit) {
    Write-Output $change
}
$addCount = @($orderedChanges | Where-Object Action -eq 'Add').Count
$updateCount = @($orderedChanges | Where-Object Action -eq 'Update').Count
$removeCount = @($orderedChanges | Where-Object Action -eq 'Remove').Count
$plannedBytes = [Int64]0
foreach ($change in $orderedChanges) {
    $plannedBytes += $change.Bytes
}
if ($orderedChanges.Count -gt $PreviewLimit) {
    Write-Host ("Preview truncated at {0} of {1} records." -f $PreviewLimit, $orderedChanges.Count)
}
# Keep the success stream machine-readable for callers that collect change records.
Write-Host ("Core runtime sync plan: {0} add, {1} update, {2} remove, {3} bytes." -f (
    $addCount
), (
    $updateCount
), (
    $removeCount
), $plannedBytes)

if (-not $Apply) {
    return
}

foreach ($change in $orderedChanges) {
    if ($change.Action -eq 'Remove') {
        $target = Assert-ExistingSafePath $project $change.Target 'stale assembled module' $false
        $owned = $false
        foreach ($tree in $trees) {
            if (Test-PathWithin (Join-Path $project $tree.Target) $target) {
                $owned = $true
                break
            }
        }
        if (-not $owned) {
            throw "stale module escapes an assembled package: $target"
        }
        Remove-Item -LiteralPath $target -Force
        continue
    }
    $source = Assert-ExistingSafePath $project $change.Source 'Core source module' $false
    $target = Assert-PlannedTarget $project $change.Target
    $destinationDirectory = [System.IO.Path]::GetDirectoryName($target)
    New-Item -ItemType Directory -Force -Path $destinationDirectory | Out-Null
    Assert-ExistingSafePath $project $destinationDirectory 'assembled package directory' $true | Out-Null
    if (Test-Path -LiteralPath $target) {
        Assert-ExistingSafePath $project $target 'assembled Core module' $false | Out-Null
    }
    [System.IO.File]::Copy($source, $target, $true)
}

if ($SkipManifestRefresh) {
    return
}

& $toolchainPython -B -I $manifestGenerator --install-root $installRoot --requirements-root $requirementsRoot --runtime-id core
if ($LASTEXITCODE -ne 0) {
    throw "runtime manifest generation failed with exit code $LASTEXITCODE"
}
${env:ANIMA_DRIFT_RUNTIME_IDS} = 'core'
try {
    & $corePython -B -I $driftTest
    if ($LASTEXITCODE -ne 0) { throw "assembled Core drift verification failed with exit code $LASTEXITCODE" }
}
finally {
    Remove-Item -LiteralPath Env:ANIMA_DRIFT_RUNTIME_IDS -ErrorAction SilentlyContinue
}
