param(
    [string]$EnvPython = "",
    [switch]$CheckOnly,
    [switch]$AllowPycOnly,
    [switch]$RestartExisting
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Add-PathPrefix([string]$PathToAdd) {
    if (-not $PathToAdd) { return }
    if (-not (Test-Path $PathToAdd)) { return }
    $current = $env:PATH -split ";"
    if ($current -contains $PathToAdd) { return }
    $env:PATH = "$PathToAdd;$env:PATH"
}

function Resolve-Python([string]$PreferredPython) {
    $candidates = @()
    if ($PreferredPython) { $candidates += $PreferredPython }
    $candidates += @(
        "C:\Users\under\.conda\envs\xiaozhi-esp32-server\python.exe",
        "C:\Users\under\.conda\envs\xiaozhi310\python.exe",
        "C:\Users\under\.conda\envs\richard-esp32-server\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) { return $pythonCmd.Source }
    return $null
}

function Resolve-PythonForPyc([string]$PycName) {
    if ($PycName -notmatch "cpython-(\d+)\.pyc$") { return $null }
    $tag = $Matches[1]
    if ($tag.Length -lt 2) { return $null }
    $major = $tag.Substring(0, 1)
    $minor = $tag.Substring(1)
    $pyVersion = "$major.$minor"

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pyLauncher) { return $null }

    $resolved = (& py -$pyVersion -c "import sys;print(sys.executable)" 2>$null).Trim()
    if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path $resolved)) {
        return $resolved
    }
    return $null
}

function Resolve-PycEntry([string]$PythonExe, [string]$ProjectRoot) {
    $cacheDir = Join-Path $ProjectRoot "__pycache__"
    if (-not (Test-Path $cacheDir)) { return $null }

    $pyVersion = (& $PythonExe -c "import sys;print(f'{sys.version_info.major}{sys.version_info.minor}')").Trim()
    $preferred = Join-Path $cacheDir ("app.cpython-{0}.pyc" -f $pyVersion)
    if (Test-Path $preferred) {
        return @{ Path = $preferred; Compatible = $true; PycName = (Split-Path $preferred -Leaf) }
    }

    $fallback = Get-ChildItem -Path $cacheDir -File -Filter "app.cpython-*.pyc" |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if ($fallback) {
        return @{ Path = $fallback.FullName; Compatible = $false; PythonTag = $pyVersion; PycName = $fallback.Name }
    }
    return $null
}

function Get-ConfiguredPort([string]$ConfigPath, [string]$Key, [int]$DefaultValue) {
    if (-not (Test-Path $ConfigPath)) { return $DefaultValue }
    $line = Select-String -Path $ConfigPath -Pattern ("^\s*{0}:\s*(\d+)\s*$" -f [regex]::Escape($Key)) -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $line) { return $DefaultValue }
    $value = $line.Matches[0].Groups[1].Value
    if (-not $value) { return $DefaultValue }
    return [int]$value
}

function Get-ListeningPidsOnPort([int]$Port) {
    $conns = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if (-not $conns) { return @() }
    return ($conns | Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-AppProcessIds([string]$ProjectRoot) {
    $escapedRoot = [regex]::Escape($ProjectRoot)
    $procs = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match $escapedRoot -and
            ($_.CommandLine -match "app\.py" -or $_.CommandLine -match "app\.cpython-\d+\.pyc")
        }
    if (-not $procs) { return @() }
    return ($procs | Select-Object -ExpandProperty ProcessId -Unique)
}

$projectRoot = $PSScriptRoot
$pythonExe = Resolve-Python -PreferredPython $EnvPython
if (-not $pythonExe) {
    throw "Python runtime not found. Use -EnvPython to provide a valid python.exe path."
}

$pythonRoot = Split-Path -Path $pythonExe -Parent
$libraryBin = Join-Path $pythonRoot "Library\bin"
Add-PathPrefix $libraryBin
Add-PathPrefix "C:\Users\under\.conda\pkgs\libopus-1.6.1-h6a83c73_0\Library\bin"

$entryPath = Join-Path $projectRoot "app.py"
$entryType = "source"
if (-not (Test-Path $entryPath)) {
    $entryType = "pyc"
    $pycEntry = Resolve-PycEntry -PythonExe $pythonExe -ProjectRoot $projectRoot
    if (-not $pycEntry) {
        throw "Entry not found in current directory. Expected app.py or __pycache__\\app.cpython-*.pyc."
    }
    if (-not $pycEntry.Compatible) {
        $matchingPython = Resolve-PythonForPyc -PycName $pycEntry.PycName
        if ($matchingPython) {
            $pythonExe = $matchingPython
            $pythonRoot = Split-Path -Path $pythonExe -Parent
            $libraryBin = Join-Path $pythonRoot "Library\bin"
            Add-PathPrefix $libraryBin
            $pycEntry = Resolve-PycEntry -PythonExe $pythonExe -ProjectRoot $projectRoot
        }
    }
    if (-not $pycEntry.Compatible) {
        throw ("Python version tag is cpython-{0}, but entry pyc is {1}. Install matching Python or restore app.py source." -f $pycEntry.PythonTag, $pycEntry.PycName)
    }
    if (-not $AllowPycOnly -and -not $CheckOnly) {
        throw "app.py is missing in current directory. This startup mode is current-directory only; restore app.py or use -AllowPycOnly."
    }
    Write-Warning "Running from pyc only in current directory."
    if ($env:PYTHONPATH) {
        $env:PYTHONPATH = "$projectRoot;$env:PYTHONPATH"
    } else {
        $env:PYTHONPATH = $projectRoot
    }
    $entryPath = $pycEntry.Path
}

$runtimeConfig = Join-Path $projectRoot "data\.config.yaml"
if (-not (Test-Path $runtimeConfig)) {
    throw "Missing runtime config: data/.config.yaml"
}

if (-not $CheckOnly) {
    $appPids = Get-AppProcessIds -ProjectRoot $projectRoot
    if ($appPids.Count -gt 0) {
        if ($RestartExisting) {
            foreach ($procId in $appPids) {
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            }
            Start-Sleep -Seconds 1
        } else {
            Write-Warning ("Server already running in current directory. PID: {0}. Use -RestartExisting to restart." -f ($appPids -join ","))
            exit 0
        }
    }

    $wsPort = Get-ConfiguredPort -ConfigPath $runtimeConfig -Key "port" -DefaultValue 9000
    $httpPort = Get-ConfiguredPort -ConfigPath $runtimeConfig -Key "http_port" -DefaultValue 9003
    $blocked = @()
    foreach ($port in @($wsPort, $httpPort)) {
        $owners = Get-ListeningPidsOnPort -Port $port
        if ($owners.Count -gt 0) {
            $blocked += ("port {0} -> pid {1}" -f $port, ($owners -join ","))
        }
    }
    if ($blocked.Count -gt 0) {
        throw ("Port conflict detected: {0}" -f ($blocked -join "; "))
    }
}

& $pythonExe -c "import ctypes.util, shutil, sys; print('python=', sys.executable); print('ffmpeg=', shutil.which('ffmpeg')); print('opus=', ctypes.util.find_library('opus')); sys.exit(0)"
if ($LASTEXITCODE -ne 0) {
    throw "Runtime self-check failed."
}

if ($CheckOnly) {
    Write-Host "Check passed. root=$projectRoot python=$pythonExe entry=$entryPath type=$entryType"
    exit 0
}

& $pythonExe $entryPath
exit $LASTEXITCODE
