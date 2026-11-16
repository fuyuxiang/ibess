#Requires -Version 7
# 负责人 A 的可选服务启动（执行卡 10.2；5.01 瘦身）。B 的启动方式照旧（start-demo.sh），本脚本在它之后按需执行。
# 5.01 起 A 的接线已进 B 的服务代码，本脚本只做三件可选的事：api 挂素材根（4.1）、阶段媒体控制器（4.2）、视觉离线质检 worker（9.4）。
# 不删数据卷、不重置数据库、不改端口；失败只打印状态，不做清理。
# 用法（项目根，PowerShell 7）：pwsh -File deploy/integration/start_a.ps1 [-AssetsMount] [-Media] [-Offline] [-Build]
[CmdletBinding()]
param(
    [switch]$AssetsMount,    # 以叠加配置重建 api 容器，只读挂上素材根（仍是 B 的 api 镜像）
    [switch]$Media,          # 启动阶段媒体控制器（需要 MEDIA_SESSION_ID / MEDIA_SOURCE_ID）
    [switch]$Offline,        # 启动视觉离线质检 worker（需要 GPU 与检测权重）
    [switch]$Build,          # 构建 A 的新增镜像（控制器、离线 worker）
    [ValidateRange(30, 600)][int]$WaitTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$compose = @('compose', '-f', 'docker-compose.yml', '-f', 'deploy/integration/docker-compose.a.yml')

function Invoke-CheckedDocker {
    param([string[]]$DockerArguments)
    & docker @DockerArguments
    if ($LASTEXITCODE -ne 0) { throw "Docker命令失败：$($DockerArguments -join ' ')" }
}

function Read-DotEnv {
    $values = @{}
    $path = Join-Path $root '.env'
    if (Test-Path -LiteralPath $path) {
        foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $values[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $values
}

function Get-Setting {
    param([hashtable]$DotEnv, [string]$Name, [string]$Default = '')
    $fromProcess = [Environment]::GetEnvironmentVariable($Name)
    if ($fromProcess) { return $fromProcess }
    if ($DotEnv.ContainsKey($Name) -and $DotEnv[$Name]) { return $DotEnv[$Name] }
    return $Default
}

Push-Location $root
try {
    $dotEnv = Read-DotEnv
    $api = & docker ps --filter 'name=^poc-api$' --format '{{.Names}}'
    if (-not $api) { throw '平台未就绪：poc-api 没在运行，先按 B 的方式启动（bash start-demo.sh）' }

    # 1. 素材根：api、控制器、离线 worker 共用，只读
    if ($AssetsMount -or $Media -or $Offline) {
        $assetsDir = Get-Setting $dotEnv 'POC_STAGE_ASSETS_DIR' (Join-Path $root 'stage_assets')
        if (-not (Test-Path -LiteralPath $assetsDir -PathType Container)) {
            throw "素材根未就绪：$assetsDir 不存在（在 .env 设 POC_STAGE_ASSETS_DIR）"
        }
    }
    if ($AssetsMount) {
        Write-Host '以叠加配置重建 api（仍是 B 的镜像，只追加素材根只读挂载）…'
        Invoke-CheckedDocker -DockerArguments ($compose + @('up', '-d', '--no-build', '--wait', '--wait-timeout', "$WaitTimeoutSeconds", 'api'))
    }

    # 2. 阶段媒体控制器：推独立的 stage 路径，不碰 B 的 cam1～9；场次与源必须显式给
    if ($Media) {
        $publisher = & docker ps --filter 'name=^poc-publisher$' --format '{{.Names}}'
        if ($publisher) {
            throw '旧推流容器 poc-publisher 正在运行（scripts/start_all.sh 启动），它推 cam1；先停掉它，或确认 RTSP_PUBLISH_URL 不是 cam1'
        }
        if (-not (Get-Setting $dotEnv 'MEDIA_SESSION_ID') -or -not (Get-Setting $dotEnv 'MEDIA_SOURCE_ID')) {
            throw '阶段媒体控制器未就绪：.env 缺 MEDIA_SESSION_ID / MEDIA_SOURCE_ID（按真实场次填写，不给默认值）'
        }
        $publishUrl = Get-Setting $dotEnv 'RTSP_PUBLISH_URL' 'rtsp://mediamtx:8554/stage'
        if ($publishUrl -match '/cam[1-9]$') {
            throw "阶段媒体控制器未就绪：RTSP_PUBLISH_URL=$publishUrl 与 B 的 poc-pub-cam 推流同路径，会互相顶掉"
        }
        if (-not (Get-Setting $dotEnv 'MEDIA_CONTROL_TOKEN')) {
            Write-Warning '未配置 MEDIA_CONTROL_TOKEN：控制器照常推流但不回 ACK（ack_disabled）；要回 ACK 须在 B 的 config/phase-consumers.json 登记 media-controller'
        }
        if ($Build) { Invoke-CheckedDocker -DockerArguments ($compose + @('--profile', 'media', 'build', 'stage-media-controller')) }
        Invoke-CheckedDocker -DockerArguments ($compose + @('--profile', 'media', 'up', '-d', '--no-build', 'stage-media-controller'))
    }

    # 3. 视觉离线质检 worker：模型权重缺失时显式报未就绪，不启动
    if ($Offline) {
        $weights = Get-Setting $dotEnv 'OFFLINE_DETECTOR_HOST_WEIGHTS' (Join-Path (Split-Path -Parent $root) 'dev_assets/yolov8n.pt')
        if (-not (Test-Path -LiteralPath $weights -PathType Leaf)) {
            Write-Warning "离线质检未就绪：检测权重 $weights 不存在，offline-worker 不启动（任务会停在 queued，不会被当成已质检）"
        } else {
            if ($Build) { Invoke-CheckedDocker -DockerArguments ($compose + @('--profile', 'offline', 'build', 'offline-worker')) }
            Invoke-CheckedDocker -DockerArguments ($compose + @('--profile', 'offline', 'up', '-d', '--no-build', 'offline-worker'))
        }
    }

    Invoke-CheckedDocker -DockerArguments ($compose + @('--profile', 'media', '--profile', 'offline', 'ps'))
    Write-Host 'A 侧可选服务已按参数启动。A 的表由 api 启动时建（create_all），不需要单独迁移。'
} catch {
    Write-Host 'A 侧启动未通过，以下为容器状态。不会删除或重置已有数据。'
    & docker @compose --profile media --profile offline ps -a
    throw
} finally { Pop-Location }
