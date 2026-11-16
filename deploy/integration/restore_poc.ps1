#Requires -Version 7
# POC 回退（执行卡 10.2）：从 scripts/backup_poc.ps1 的备份恢复数据库。
# 默认恢复到一个新库 <库名>_restore_<时间>，不碰现库；确认无误后再把服务的连接切过去。
# 覆盖现库必须显式加 -OverwriteCurrentDatabase，且会再要求键入库名确认。命名卷不自动恢复（避免覆盖现有音频 / 证据），步骤见 README。
# 用法（项目根，PowerShell 7）：pwsh -File deploy/integration/restore_poc.ps1 -BackupDir ..\poc_backups\20261019T120000Z [-OverwriteCurrentDatabase]
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupDir,
    [string]$PostgresContainer = 'poc-postgres',
    [switch]$OverwriteCurrentDatabase
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Invoke-CheckedDocker {
    param([string[]]$DockerArguments)
    & docker @DockerArguments
    if ($LASTEXITCODE -ne 0) { throw "Docker命令失败：$($DockerArguments -join ' ')" }
}

$manifestPath = Join-Path $BackupDir 'manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "备份清单不存在：$manifestPath" }
$manifest = Get-Content -LiteralPath $manifestPath -Encoding UTF8 -Raw | ConvertFrom-Json
$database = $manifest.database
$dump = Join-Path $BackupDir "$database.dump"
$expected = ($manifest.files | Where-Object { $_.name -eq "$database.dump" }).sha256
$actual = (Get-FileHash -LiteralPath $dump -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) { throw "备份文件摘要与清单不符，拒绝恢复：$dump" }

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

$user = Read-Setting 'POSTGRES_USER' 'demo'
if ($OverwriteCurrentDatabase) {
    $current = Read-Setting 'POSTGRES_DB' 'demo'
    if ($current -ne $database) { throw "备份来自库 $database，当前 .env 指向 $current；覆盖模式只允许恢复同名库" }
    Write-Warning '覆盖前请确认已用 scripts/backup_poc.ps1 备份过现库（本脚本不自动再备份）'
    $typed = Read-Host "将用备份覆盖现库 $database（--clean）。确认请键入库名"
    if ($typed -ne $database) { throw '确认不符，已取消' }
    $targetDb = $database
    $restoreArgs = @('--clean', '--if-exists')
} else {
    $targetDb = "${database}_restore_$($manifest.created_at.ToLower())"
    Invoke-CheckedDocker -DockerArguments @('exec', $PostgresContainer, 'createdb', '-U', $user, $targetDb)
    $restoreArgs = @()
}

$inner = "/tmp/poc_restore_$($manifest.created_at).dump"
Invoke-CheckedDocker -DockerArguments @('cp', $dump, "${PostgresContainer}:$inner")
Invoke-CheckedDocker -DockerArguments (@('exec', $PostgresContainer, 'pg_restore', '-U', $user, '-d', $targetDb, '--no-owner') + $restoreArgs + @($inner))
Invoke-CheckedDocker -DockerArguments @('exec', $PostgresContainer, 'rm', '-f', $inner)
Write-Host "已恢复到库 $targetDb。核对后如需切换，把 .env 的 POSTGRES_DB 改为该库名并重启应用服务；原库未改动。"
