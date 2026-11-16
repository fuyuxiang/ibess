#!/usr/bin/env bash
# 读取受信任的本机 .env；已有环境变量优先，不执行文件里的 shell 代码。
load_poc_env() {
  local env_path="${1:?需要 .env 路径}" env_key env_value
  [ -f "$env_path" ] || return 0
  while IFS='=' read -r env_key env_value || [ -n "$env_key" ]; do
    env_value="${env_value%$'\r'}"
    [[ "$env_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    case "$env_value" in
      \"*\") env_value="${env_value:1:${#env_value}-2}" ;;
      \'*\') env_value="${env_value:1:${#env_value}-2}" ;;
    esac
    if [ "${!env_key+x}" != x ]; then
      export "$env_key=$env_value"
    fi
  done < "$env_path"
}
