[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
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

function Get-SafeDirectoryBytes([string]$Root, [string]$Directory) {
    $safeDirectory = Assert-ExistingSafePath $Root $Directory 'cache directory' $true
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($safeDirectory)
    $bytes = [Int64]0
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        Assert-ExistingSafePath $Root $current 'cache directory' $true | Out-Null
        foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "reparse point is not allowed: $($child.FullName)"
            }
            if ($child.PSIsContainer) {
                $stack.Push($child.FullName)
            }
            else {
                $bytes += [Int64]$child.Length
            }
        }
    }
    return $bytes
}

function Get-SafeCleanupTargets([string]$Root, [string]$ScanRoot) {
    $safeScanRoot = Assert-ExistingSafePath $Root $ScanRoot 'cleanup scan root' $true
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($safeScanRoot)
    $targets = New-Object 'System.Collections.Generic.List[object]'
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        Assert-ExistingSafePath $Root $current 'cleanup scan directory' $true | Out-Null
        foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "reparse point is not allowed: $($child.FullName)"
            }
            if ($child.PSIsContainer) {
                if ($child.Name -ieq '__pycache__') {
                    [void]$targets.Add([pscustomobject][ordered]@{
                        Path = $child.FullName
                        Type = 'Directory'
                        Bytes = Get-SafeDirectoryBytes $Root $child.FullName
                        Root = $safeScanRoot
                    })
                    continue
                }
                $stack.Push($child.FullName)
                continue
            }
            if ($child.Extension -ieq '.pyc') {
                [void]$targets.Add([pscustomobject][ordered]@{
                    Path = $child.FullName
                    Type = 'File'
                    Bytes = [Int64]$child.Length
                    Root = $safeScanRoot
                })
            }
        }
    }
    return $targets.ToArray()
}

$project = Assert-SafeProjectRoot $ProjectRoot
$scanRoots = @(
    (Join-Path $project 'core'),
    (Join-Path $project 'tests'),
    (Join-Path $project 'workers'),
    (Join-Path $project 'packaging'),
    (Join-Path $project '.runtime-build\runtimes')
)

$targets = @()
foreach ($scanRoot in $scanRoots) {
    $targets += @(Get-SafeCleanupTargets $project $scanRoot)
}
$orderedTargets = @($targets | Sort-Object Path)
$directoryCount = @($orderedTargets | Where-Object Type -eq 'Directory').Count
$fileCount = @($orderedTargets | Where-Object Type -eq 'File').Count
$plannedBytes = [Int64]0
foreach ($target in $orderedTargets) {
    $plannedBytes += $target.Bytes
}

foreach ($target in $orderedTargets | Select-Object -First $PreviewLimit) {
    Write-Output $target
}
if ($orderedTargets.Count -gt $PreviewLimit) {
    Write-Host ("Preview truncated at {0} of {1} records." -f $PreviewLimit, $orderedTargets.Count)
}
Write-Host ("Local artifact cleanup plan: {0} cache directories, {1} bytecode files, {2} bytes." -f (
    $directoryCount
), (
    $fileCount
), $plannedBytes)

if (-not $Apply) {
    return
}

foreach ($target in $orderedTargets) {
    $safeRoot = Assert-ExistingSafePath $project $target.Root 'cleanup scan root' $true
    $targetIsDirectory = $target.Type -eq 'Directory'
    $safeTarget = Assert-ExistingSafePath $project $target.Path 'cleanup target' $targetIsDirectory
    if ((-not (Test-PathWithin $safeRoot $safeTarget)) -or (Test-SamePath $safeRoot $safeTarget)) {
        throw "cleanup target escapes scan root: $safeTarget"
    }
    if ($targetIsDirectory) {
        if ((Get-Item -LiteralPath $safeTarget -Force).Name -ine '__pycache__') {
            throw "cleanup directory must be named __pycache__: $safeTarget"
        }
        Get-SafeDirectoryBytes $project $safeTarget | Out-Null
        Remove-Item -LiteralPath $safeTarget -Recurse -Force
        continue
    }
    if ($target.Type -ne 'File') {
        throw "unknown cleanup target type: $($target.Type)"
    }
    if ((Get-Item -LiteralPath $safeTarget -Force).Extension -ine '.pyc') {
        throw "cleanup file must have a .pyc extension: $safeTarget"
    }
    Remove-Item -LiteralPath $safeTarget -Force
}
