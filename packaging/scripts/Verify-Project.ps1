[CmdletBinding()]
param(
    [ValidateSet('Fast', 'Full', 'Release')][string]$Level = 'Full',
    [ValidateSet('Auto', 'None', 'Cpu', 'Gpu')][string]$OcrMode = 'Auto',
    [string]$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')),
    [string]$InstallRoot = ''
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

function Resolve-OcrMode([string]$Requested) {
    $code = 'from anima_core.runtime_manifest import inspect_optional_ocr_runtime_state; import sys; print(inspect_optional_ocr_runtime_state(sys.argv[1]))'
    $actual = (& $script:corePython -B -I -c $code $script:runtimeRoot).Trim()
    if ($LASTEXITCODE -ne 0 -or $actual -notin @('none', 'cpu', 'gpu')) {
        throw 'optional OCR state is invalid; refusing to continue'
    }
    if ($Requested -ne 'Auto' -and $actual -ne $Requested.ToLowerInvariant()) {
        throw "requested OCR mode $Requested does not match installed state $actual"
    }
    return $actual.Substring(0, 1).ToUpperInvariant() + $actual.Substring(1)
}

function New-Gate([string]$Name, [string]$Executable, [string[]]$Arguments, [object]$BrowserPath) {
    return [pscustomobject][ordered]@{
        Name = $Name
        Executable = $Executable
        Arguments = @($Arguments)
        BrowserPath = $BrowserPath
    }
}

function Invoke-WithProjectNodePath([string]$NodeDirectory, [scriptblock]$Action, [ref]$ExitCode) {
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
        $ExitCode.Value = $LASTEXITCODE
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

function Get-AvailableLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        $endpoint = $listener.LocalEndpoint
        return [int]$endpoint.Port
    }
    finally {
        $listener.Stop()
    }
}

function Get-Gates {
    [CmdletBinding()]
    param(
        [ValidateSet('Fast', 'Full', 'Release')][string]$Level,
        [ValidateSet('None', 'Cpu', 'Gpu')][string]$OcrMode = $script:ocrMode
    )

    $fast = @(
        (New-Gate 'embedded-fast' $script:corePython @('-B', '-I', $script:testRunner, '--level', 'fast', '--ocr-mode', $OcrMode.ToLowerInvariant(), '--install-root', $script:runtimeRoot) $null),
        (New-Gate 'frontend-typecheck' $script:npm @('--prefix', $script:frontend, 'run', 'typecheck') $null)
    )
    $full = @(
        (New-Gate 'embedded-full' $script:corePython @('-B', '-I', $script:testRunner, '--level', 'full', '--ocr-mode', $OcrMode.ToLowerInvariant(), '--install-root', $script:runtimeRoot) $null),
        (New-Gate 'frontend-typecheck' $script:npm @('--prefix', $script:frontend, 'run', 'typecheck') $null),
        (New-Gate 'frontend-build' $script:npm @('--prefix', $script:frontend, 'run', 'build') $null),
        (New-Gate 'ocr-integration' $script:corePython @('-B', '-I', '-m', 'unittest', 'discover', '-s', (Join-Path $script:root 'tests\integration'), '-t', $script:root, '-p', 'test_ocr_end_to_end.py', '-v') $null)
    )
    if ($OcrMode -eq 'None') {
        $full = @($full | Where-Object { $_.Name -ne 'ocr-integration' })
    }
    if ($Level -eq 'Fast') {
        return $fast
    }
    if ($Level -eq 'Full') {
        return $full
    }
    return @(
        $full + @(
            (New-Gate 'assembled-drift' $script:corePython @('-B', '-I', $script:assembledDrift, '--ocr-mode', $OcrMode.ToLowerInvariant()) $null),
            (New-Gate 'frontend-e2e' $script:npm @('--prefix', $script:frontend, 'run', 'test:e2e') $script:browserDirectory),
            (New-Gate 'resource-validation' $script:toolchainPython @('-B', '-I', $script:resourceValidator, '--root', $script:resourceRoot) $null)
        )
    )
}

