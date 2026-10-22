#!/usr/bin/env bash
# Usage: ai-service.sh <service> [setup]
set -euo pipefail
service="$1"
mode="${2:-update}"
case "$service" in mcp|rag|multi-agent) ;; *) exit 2 ;; esac
venv="/opt/tally-venv-$service"
revision_file="/var/lib/tally/deployed-revision-$service"
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
ExecStart=$venv/bin/python server.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
fi

cd /opt/tally/repo
git pull --ff-only origin main
cp -f /opt/tally/repo/scripts/ai-service.sh /usr/local/bin/tally-ai-service 2>/dev/null || true
chmod 755 /usr/local/bin/tally-ai-service 2>/dev/null || true
if [ "$mode" = setup ]; then
    systemctl enable "tally-$service"
    if [ "$service" = rag ] && [ ! -e ai-services/rag-server/chroma ]; then
        ln -s /var/lib/tally/chroma ai-services/rag-server/chroma
    fi
fi
revision=$(git rev-parse HEAD)
previous=$(cat "$revision_file" 2>/dev/null || true)
# Keep the service's lock and database directory writable after recreation.
install -d -o tally -g tally -m 0750 /var/lib/tally/logs
paths=("ai-services/$service-server" scripts/ai-service.sh)
[ "$service" != multi-agent ] || paths+=(prompts/multi_agent)
if [ "$mode" = update ] && [ -d "$venv" ] && [ -n "$previous" ] && git diff --quiet "$previous" "$revision" -- "${paths[@]}" && systemctl is-active --quiet "tally-$service"; then
    exit 0
fi
requirements="ai-services/$service-server/requirements.txt"
if [ ! -d "$venv" ]; then
    python3 -m venv "$venv"
    previous=""
fi
if [ -z "$previous" ] || ! git diff --quiet "$previous" "$revision" -- "$requirements"; then
    "$venv/bin/pip" install -r "$requirements"
    "$venv/bin/pip" check
fi
systemctl restart "tally-$service"
systemctl is-active --quiet "tally-$service"
# Record success only after installation/restart, so rerunning a failed deployment retries it.
printf '%s\n' "$revision" > "$revision_file"
