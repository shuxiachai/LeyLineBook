param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$appFile = Join-Path $appDir "app.py"
$pythonCandidates = @(
    (Join-Path $appDir ".venv\Scripts\python.exe"),
    (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"),
    "python",
    "py"
)

try {
    foreach ($candidate in $pythonCandidates) {
        $isPath = $candidate.Contains("\")
        if ($isPath -and -not (Test-Path -LiteralPath $candidate)) {
            continue
        }
        if (-not $isPath -and -not (Get-Command $candidate -ErrorAction SilentlyContinue)) {
            continue
        }

        $arguments = @()
        if ($candidate -eq "py") {
            $arguments += "-3"
        }
        $arguments += $appFile
        if ($NoBrowser) {
            $arguments += "--no-browser"
        }

        & $candidate @arguments
        if ($LASTEXITCODE -eq 0) {
            exit 0
        }
    }

    throw "Python 3 was not found or the application could not start."
} catch {
    Write-Host ""
    Write-Host "LeyLineBook failed to start:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}
