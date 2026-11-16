# 统一启动/恢复入口，不删除数据卷。默认只启动平台及必要基础服务。
[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$Restart,
    [switch]$Workers,
    [switch]$VideoWorker,
    [ValidateRange(30, 600)][int]$WaitTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$taskPocRoot = Split-Path -Parent $PSScriptRoot

function Invoke-CheckedDocker {
    param([string[]]$DockerArguments)
    & docker @DockerArguments
    if ($LASTEXITCODE -ne 0) { throw "Docker命令失败：$($DockerArguments -join ' ')" }
}

function Wait-PocUrl {
    param([string]$Url)
    $deadline = [DateTime]::UtcNow.AddSeconds($WaitTimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -TimeoutSec 12
            if ($response.StatusCode -eq 200) { return }
        } catch { }
        if ([DateTime]::UtcNow -ge $deadline) { throw "等待服务超时：$Url" }
        Start-Sleep -Seconds 2
    } while ($true)
}

Push-Location $taskPocRoot
try {
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        if (-not (Test-Path -LiteralPath $desktop)) { throw 'Docker未就绪，请先启动Docker服务' }
        Write-Host '正在启动Docker Desktop…'
        Start-Process -FilePath $desktop -WindowStyle Hidden
        $deadline = [DateTime]::UtcNow.AddSeconds($WaitTimeoutSeconds)
        do {
            Start-Sleep -Seconds 3
            & docker info *> $null
            if ($LASTEXITCODE -eq 0) { break }
            if ([DateTime]::UtcNow -ge $deadline) { throw 'Docker引擎启动超时' }
        } while ($true)
    }

    if ($Build) {
        Push-Location (Join-Path $taskPocRoot 'web-console')
        try {
            & npm run build
            if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
            Invoke-CheckedDocker -DockerArguments @('build', '-f', 'Dockerfile.local', '-t', 'poc-web-console', '.')
        } finally { Pop-Location }
        $buildTargets = @('api')
        if ($Workers) { $buildTargets += @('event-service', 'media-service', 'audio-worker') }
        if ($VideoWorker) { $buildTargets += 'vision-worker' }
        Invoke-CheckedDocker -DockerArguments (@('compose', 'build') + $buildTargets)
    }

    Write-Host '等待数据库、Redis、MinIO及流媒体服务…'
    Invoke-CheckedDocker -DockerArguments @('compose', 'up', '-d', '--no-build', '--wait', '--wait-timeout', "$WaitTimeoutSeconds", 'postgres', 'redis', 'minio', 'mediamtx')
    Write-Host '启动API并等待依赖及数据库初始化…'
    Invoke-CheckedDocker -DockerArguments @('compose', 'up', '-d', '--no-build', 'api')
    if ($Restart) {
        Invoke-CheckedDocker -DockerArguments @('compose', 'restart', 'api')
    }
    Invoke-CheckedDocker -DockerArguments @('compose', 'up', '-d', '--no-build', '--wait', '--wait-timeout', "$WaitTimeoutSeconds", 'api')
    Invoke-CheckedDocker -DockerArguments @('compose', 'up', '-d', '--no-build', 'web-console')
    if ($Restart) { Invoke-CheckedDocker -DockerArguments @('compose', 'restart', 'web-console') }
    Invoke-CheckedDocker -DockerArguments @('compose', 'up', '-d', '--no-build', '--wait', '--wait-timeout', "$WaitTimeoutSeconds", 'web-console')

    $workerTargets = @()
    if ($Workers) { $workerTargets += @('event-service', 'media-service', 'audio-worker') }
    if ($VideoWorker) { $workerTargets += 'vision-worker' }
    if ($workerTargets.Count -gt 0) {
        Invoke-CheckedDocker -DockerArguments (@('compose', 'up', '-d', '--no-build') + $workerTargets)
    }
    Wait-PocUrl -Url 'http://localhost:3000/health/ready'
    Wait-PocUrl -Url 'http://localhost:3000/api/auth/status'
    Invoke-CheckedDocker -DockerArguments @('compose', 'ps', 'api', 'web-console', 'postgres', 'redis', 'minio')
    Write-Host '平台就绪：http://localhost:3000/stages'
    Write-Host 'API就绪：http://localhost:8000/health/ready'
    if ($VideoWorker) { Write-Host '视频worker已启动；输入流有效性另看采集日志，不等同于视频检测就绪。' }
} catch {
    Write-Host '启动未通过，以下为容器状态。不会删除或重置已有数据。'
    & docker compose ps -a
    throw
} finally { Pop-Location }
