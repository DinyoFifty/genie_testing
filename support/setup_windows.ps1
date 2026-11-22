param([Parameter(ValueFromRemainingArguments = $true)][string[]]$SetupArgs)

$ErrorActionPreference = 'Stop'

function Find-Python311 {
    param([switch]$SkipProjectEnvironment)
    $candidates = @()
    if (-not $SkipProjectEnvironment) {
        $candidates += Join-Path (Split-Path $PSScriptRoot -Parent) '.venv/Scripts/python.exe'
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher -and $launcher.Source -notmatch '\\Microsoft\\WindowsApps\\') {
        try {
            $candidates += & $launcher.Source -3.11 -c 'import sys; print(sys.executable)' 2>$null
        } catch { }
    }
    $candidates += @(Get-Command python.exe -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch '\\Microsoft\\WindowsApps\\' } |
        ForEach-Object { $_.Source })
    if (Get-Command Get-AppxPackage -ErrorAction SilentlyContinue) {
        $candidates += @(Get-AppxPackage -Name PythonSoftwareFoundation.Python.3.11 |
            ForEach-Object { Join-Path $env:LOCALAPPDATA ('Microsoft/WindowsApps/' + $_.PackageFamilyName + '/python.exe') })
    }
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs/Python/Python311/python.exe'
    $candidates += Join-Path $env:ProgramFiles 'Python311/python.exe'
    foreach ($key in @('HKCU:\Software\Python\PythonCore\3.11\InstallPath',
                       'HKLM:\Software\Python\PythonCore\3.11\InstallPath')) {
        if (Test-Path -LiteralPath $key) {
            $location = (Get-Item -LiteralPath $key).GetValue('')
            if ($location) { $candidates += Join-Path $location 'python.exe' }
        }
    }
    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $resolved = & $candidate -c 'import sys; assert sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) { return [string]$resolved }
        } catch { }
    }
    return $null
}

function Install-Python311 {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Genie setup requires 64-bit Windows.' }
    $downloadFolder = Join-Path ([IO.Path]::GetTempPath()) ('genie-python-' + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $downloadFolder | Out-Null
    $installer = Join-Path $downloadFolder 'python-3.11.9-amd64.exe'
    $target = Join-Path $env:LOCALAPPDATA 'Programs/Python/Python311'
    try {
        Write-Host 'Python 3.11 is missing. Downloading the official Windows installer...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile $installer -TimeoutSec 300
        # SHA-256 published in the official installer's .sigstore file.
        $expected = '5ee42c4eee1e6b4464bb23722f90b45303f79442df63083f05322f1785f5fdde'
        if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $expected) {
            throw 'The Python download failed its checksum check. Run setup again to retry.'
        }
        Write-Host 'Installing Python for your Windows account. This can take a few minutes...'
        $arguments = @('/quiet', 'InstallAllUsers=0', ('TargetDir="' + $target + '"'),
            'Include_launcher=0', 'PrependPath=0', 'Include_test=0', 'Include_doc=0', 'Shortcuts=0')
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Python installation returned exit code $($process.ExitCode). Install Python 3.11 from python.org, then rerun setup."
        }
    } finally {
        if (Test-Path -LiteralPath $installer) { Remove-Item -LiteralPath $installer -Force }
        Remove-Item -LiteralPath $downloadFolder -Force
    }
    return Find-Python311
}

function Invoke-GenieSetup {
    param([string[]]$Arguments, [string]$Root = (Split-Path $PSScriptRoot -Parent))
    $transcribing = $false
    try {
        if ($Arguments -notcontains '--check') {
            $logFolder = Join-Path $root 'data/setup'
            New-Item -ItemType Directory -Path $logFolder -Force | Out-Null
            Start-Transcript -LiteralPath (Join-Path $logFolder 'setup.log') -Force | Out-Null
            $transcribing = $true
            Write-Host "Setup log: $logFolder\setup.log"
        }
        $python = Find-Python311
        if (-not $python) {
            if ($Arguments -contains '--check') {
                throw 'Python 3.11 (64-bit) is missing. Run setup.cmd without --check to install it automatically.'
            }
            $python = Install-Python311
        }
        if (-not $python) { throw 'Python 3.11 could not be located after installation. Rerun setup.' }
        Write-Host "Using Python: $python"
        & $python -B (Join-Path $root 'support/setup_genie.py') @Arguments | Out-Host
        return $LASTEXITCODE
    } catch {
        Write-Host ("Setup stopped. " + $_.Exception.Message) -ForegroundColor Red
        return 1
    } finally {
        if ($transcribing) { Stop-Transcript | Out-Null }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-GenieSetup -Arguments $SetupArgs)
}
