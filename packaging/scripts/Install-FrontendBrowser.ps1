[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Reset,
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

function Assert-PlannedDirectory([string]$Root, [string]$Path, [string]$Label) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-PathWithin $Root $fullPath)) {
        throw "$Label escapes project root: $fullPath"
    }
    if (Test-Path -LiteralPath $fullPath) {
        Assert-ExistingSafePath $Root $fullPath $Label $true | Out-Null
        return $fullPath
    }
    $current = $fullPath
    while (-not (Test-Path -LiteralPath $current)) {
        $parent = [System.IO.Path]::GetDirectoryName($current)
        if (-not $parent -or (Test-SamePath $parent $current)) {
            throw "$Label does not have a project-local ancestor: $fullPath"
        }
        $current = $parent
    }
    Assert-ExistingSafePath $Root $current "$Label ancestor" $true | Out-Null
    return $fullPath
}

function Assert-SafeDirectoryTree([string]$Root, [string]$Directory, [string]$Label) {
    $safeDirectory = Assert-ExistingSafePath $Root $Directory $Label $true
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($safeDirectory)
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        Assert-ExistingSafePath $Root $current $Label $true | Out-Null
        foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "reparse point is not allowed: $($child.FullName)"
            }
            if ($child.PSIsContainer) {
                $stack.Push($child.FullName)
            }
        }
    }
    return $safeDirectory
}

function Get-SafeFiles([string]$Root, [string]$Directory, [string]$Label) {
    $safeDirectory = Assert-ExistingSafePath $Root $Directory $Label $true
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($safeDirectory)
    $files = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        Assert-ExistingSafePath $Root $current $Label $true | Out-Null
        foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "reparse point is not allowed: $($child.FullName)"
            }
            if ($child.PSIsContainer) {
                $stack.Push($child.FullName)
            }
            else {
                [void]$files.Add($child)
            }
        }
    }
    return $files.ToArray()
}

function Get-LocalToolVersion([string]$Tool, [string]$Label) {
    $output = @(& $Tool --version)
    if ($LASTEXITCODE -ne 0) {
        throw "$Label version check failed with exit code $LASTEXITCODE"
    }
    $version = @($output | ForEach-Object { $_.ToString().Trim() } | Where-Object { $_ } | Select-Object -Last 1)
    if ($version.Count -ne 1) {
        throw "$Label version check produced no version"
    }
    return $version[0]
}

function Invoke-WithProjectNodePath([string]$NodeDirectory, [scriptblock]$Action) {
    $hadPath = Test-Path -LiteralPath 'Env:PATH'
    $previousPath = [System.Environment]::GetEnvironmentVariable('PATH', 'Process')
    $scopedPath = if ([string]::IsNullOrEmpty($previousPath)) {
        $NodeDirectory
    }
    else {
        $NodeDirectory + [System.IO.Path]::PathSeparator + $previousPath
    }
    try {
        [System.Environment]::SetEnvironmentVariable('PATH', $scopedPath, 'Process')
        & $Action
    }
    finally {
        if ($hadPath) {
            [System.Environment]::SetEnvironmentVariable('PATH', $previousPath, 'Process')
        }
        else {
            [System.Environment]::SetEnvironmentVariable('PATH', $null, 'Process')
        }
    }
}

function Remove-ValidatedFrontendTarget([string]$Project, [string]$Frontend, [string]$Target, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Target)) {
        return
    }
    $safeTarget = Assert-ExistingSafePath $Project $Target $Label $true
    if ((-not (Test-PathWithin $Frontend $safeTarget)) -or (Test-SamePath $Frontend $safeTarget)) {
        throw "$Label escapes frontend root: $safeTarget"
    }
    Assert-SafeDirectoryTree $Project $safeTarget $Label | Out-Null
    Remove-Item -LiteralPath $safeTarget -Recurse -Force
}

