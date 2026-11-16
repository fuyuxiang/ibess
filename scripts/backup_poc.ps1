#Requires -Version 7
# POC 备份（执行卡 10.2）：只读导出，不回写任何用户数据。
# 数据库用 pg_dump 自定义格式；命名卷（音频、证据对象、媒体运行记录）用只读挂载 tar 打包；最后写清单与 SHA256。
# 用法（项目根，PowerShell 7）：pwsh -File scripts/backup_poc.ps1 [-OutputRoot ..\poc_backups]
[CmdletBinding()]
param(
    [string]$OutputRoot = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'poc_backups'),
    [string]$PostgresContainer = 'poc-postgres'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Invoke-CheckedDocker {
    param([string[]]$DockerArguments)
    & docker @DockerArguments
    if ($LASTEXITCODE -ne 0) { throw "Docker命令失败：$($DockerArguments -join ' ')" }
}

function Read-Setting {
    param([string]$Name, [string]$Default)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value }
    $envFile = Join-Path $root '.env'
    if (Test-Path -LiteralPath $envFile) {
        $line = Get-Content -LiteralPath $envFile -Encoding UTF8 | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -First 1
        if ($line) { return ($line -split '=', 2)[1].Trim() }
    }
    return $Default
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$target = [IO.Path]::GetFullPath((Join-Path $OutputRoot $stamp))   # docker -v 需要绝对路径，相对路径会被当成卷名
New-Item -ItemType Directory -Force -Path $target | Out-Null
$user = Read-Setting 'POSTGRES_USER' 'demo'
$database = Read-Setting 'POSTGRES_DB' 'demo'

Write-Host "备份到 $target"
# 1. 数据库：容器内导出到临时文件再拷出（PowerShell 重定向会改坏二进制）
$inner = "/tmp/poc_backup_$stamp.dump"
Invoke-CheckedDocker -DockerArguments @('exec', $PostgresContainer, 'pg_dump', '-U', $user, '-d', $database, '-Fc', '-f', $inner)
Invoke-CheckedDocker -DockerArguments @('cp', "${PostgresContainer}:$inner", (Join-Path $target "$database.dump"))
Invoke-CheckedDocker -DockerArguments @('exec', $PostgresContainer, 'rm', '-f', $inner)

# 2. 命名卷：只读挂载打包；卷不存在就记跳过，不创建
$volumes = @(
    (Read-Setting 'POC_AUDIO_VOLUME_NAME' 'demo_audio-data'),
    (Read-Setting 'POC_MINIO_VOLUME_NAME' 'demo_miniodata'),
    'poc_media-runs'
)
$skipped = @()
foreach ($volume in $volumes) {
    $found = & docker volume ls -q --filter "name=^$volume$"            # 卷不存在不报错（控制器首次启动前没有 media-runs）
    if ($LASTEXITCODE -ne 0) { throw "Docker命令失败：volume ls $volume" }
    if (-not ($found -contains $volume)) { $skipped += $volume; continue }
    Invoke-CheckedDocker -DockerArguments @('run', '--rm', '-v', "${volume}:/data:ro", '-v', "${target}:/backup", 'alpine:3.20',
        'tar', 'czf', "/backup/$volume.tgz", '-C', '/data', '.')
}

# 3. 清单：文件名、字节数、SHA256，回退前核对
$files = Get-ChildItem -LiteralPath $target -File | ForEach-Object {
    [ordered]@{ name = $_.Name; bytes = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower() }
}
[ordered]@{ created_at = $stamp; database = $database; skipped_volumes = $skipped; files = @($files) } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $target 'manifest.json') -Encoding UTF8
Write-Host "完成：$target（跳过的卷：$($skipped -join ', ')）"