function Invoke-Gate {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments = @(),
        [string]$BrowserPath
    )

    Write-Host ("[{0}] {1}" -f $Name, ((@($Executable) + @($Arguments)) -join ' '))
    $exitCode = 0
    $hasBrowserPath = -not [string]::IsNullOrEmpty($BrowserPath)
    $hadBrowserPath = $false
    $previousBrowserPath = $null
    $hasE2ePort = $Name -eq 'frontend-e2e'
    $hadE2ePort = $false
    $previousE2ePort = $null
    $e2ePort = $null
    $usesInstalledOcr = $Name -in @('ocr-integration', 'assembled-drift')
    $hadInstallRoot = $false
    $previousInstallRoot = $null
    $hadResourceRoot = $false
    $previousResourceRoot = $null
    if ($usesInstalledOcr) {
        $hadInstallRoot = Test-Path -LiteralPath 'Env:ANIMA_INSTALL_ROOT'
        $previousInstallRoot = $env:ANIMA_INSTALL_ROOT
        $hadResourceRoot = Test-Path -LiteralPath 'Env:ANIMA_RESOURCE_ROOT'
        $previousResourceRoot = $env:ANIMA_RESOURCE_ROOT
    }
    if ($hasBrowserPath) {
        $hadBrowserPath = Test-Path -LiteralPath 'Env:PLAYWRIGHT_BROWSERS_PATH'
        $previousBrowserPath = $env:PLAYWRIGHT_BROWSERS_PATH
    }
    if ($hasE2ePort) {
        $hadE2ePort = Test-Path -LiteralPath 'Env:ANIMA_E2E_PORT'
        $previousE2ePort = $env:ANIMA_E2E_PORT
        $e2ePort = Get-AvailableLoopbackPort
    }
    try {
        if ($usesInstalledOcr) {
            [System.Environment]::SetEnvironmentVariable('ANIMA_INSTALL_ROOT', $script:runtimeRoot, 'Process')
            [System.Environment]::SetEnvironmentVariable('ANIMA_RESOURCE_ROOT', $script:resourceRoot, 'Process')
        }
        if ($hasBrowserPath) {
            [System.Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', $BrowserPath, 'Process')
        }
        if ($hasE2ePort) {
            [System.Environment]::SetEnvironmentVariable('ANIMA_E2E_PORT', $e2ePort.ToString(), 'Process')
        }
        if (Test-SamePath $Executable $script:npm) {
            Invoke-WithProjectNodePath $script:nodeDirectory { & $Executable @Arguments } ([ref]$exitCode)
        }
        else {
            & $Executable @Arguments
            $exitCode = $LASTEXITCODE
        }
    }
    finally {
        if ($usesInstalledOcr) {
            if ($hadInstallRoot) {
                [System.Environment]::SetEnvironmentVariable('ANIMA_INSTALL_ROOT', $previousInstallRoot, 'Process')
            }
            else {
                [System.Environment]::SetEnvironmentVariable('ANIMA_INSTALL_ROOT', $null, 'Process')
            }
            if ($hadResourceRoot) {
                [System.Environment]::SetEnvironmentVariable('ANIMA_RESOURCE_ROOT', $previousResourceRoot, 'Process')
            }
            else {
                [System.Environment]::SetEnvironmentVariable('ANIMA_RESOURCE_ROOT', $null, 'Process')
            }
        }
        if ($hasBrowserPath) {
            if ($hadBrowserPath) {
                [System.Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', $previousBrowserPath, 'Process')
            }
            else {
                [System.Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', $null, 'Process')
            }
        }
        if ($hasE2ePort) {
            if ($hadE2ePort) {
                [System.Environment]::SetEnvironmentVariable('ANIMA_E2E_PORT', $previousE2ePort, 'Process')
            }
            else {
                [System.Environment]::SetEnvironmentVariable('ANIMA_E2E_PORT', $null, 'Process')
            }
        }
    }
    if ($exitCode -ne 0) {
        exit $exitCode
    }
}

$script:root = Assert-SafeProjectRoot $ProjectRoot
$selectedInstallRoot = if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    Join-Path $script:root '.runtime-build'
}
else {
    $InstallRoot
}
$script:runtimeRoot = Assert-SafeProjectRoot $selectedInstallRoot
$script:corePython = Assert-ExistingSafePath $script:runtimeRoot (Join-Path $script:runtimeRoot 'runtimes\core\python.exe') 'embedded Core Python' $false
$script:toolchainPython = Assert-ExistingSafePath $script:root (Join-Path $script:root '.toolchains\Python-3.11.15\PCbuild\amd64\python.exe') 'project toolchain Python' $false
$script:nodeDirectory = Assert-ExistingSafePath $script:root (Join-Path $script:root '.toolchains\node-v24.18.0-win-x64') 'project Node toolchain' $true
$script:node = Assert-ExistingSafePath $script:root (Join-Path $script:nodeDirectory 'node.exe') 'project node executable' $false
$script:npm = Assert-ExistingSafePath $script:root (Join-Path $script:nodeDirectory 'npm.cmd') 'project npm executable' $false
$script:frontend = Assert-ExistingSafePath $script:root (Join-Path $script:root 'frontend') 'frontend root' $true
$script:testRunner = Assert-ExistingSafePath $script:root (Join-Path $script:root 'tests\run_embedded_suite.py') 'embedded test runner' $false
$script:assembledDrift = Assert-ExistingSafePath $script:root (Join-Path $script:root 'tests\contract\test_assembled_tree_drift.py') 'assembled-tree drift test' $false
$script:resourceValidator = Assert-ExistingSafePath $script:root (Join-Path $script:root 'packaging\scripts\validate_resource_library.py') 'resource validation script' $false
$resourceOwner = if ([string]::IsNullOrWhiteSpace($InstallRoot)) { $script:root } else { $script:runtimeRoot }
$script:resourceRoot = Assert-ExistingSafePath $resourceOwner (Join-Path $resourceOwner 'resource-library') 'resource library' $true
$script:browserDirectory = Assert-PlannedDirectory $script:root (Join-Path $script:frontend '.playwright-browsers') 'frontend browser directory'
$script:ocrMode = Resolve-OcrMode $OcrMode

if ($MyInvocation.InvocationName -ne '.') {
    foreach ($gate in @(Get-Gates -Level $Level -OcrMode $script:ocrMode)) {
        Invoke-Gate -Name $gate.Name -Executable $gate.Executable -Arguments @($gate.Arguments) -BrowserPath $gate.BrowserPath
    }
}