$project = Assert-SafeProjectRoot $ProjectRoot
$toolchain = Assert-ExistingSafePath $project (Join-Path $project '.runtime-build\node-v24.18.0-win-x64') 'project Node toolchain' $true
$nodeDirectory = $toolchain
$node = Assert-ExistingSafePath $project (Join-Path $nodeDirectory 'node.exe') 'project node executable' $false
$npm = Assert-ExistingSafePath $project (Join-Path $nodeDirectory 'npm.cmd') 'project npm executable' $false
$frontend = Assert-ExistingSafePath $project (Join-Path $project 'frontend') 'frontend root' $true
$nodeModules = Assert-PlannedDirectory $project (Join-Path $frontend 'node_modules') 'frontend node_modules'
$browserDirectory = Assert-PlannedDirectory $project (Join-Path $frontend '.playwright-browsers') 'frontend browser directory'

$nodeVersion = Get-LocalToolVersion $node 'node'
if ($nodeVersion -ne 'v24.18.0') {
    throw "node version must be v24.18.0, got $nodeVersion"
}
$npmVersion = Get-LocalToolVersion $npm 'npm'
if ($npmVersion -ne '11.16.0') {
    throw "npm version must be 11.16.0, got $npmVersion"
}

Write-Output ([pscustomobject][ordered]@{
    Action = 'InstallFrontendBrowser'
    Node = $node
    NodeVersion = $nodeVersion
    Npm = $npm
    NpmVersion = $npmVersion
    NodeModules = $nodeModules
    BrowserDirectory = $browserDirectory
    Reset = [bool]$Reset
})
Write-Host ("Frontend browser plan: Node {0}, npm {1}, reset {2}." -f $nodeVersion, $npmVersion, [bool]$Reset)

if (-not $Apply) {
    return
}

if ($Reset) {
    Remove-ValidatedFrontendTarget $project $frontend $nodeModules 'frontend node_modules'
    Remove-ValidatedFrontendTarget $project $frontend $browserDirectory 'frontend browser directory'
}

$packageLock = Assert-ExistingSafePath $project (Join-Path $frontend 'package-lock.json') 'frontend package lock' $false
Push-Location -LiteralPath $frontend
try {
    Invoke-WithProjectNodePath $nodeDirectory {
        & $npm ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) {
            throw "npm ci failed with exit code $LASTEXITCODE"
        }
    }
}
finally {
    Pop-Location
}

$playwright = Assert-ExistingSafePath $project (Join-Path $frontend 'node_modules\.bin\playwright.cmd') 'project playwright executable' $false
$hadBrowserPath = Test-Path -LiteralPath 'Env:PLAYWRIGHT_BROWSERS_PATH'
$previousBrowserPath = $env:PLAYWRIGHT_BROWSERS_PATH
try {
    [System.Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', $browserDirectory, 'Process')
    Invoke-WithProjectNodePath $nodeDirectory {
        & $playwright install chromium
        if ($LASTEXITCODE -ne 0) {
            throw "Playwright Chromium install failed with exit code $LASTEXITCODE"
        }
    }
}
finally {
    if ($hadBrowserPath) {
        [System.Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', $previousBrowserPath, 'Process')
    }
    else {
        [System.Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', $null, 'Process')
    }
}

$safeBrowserDirectory = Assert-SafeDirectoryTree $project $browserDirectory 'frontend browser directory'
$chromiumExecutables = @(Get-SafeFiles $project $safeBrowserDirectory 'frontend browser directory' | Where-Object {
    $_.Name -ieq 'chrome.exe'
})
if ($chromiumExecutables.Count -eq 0) {
    throw "no Chromium executable was installed under $safeBrowserDirectory"
}
foreach ($chromium in $chromiumExecutables) {
    if (-not (Test-PathWithin $safeBrowserDirectory $chromium.FullName)) {
        throw "Chromium executable escapes browser directory: $($chromium.FullName)"
    }
}
