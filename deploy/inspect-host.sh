#!/bin/sh
# Read-only inspection for the user-designated deployment host.
# Does not install packages, edit configuration, restart services or expose environment variables.
set -eu

printf '\nSystem\n'
uname -srm
if [ -r /etc/os-release ]; then
  sed -n '/^ID=/p; /^VERSION_ID=/p; /^PRETTY_NAME=/p' /etc/os-release
fi

printf '\nCapacity\n'
df -h / /opt 2>/dev/null || df -h /
if command -v free >/dev/null 2>&1; then free -m; fi

printf '\nAvailable runtimes\n'
for tool in docker podman dotnet curl tar openssl; do
  if command -v "$tool" >/dev/null 2>&1; then command -v "$tool"; fi
done
if command -v docker >/dev/null 2>&1; then
  docker --version
  docker compose version 2>/dev/null || true
  docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null || true
fi
if command -v dotnet >/dev/null 2>&1; then dotnet --list-runtimes; fi

printf '\nListening TCP ports\n'
if command -v ss >/dev/null 2>&1; then
  ss -lnt
elif command -v netstat >/dev/null 2>&1; then
  netstat -lnt
fi

printf '\nExisting service state\n'
if command -v systemctl >/dev/null 2>&1; then
  for service in nginx apache2 httpd caddy docker; do
    printf '%s: ' "$service"
    systemctl is-active "$service" 2>/dev/null || true
  done
fi

printf '\nExisting project installation\n'
if [ -d /opt/codex-usage-monitor ]; then
  ls -ld /opt/codex-usage-monitor
else
  printf 'No /opt/codex-usage-monitor directory\n'
fi
