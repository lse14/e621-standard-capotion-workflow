[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('Install', 'Start', 'Stop')][string]$Action,
    [ValidateRange(1024, 65535)][int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$runtimeRoot = if (Test-Path -LiteralPath (Join-Path $projectRoot 'runtimes\core\python.exe') -PathType Leaf) {
    $projectRoot
} else {
    Join-Path $projectRoot '.runtime-build'
}
$corePython = Join-Path $runtimeRoot 'runtimes\core\python.exe'
$frontendRoot = Join-Path $projectRoot 'frontend\dist'
$resourceRoot = Join-Path $projectRoot 'resource-library'
$launcherRoot = Join-Path $projectRoot '.runtime-build\launcher'
$installStatePath = Join-Path $projectRoot '.runtime-build\manifests\install-state.json'

function Get-InstallationStateId([string]$InstallRoot) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($InstallRoot.ToLowerInvariant())
    $sha256 = New-Object System.Security.Cryptography.SHA256Managed
    try { $digest = $sha256.ComputeHash($bytes) }
    finally { $sha256.Dispose() }
    return ([BitConverter]::ToString($digest).Replace('-', '')).ToLowerInvariant()
}

function Get-WebUiInstanceId([string]$InstallRoot, [int]$LocalPort) {
    return "{0}-{1}" -f (Get-InstallationStateId $InstallRoot), $LocalPort
}

$installationStateId = Get-InstallationStateId $projectRoot
$instanceId = Get-WebUiInstanceId $projectRoot $Port
$statePath = Join-Path $launcherRoot ("webui-{0}.json" -f $instanceId)

function Assert-ReleaseBundle {
    if (-not (Test-Path -LiteralPath $installStatePath -PathType Leaf)) {
        throw "Source bootstrap installation is incomplete: $installStatePath"
    }
    try {
        $installState = Get-Content -LiteralPath $installStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([int]$installState.schemaVersion -ne 1 -or [string]::IsNullOrWhiteSpace([string]$installState.installManifestSha256)) {
            throw 'invalid installation state'
        }
    } catch {
        throw "Source bootstrap installation state is invalid: $installStatePath"
    }
    if (-not (Test-Path -LiteralPath $corePython -PathType Leaf)) {
        throw "Distributed core runtime is missing: $corePython"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $runtimeRoot 'manifests\runtimes\core.json') -PathType Leaf)) {
        throw 'Distributed core runtime manifest is missing'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot 'index.html') -PathType Leaf)) {
        throw "Built frontend is missing: $frontendRoot"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $resourceRoot 'defaults.json') -PathType Leaf)) {
        throw "Project-local resource library is missing: $resourceRoot"
    }
    & $corePython -B -I -m anima_core --check-runtime
    if ($LASTEXITCODE -ne 0) { throw 'Distributed core runtime verification failed' }
}

function Get-Listener([int]$LocalPort) {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) { return $listeners }
    $pattern = '^\s*TCP\s+127\.0\.0\.1:{0}\s+\S+\s+LISTENING\s+(\d+)\s*$' -f $LocalPort
    @(
        netstat -ano -p tcp 2>$null |
            ForEach-Object {
                if ($_ -match $pattern) { [pscustomobject]@{ OwningProcess = [int]$Matches[1] } }
            }
    )
}

function Test-HealthyWebUi([int]$LocalPort) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$LocalPort/api/health" -TimeoutSec 2
        return $health.status -eq 'ok' -and $health.protocolVersion -eq '1.0'
    } catch {
        return $false
    }
}

function Read-StateFile([string]$Path) {
    try { return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "WebUI state file is invalid: $Path" }
}

function Read-State {
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        return (Read-StateFile $statePath)
    }
    return $null
}

function Write-State([object]$State) {
    New-Item -ItemType Directory -Force -Path $launcherRoot | Out-Null
    $temporary = "$statePath.part"
    $encoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($temporary, ($State | ConvertTo-Json -Compress), $encoding)
    Move-Item -LiteralPath $temporary -Destination $statePath -Force
}

function New-Token {
    $bytes = New-Object byte[] 32
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    try { $rng.GetBytes($bytes) }
    finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes).Replace('-', '')).ToLowerInvariant()
}

function Start-WebUi {
    Assert-ReleaseBundle
    $state = Read-State
    $listeners = @(Get-Listener $Port)
    if ($listeners.Count -gt 0) {
        if ($null -ne $state -and $state.installRoot -eq $projectRoot -and $state.port -eq $Port -and ($listeners | Where-Object { $_.OwningProcess -eq [int]$state.pid })) {
            Start-Process "http://127.0.0.1:$Port/"
            return
        }
        if (Test-HealthyWebUi $Port) {
            Start-Process "http://127.0.0.1:$Port/"
            return
        }
        throw "Port $Port is already owned by another process; it will not be stopped automatically"
    }
    if ($null -ne $state) { Remove-Item -LiteralPath $statePath -Force }
    New-Item -ItemType Directory -Force -Path $launcherRoot | Out-Null
    $token = New-Token
    $stdout = Join-Path $launcherRoot ("webui-{0}.stdout.log" -f $instanceId)
    $stderr = Join-Path $launcherRoot ("webui-{0}.stderr.log" -f $instanceId)
    $arguments = '-B -I -m anima_core --port {0} --static-root "{1}" --resource-root "{2}" --shutdown-token {3}' -f $Port, $frontendRoot, $resourceRoot, $token
    $process = Start-Process -FilePath $corePython -ArgumentList $arguments -WorkingDirectory $runtimeRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $state = [ordered]@{ schemaVersion = 1; installRoot = $projectRoot; runtimeRoot = $runtimeRoot; pid = $process.Id; port = $Port; token = $token }
    Write-State $state
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            $ownedListeners = @(Get-Listener $Port | Where-Object { $_.OwningProcess -eq $process.Id })
            if ($health.status -eq 'ok' -and $ownedListeners.Count -gt 0) {
                Start-Process "http://127.0.0.1:$Port/"
                return
            }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    throw 'WebUI backend did not become healthy within 30 seconds; see launcher logs'
}

function Stop-WebUi {
    $state = Read-State
    if ($null -eq $state) {
        if ((@(Get-Listener $Port).Count -eq 0)) { return }
        throw "Port $Port is listening but this installation has no matching state file"
    }
    $stateBelongsToInstallation = (
        ([string]$state.installRoot -eq [string]$projectRoot) -and
        (([int]$state.port) -eq $Port) -and
        (([string]$state.token) -match '^[0-9a-f]{64}$')
    )
    if (-not $stateBelongsToInstallation) {
        throw 'WebUI state file does not belong to this installation'
    }
    $listeners = @(Get-Listener $Port)
    if ($listeners.Count -eq 0) {
        Remove-Item -LiteralPath $statePath -Force
        return
    }
    if (-not ($listeners | Where-Object { $_.OwningProcess -eq [int]$state.pid })) {
        throw "Port $Port is no longer owned by this installation"
    }
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/application/shutdown" -ContentType 'application/json' -Body (@{ token = [string]$state.token } | ConvertTo-Json -Compress) -TimeoutSec 10 | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ((@(Get-Listener $Port).Count -eq 0)) {
            Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "WebUI backend did not release port $Port within 60 seconds; it was not force-terminated"
}

switch ($Action) {
    'Install' {
        Assert-ReleaseBundle
        New-Item -ItemType Directory -Force -Path $launcherRoot | Out-Null
        Write-Output 'Anima source bootstrap installation is ready.'
    }
    'Start' { Start-WebUi }
    'Stop' { Stop-WebUi }
}
