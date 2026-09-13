param([string]$Python = '', [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$tingyeProject = $PSScriptRoot
$tingyeData = Join-Path $env:USERPROFILE '.tingye'
$tingyeLogs = Join-Path $tingyeData 'logs'
New-Item -ItemType Directory -Path $tingyeData,$tingyeLogs -Force | Out-Null
$tingyeRuntimeFile = Join-Path $tingyeData 'python-runtime.txt'
$tingyeSetupLog = Join-Path $tingyeLogs 'setup.log'
function Invoke-TingyePython {
    param([string]$Runtime, [string[]]$RuntimeArguments, [switch]$AppendLog)
    $tingyeOldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($AppendLog) { & $Runtime @RuntimeArguments *>> $tingyeSetupLog }
        else { & $Runtime @RuntimeArguments *> $tingyeSetupLog }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $tingyeOldPreference }
}
try {
    $tingyeAlreadyRunning = $false
    try {
        $tingyeHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:18761/health' -TimeoutSec 2
        $tingyeAlreadyRunning = $tingyeHealth.app -eq 'tingye'
    } catch { }
    if ($tingyeAlreadyRunning) {
        if (!$NoBrowser) { Start-Process 'http://127.0.0.1:18761' | Out-Null }
        exit 0
    }
    if ($Python -and !(Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'The selected Python runtime does not exist.' }
    if (!$Python -and (Test-Path -LiteralPath $tingyeRuntimeFile)) {
        $tingyeSavedRuntime = (Get-Content -LiteralPath $tingyeRuntimeFile -Raw -Encoding UTF8).Trim()
        if (Test-Path -LiteralPath $tingyeSavedRuntime -PathType Leaf) { $Python = $tingyeSavedRuntime }
    }
    if (!$Python) {
        $tingyeVenv = Join-Path $tingyeData 'venv'
        $Python = Join-Path $tingyeVenv 'Scripts\python.exe'
        if (!(Test-Path -LiteralPath $Python)) {
            $tingyeBase = Get-Command python.exe -ErrorAction SilentlyContinue
            if (!$tingyeBase) { throw 'Please install Python 3.11 or newer from python.org, then start Tingye again.' }
            $tingyeExit = Invoke-TingyePython -Runtime $tingyeBase.Source -RuntimeArguments @('-c','import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)')
            if ($tingyeExit -ne 0) { throw 'Tingye requires Python 3.11 or newer.' }
            $tingyeExit = Invoke-TingyePython -Runtime $tingyeBase.Source -RuntimeArguments @('-m','venv',$tingyeVenv) -AppendLog
            if ($tingyeExit -ne 0) { throw 'Could not create the local Python environment. See setup.log.' }
        }
    }
    $tingyeExit = Invoke-TingyePython -Runtime $Python -RuntimeArguments @((Join-Path $tingyeProject 'app\check_runtime.py'))
    if ($tingyeExit -ne 0) {
        $tingyeExit = Invoke-TingyePython -Runtime $Python -RuntimeArguments @('-m','pip','install','--disable-pip-version-check','-r',(Join-Path $tingyeProject 'requirements.txt')) -AppendLog
        if ($tingyeExit -ne 0) { throw 'Could not install local dependencies. Check the internet connection and setup.log.' }
    }
    [IO.File]::WriteAllText($tingyeRuntimeFile, $Python, [Text.UTF8Encoding]::new($false))
    $env:PYTHONIOENCODING = 'utf-8'
    $tingyeArguments = @('-m', 'app.server')
    if ($NoBrowser) { $tingyeArguments += '--no-browser' }
    Start-Process -FilePath $Python -ArgumentList $tingyeArguments -WorkingDirectory $tingyeProject -WindowStyle Hidden -RedirectStandardOutput (Join-Path $tingyeLogs 'server-output.log') -RedirectStandardError (Join-Path $tingyeLogs 'server-error.log') | Out-Null
} catch {
    $_ | Out-String | Add-Content -LiteralPath $tingyeSetupLog
    if ($NoBrowser) {
        Write-Error -Message $_.Exception.Message -ErrorAction Continue
        exit 1
    }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message + [Environment]::NewLine + $tingyeSetupLog, 'Tingye could not start') | Out-Null
    exit 1
}
