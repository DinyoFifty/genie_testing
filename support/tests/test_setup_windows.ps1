$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'setup_windows.ps1')

function Assert-True($Condition, $Message) {
    if (-not $Condition) { throw $Message }
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('genie-setup-test-' + [guid]::NewGuid())
$previousCapture = $env:GENIE_SETUP_TEST_ARGS
New-Item -ItemType Directory -Path $testRoot | Out-Null
$script:fakePython = Join-Path $testRoot 'fake-python.cmd'
$env:GENIE_SETUP_TEST_ARGS = Join-Path $testRoot 'arguments.txt'
try {
    $projectPython = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) '.venv/Scripts/python.exe'
    if (Test-Path -LiteralPath $projectPython) {
        Assert-True ((Find-Python311) -eq $projectPython) 'The real project Python was not detected.'
        Write-Host 'PASS: detects the real Python interpreter already installed'
    }
    if (Get-AppxPackage -Name PythonSoftwareFoundation.Python.3.11) {
        Assert-True ([bool](Find-Python311 -SkipProjectEnvironment)) 'The installed Microsoft Store Python was not detected.'
        Write-Host 'PASS: detects Microsoft Store Python without an existing project environment'
    }
    Set-Content -LiteralPath $script:fakePython -Encoding Ascii -Value @'
@echo off
echo %*>"%GENIE_SETUP_TEST_ARGS%"
echo Simulated setup output
exit /b 7
'@
    & {
        function Find-Python311 { return $script:fakePython }
        function Install-Python311 { throw 'Existing Python must not be reinstalled.' }
        $code = Invoke-GenieSetup -Arguments @('--no-open') -Root $testRoot
        Assert-True ($code -eq 7) 'The setup exit code was not preserved.'
        Assert-True ((Get-Content -LiteralPath $env:GENIE_SETUP_TEST_ARGS -Raw) -match '--no-open') 'Setup arguments were lost.'
        Assert-True ((Get-Content -LiteralPath (Join-Path $testRoot 'data/setup/setup.log') -Raw) -match 'Simulated setup output') 'The setup log missed program output.'
    }
    Write-Host 'PASS: existing Python, argument forwarding, output log, and failure exit code'

    Set-Content -LiteralPath $script:fakePython -Encoding Ascii -Value "@echo off`r`nexit /b 0"
    & {
        $script:installCount = 0
        function Find-Python311 { return $null }
        function Install-Python311 { $script:installCount++; return $script:fakePython }
        $code = Invoke-GenieSetup -Arguments @('--no-open') -Root $testRoot
        Assert-True ($code -eq 0 -and $script:installCount -eq 1) 'Missing Python did not install once and continue setup.'
    }
    Write-Host 'PASS: missing Python installs once and continues'

    & {
        $script:installCount = 0
        function Find-Python311 { return $null }
        function Install-Python311 { $script:installCount++; throw 'Check must not install.' }
        $checkRoot = Join-Path $testRoot 'check-only'
        $code = Invoke-GenieSetup -Arguments @('--check') -Root $checkRoot
        Assert-True ($code -eq 1 -and $script:installCount -eq 0) 'Check mode tried to install Python.'
        Assert-True (-not (Test-Path -LiteralPath $checkRoot)) 'Check mode wrote project files.'
    }
    Write-Host 'PASS: check mode does not install or write files'

    & {
        function Invoke-WebRequest {
            param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing)
            [IO.File]::WriteAllText($OutFile, 'corrupt download')
        }
        function Start-Process { throw 'An unverified installer must never run.' }
        $rejected = $false
        try { Install-Python311 | Out-Null } catch { $rejected = $_.Exception.Message -match 'checksum' }
        Assert-True $rejected 'A corrupt installer was not rejected before execution.'
    }
    Write-Host 'PASS: corrupt installer is rejected before execution'

    & {
        function Invoke-WebRequest {
            param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing)
            [IO.File]::WriteAllText($OutFile, 'installer fixture')
        }
        function Get-FileHash {
            return @{ Hash = '5ee42c4eee1e6b4464bb23722f90b45303f79442df63083f05322f1785f5fdde' }
        }
        function Start-Process {
            param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru, $WindowStyle)
            Assert-True ($ArgumentList -contains 'InstallAllUsers=0') 'Python must install for the current user.'
            Assert-True ($ArgumentList -contains 'Include_launcher=0') 'Setup must not request a system launcher install.'
            Assert-True ($Wait -and $PassThru -and $WindowStyle -eq 'Hidden') 'Setup must wait for the installer.'
            return @{ ExitCode = 0 }
        }
        function Find-Python311 { return $script:fakePython }
        Assert-True ((Install-Python311) -eq $script:fakePython) 'Setup did not detect Python after the installer finished.'
    }
    Write-Host 'PASS: verified installer uses user scope and detects Python afterward'
} finally {
    $env:GENIE_SETUP_TEST_ARGS = $previousCapture
    $resolved = [IO.Path]::GetFullPath($testRoot)
    $expected = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) (Split-Path $testRoot -Leaf)))
    if ($resolved -ne $expected -or (Split-Path $testRoot -Leaf) -notlike 'genie-setup-test-*') {
        throw 'Test cleanup path verification failed.'
    }
    Remove-Item -LiteralPath $testRoot -Recurse -Force
}
