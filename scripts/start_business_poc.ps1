# 兼容旧构建入口；启动和健康等待统一交给start_poc.ps1。
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start_poc.ps1') -Build
