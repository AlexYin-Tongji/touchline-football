param([switch]$Setup, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$runtimeRecord = Join-Path $PSScriptRoot 'data\python-path.txt'
$pythonRuntime = $null
if (Test-Path -LiteralPath $venvPython) {
    $pythonRuntime = $venvPython
} elseif (Test-Path -LiteralPath $runtimeRecord) {
    $savedRuntime = (Get-Content -Raw -LiteralPath $runtimeRecord).Trim()
    if (Test-Path -LiteralPath $savedRuntime) { $pythonRuntime = $savedRuntime }
}
if (-not $pythonRuntime) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw '未找到 Python 3.11+，请先安装 Python。' }
    $detectedRuntime = & $pythonCommand.Source -c "import sys; print(sys.executable)"
    if ($LASTEXITCODE -ne 0 -or -not $detectedRuntime) { throw '未找到 Python 3.11+，请先安装 Python。' }
    $pythonRuntime = $detectedRuntime.Trim()
}
& $pythonRuntime -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw '需要 Python 3.11 或更新版本，请更新 Python 后重试。' }
$planArgs = @('-X', 'utf8', '-m', 'server.build_info', '--plan')
if ($Setup) { $planArgs += '--setup' }
$planOutput = & $pythonRuntime @planArgs
if ($LASTEXITCODE -ne 0) { throw '无法检查本地安装状态。' }
$setupPlan = $planOutput | ConvertFrom-Json
& $pythonRuntime -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(name) for name in ('fastapi','uvicorn','httpx','pydantic','cryptography','bs4')) else 1)"
$dependenciesReady = $LASTEXITCODE -eq 0
$needsPython = $Setup -or $setupPlan.python -or -not $dependenciesReady
$needsNode = $Setup -or $setupPlan.node
$needsBuild = $Setup -or $setupPlan.frontend -or $needsNode

# Check before any environment or dist changes. A same-folder old process must
# not silently serve the newly built UI with its previously imported backend.
$preflightArgs = @('-X', 'utf8', 'launch.py', '--preflight')
if ($NoBrowser) { $preflightArgs += '--no-browser' }
if ($needsPython -or $needsNode -or $needsBuild) { $preflightArgs += '--require-stopped' }
& $pythonRuntime @preflightArgs
if ($LASTEXITCODE -ne 0) { throw '请先停止旧服务，再重新启动看台。' }

function Save-SetupStage([string]$stage) {
    & $pythonRuntime -X utf8 -m server.build_info --record $stage
    if ($LASTEXITCODE -ne 0) { throw '无法保存安装状态，下次启动会重新检查。' }
}
function Begin-SetupStage([string]$stage) {
    & $pythonRuntime -X utf8 -m server.build_info --invalidate $stage
    if ($LASTEXITCODE -ne 0) { throw '无法记录本次安装，尚未修改运行环境。' }
}

if ($needsPython) {
    Begin-SetupStage 'python'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host '正在准备本地运行环境…'
        & $pythonRuntime -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 环境创建失败。' }
    }
    $pythonRuntime = $venvPython
    & $pythonRuntime -m pip install -r requirements.txt -c requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw '运行依赖安装失败，请检查网络后重试。' }
    Save-SetupStage 'python'
}
if ($needsNode) {
    Begin-SetupStage 'node'
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw '页面依赖安装失败。' }
    Save-SetupStage 'node'
}
if ($needsBuild) {
    Begin-SetupStage 'frontend'
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw '页面构建失败，请查看上方错误。' }
    Save-SetupStage 'frontend'
}
New-Item -ItemType Directory -Force -Path 'data' | Out-Null
Set-Content -LiteralPath $runtimeRecord -Value $pythonRuntime -Encoding UTF8
if ($NoBrowser) { & $pythonRuntime -X utf8 launch.py --no-browser }
else { & $pythonRuntime -X utf8 launch.py }
if ($LASTEXITCODE -ne 0) { throw '启动未完成，请查看 output 中的服务日志。' }
