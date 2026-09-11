#!/usr/bin/env bash
#
# Render and install the systemd units for wherever this checkout actually
# lives. The install path and user are decided once, here, instead of being
# hardcoded into seven unit files -- a unit pointing at /opt while the code
# sits in /var fails with systemd's famously unhelpful "unavailable resources
# or another system error".
#
# Usage:
#   sudo ./scripts/install-systemd.sh                  # infer path and user
#   sudo INSTALL_DIR=/var/py/grid RUN_USER=gridbot ./scripts/install-systemd.sh
#   ./scripts/install-systemd.sh --print               # dry run, nothing written
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TIMER="$ROOT/deploy/systemd/mini-grid-market-data.timer"
UNIT_DIR=/etc/systemd/system

PRINT_ONLY=false
[ "${1:-}" = "--print" ] && PRINT_ONLY=true

# Default to this checkout, owned by whoever owns it. BSD and GNU stat differ.
owner_of() {
    stat -c %U "$1" 2>/dev/null || stat -f %Su "$1" 2>/dev/null || echo root
}

INSTALL_DIR="${INSTALL_DIR:-$ROOT}"
RUN_USER="${RUN_USER:-$(owner_of "$INSTALL_DIR")}"
RUN_GROUP="${RUN_GROUP:-$RUN_USER}"

# ProtectHome hides /home and /root from the service. A sane default right up
# until the code itself lives there, which is when it stops being one.
case "$INSTALL_DIR" in
    /home/*|/root/*|/Users/*) PROTECT_HOME=false ;;
    *) PROTECT_HOME=true ;;
esac

PYTHON="$INSTALL_DIR/.venv/bin/python"
UVICORN="$INSTALL_DIR/.venv/bin/uvicorn"

# The API's port is load-bearing: something in front of it (nginx, a tunnel)
# is already pointed at a specific one, and regenerating the unit with a
# different default silently breaks that with a 502 nobody connects to this
# script. So an installed unit's port wins unless asked otherwise.
API_HOST="${API_HOST:-127.0.0.1}"
if [ -z "${API_PORT:-}" ]; then
    # `|| true`: on a host with nothing installed yet these files do not
    # exist, and pipefail would otherwise abort the whole script here.
    API_PORT="$( { cat "$UNIT_DIR/mini-grid-api.service" \
                       "$UNIT_DIR/mini-grid-api.service.d/"*.conf 2>/dev/null || true; } \
                 | sed -n 's/.*--port[= ]\([0-9]\{1,\}\).*/\1/p' | tail -1 )"
    if [ -n "$API_PORT" ]; then
        KEPT_PORT=yes
    else
        API_PORT=8000
    fi
fi

# name|type|after extra|restart|restartsec|install|description|command
# An empty restart field means no Restart= line at all, which is what a
# oneshot job wants. The worker orders itself after the API on purpose.
SERVICES=$(cat <<EOF
api|simple||on-failure|3|yes|Mini Grid Bot FastAPI|$UVICORN app.main:app --host $API_HOST --port $API_PORT
worker|simple| mini-grid-api.service|always|3|yes|Mini Grid Bot Worker|$PYTHON -m app.workers.grid
dex-worker|simple||always|5|yes|Mini Grid Bot DEX worker|$PYTHON -m app.workers.dex
dex-sampler|simple||always|5|yes|Mini Grid Bot DEX price sampler|$PYTHON -m app.workers.dex_sampler
fomo-registry|simple||always|5|yes|Mini Grid Bot FOMO trader registry|$PYTHON -m app.workers.fomo_registry
chain-tape|simple||always|5|yes|Mini Grid Bot Robinhood Chain trade tape|$PYTHON -m app.workers.chain_tape
market-data|oneshot|||| no|Mini Grid Bot daily market data collector|$PYTHON -m app.workers.market_data
EOF
)

render() {
    local type="$1" after_extra="$2" restart="$3" restart_sec="$4" install="$5" description="$6" exec_line="$7"
    cat <<UNIT
[Unit]
Description=$description
After=network-online.target postgresql.service$after_extra
Wants=network-online.target
# A missing setting is not something a restart fixes: without this a
# misconfigured service restarts every RestartSec forever, burning CPU and
# burying the one useful line in thousands of identical tracebacks.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=$type
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=-$INSTALL_DIR/.env
ExecStart=$exec_line
UNIT
    [ -n "$restart" ] && printf 'Restart=%s\nRestartSec=%s\n' "$restart" "$restart_sec"
    cat <<UNIT

# Basic hardening. The app only needs network and its working directory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=$PROTECT_HOME
ReadWritePaths=$INSTALL_DIR
UNIT
    # market-data is started by its timer, so it must not also want to start
    # at boot on its own.
    [ "$install" = "yes" ] && printf '\n[Install]\nWantedBy=multi-user.target\n'
    return 0
}

echo "install dir : $INSTALL_DIR"
echo "run as      : $RUN_USER:$RUN_GROUP"
echo "ProtectHome : $PROTECT_HOME"
echo "API         : $API_HOST:$API_PORT${KEPT_PORT:+  (kept from the installed unit)}"
echo

[ -x "$PYTHON" ] || echo "warning: $PYTHON not found -- run 'make install' in $INSTALL_DIR first" >&2
[ -f "$INSTALL_DIR/.env" ] || echo "warning: $INSTALL_DIR/.env not found -- every unit reads it via EnvironmentFile" >&2

if [ "$PRINT_ONLY" = false ] && [ "$(id -u)" -ne 0 ]; then
    echo "error: writing to $UNIT_DIR needs root; re-run with sudo (or use --print)" >&2
    exit 1
fi

while IFS='|' read -r name type after_extra restart restart_sec install description exec_line; do
    [ -z "$name" ] && continue
    install="$(echo "$install" | tr -d ' ')"
    unit="mini-grid-$name.service"
    if [ "$PRINT_ONLY" = true ]; then
        echo "----- $unit -----"
        render "$type" "$after_extra" "$restart" "$restart_sec" "$install" "$description" "$exec_line"
        echo
    else
        render "$type" "$after_extra" "$restart" "$restart_sec" "$install" "$description" "$exec_line" > "$UNIT_DIR/$unit"
        echo "wrote $UNIT_DIR/$unit"
    fi
done <<< "$SERVICES"

if [ "$PRINT_ONLY" = true ]; then
    echo "----- mini-grid-market-data.timer (copied as-is) -----"
    cat "$TIMER"
    exit 0
fi

cp "$TIMER" "$UNIT_DIR/"
echo "wrote $UNIT_DIR/$(basename "$TIMER")"
systemctl daemon-reload
echo
echo "done. Enable what this host should run, for example:"
echo "  systemctl enable --now mini-grid-api mini-grid-chain-tape"
echo "  systemctl enable --now mini-grid-market-data.timer"
echo "Units already running need an explicit restart to pick up the new files."
