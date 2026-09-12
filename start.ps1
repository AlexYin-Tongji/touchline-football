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
    $pythonRuntime = (& python -c "import sys; print(sys.executable)").Trim()
    if ($LASTEXITCODE -ne 0) { throw '未找到 Python 3.11+，请先安装 Python。' }
}
& $pythonRuntime -c "import fastapi,uvicorn,httpx,cryptography,bs4" 2>$null
$dependenciesReady = $LASTEXITCODE -eq 0
if ($Setup -or -not $dependenciesReady) {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host '正在准备本地运行环境…'
        & $pythonRuntime -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 环境创建失败。' }
    }
    $pythonRuntime = $venvPython
    & $pythonRuntime -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '运行依赖安装失败，请检查网络后重试。' }
}
$indexPath = Join-Path $PSScriptRoot 'dist\index.html'
$needsBuild = $Setup -or -not (Test-Path -LiteralPath $indexPath)
if (-not $needsBuild) {
    $buildTime = (Get-Item -LiteralPath $indexPath).LastWriteTimeUtc
    $newerSource = Get-ChildItem -LiteralPath 'src' -Recurse -File | Where-Object { $_.LastWriteTimeUtc -gt $buildTime } | Select-Object -First 1
    $needsBuild = $null -ne $newerSource
}
if ($needsBuild) {
    if (-not (Test-Path -LiteralPath 'node_modules')) {
        npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw '页面依赖安装失败。' }
    }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw '页面构建失败，请查看上方错误。' }
}
New-Item -ItemType Directory -Force -Path 'data' | Out-Null
Set-Content -LiteralPath $runtimeRecord -Value $pythonRuntime -Encoding UTF8
if ($NoBrowser) { & $pythonRuntime -X utf8 launch.py --no-browser }
else { & $pythonRuntime -X utf8 launch.py }
if ($LASTEXITCODE -ne 0) { throw '启动未完成，请查看 output 中的服务日志。' }
