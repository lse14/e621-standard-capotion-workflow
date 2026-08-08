[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
)

$ErrorActionPreference = 'Stop'

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

function Assert-ExistingSafeDirectory([string]$Root, [string]$Path, [string]$Label) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) {
        throw "$Label must be a directory: $($item.FullName)"
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

function Assert-SafeDirectoryTree([string]$Root, [string]$Directory, [string]$Label) {
    $safeDirectory = Assert-ExistingSafeDirectory $Root $Directory $Label
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($safeDirectory)
    [Int64]$bytes = 0
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        $currentItem = Get-Item -LiteralPath $current -Force
        if (-not $currentItem.PSIsContainer) {
            throw "$Label must be a directory: $($currentItem.FullName)"
        }
        if (($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "reparse point is not allowed: $($currentItem.FullName)"
        }
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
    return [pscustomobject][ordered]@{
        Path = $safeDirectory
        Type = 'Directory'
        Bytes = $bytes
    }
}

$project = Assert-SafeProjectRoot $ProjectRoot
$workingRoot = [System.IO.Path]::GetFullPath((Join-Path $project '.runtime-build\ocr-import'))
if (-not (Test-PathWithin $project $workingRoot)) {
    throw "OCR import root escapes project root: $workingRoot"
}

$approvedNames = @('environment', 'downloads', 'conversion-cache')
$targets = New-Object 'System.Collections.Generic.List[object]'
foreach ($name in $approvedNames) {
    $path = Join-Path $workingRoot $name
    if (-not (Test-Path -LiteralPath $path)) {
        continue
    }
    $record = Assert-SafeDirectoryTree $project $path "OCR import $name"
    if ((-not (Test-PathWithin $workingRoot $record.Path)) -or (Test-SamePath $workingRoot $record.Path)) {
        throw "OCR cleanup target escapes its working root: $($record.Path)"
    }
    if ((Get-Item -LiteralPath $record.Path -Force).Name -cne $name) {
        throw "unexpected OCR cleanup target name: $($record.Path)"
    }
    [void]$targets.Add($record)
}

$orderedTargets = @($targets.ToArray() | Sort-Object Path)
foreach ($target in $orderedTargets) {
    Write-Output $target
}
$plannedBytes = [Int64]0
foreach ($target in $orderedTargets) {
    $plannedBytes += [Int64]$target.Bytes
}
Write-Host ("OCR import cleanup plan: {0} directories, {1} bytes." -f $orderedTargets.Count, $plannedBytes)

if (-not $Apply) {
    return
}

# Every target tree is validated above before the first removal begins.
foreach ($target in $orderedTargets) {
    Remove-Item -LiteralPath $target.Path -Recurse -Force
}
