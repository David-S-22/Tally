#!/usr/bin/env bash
# Usage: ai-service.sh <service> [setup]
set -euo pipefail
service="$1"
mode="${2:-update}"
case "$service" in mcp|rag|multi-agent) ;; *) exit 2 ;; esac
if [ "$mode" = setup ]; then
    cloud-init status --wait
    id tally >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/tally tally
    mkdir -p /opt/tally /var/lib/tally/chroma /var/lib/tally/logs
    chown -R tally:tally /var/lib/tally
    if [ ! -d /opt/tally/repo/.git ]; then
        git clone --branch main --single-branch https://github.com/David-S-22/ASD-Spring-2026.git /opt/tally/repo
    fi
    cat > "/etc/systemd/system/tally-$service.service" <<UNIT
[Unit]
Description=Tally $service
After=network-online.target
Wants=network-online.target
[Service]
User=tally
WorkingDirectory=/opt/tally/repo/ai-services/$service-server
EnvironmentFile=/etc/tally.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/tally-venv/bin/python server.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
fi

cd /opt/tally/repo
git pull --ff-only origin main
if [ "$mode" = setup ]; then
    install -m 755 scripts/ai-service.sh /usr/local/bin/tally-ai-service
    systemctl enable "tally-$service"
    if [ "$service" = rag ] && [ ! -e ai-services/rag-server/chroma ]; then
        ln -s /var/lib/tally/chroma ai-services/rag-server/chroma
    fi
fi
revision=$(git rev-parse HEAD)
previous=$(cat /var/lib/tally/deployed-revision 2>/dev/null || true)
paths=("ai-services/$service-server" scripts/ai-service.sh)
[ "$service" != multi-agent ] || paths+=(prompts/multi_agent)
if [ "$mode" = update ] && [ -d /opt/tally-venv ] && [ -n "$previous" ] && git diff --quiet "$previous" "$revision" -- "${paths[@]}"; then
    exit 0
fi
requirements="ai-services/$service-server/requirements.txt"
if [ ! -d /opt/tally-venv ]; then
    python3 -m venv /opt/tally-venv
    previous=""
fi
if [ -z "$previous" ] || ! git diff --quiet "$previous" "$revision" -- "$requirements"; then
    /opt/tally-venv/bin/pip install -r "$requirements"
    /opt/tally-venv/bin/pip check
fi
systemctl restart "tally-$service"
systemctl is-active --quiet "tally-$service"
# Record success only after installation/restart, so rerunning a failed deployment retries it.
printf '%s\n' "$revision" > /var/lib/tally/deployed-revision
