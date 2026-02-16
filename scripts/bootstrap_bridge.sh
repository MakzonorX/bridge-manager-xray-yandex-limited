#!/usr/bin/env bash
set -euo pipefail

XRAY_VERSION="v26.2.6"
XRAY_BIN="/usr/local/bin/xray"
XRAY_ETC_DIR="/usr/local/etc/xray"
XRAY_SHARE_DIR="/usr/local/share/xray"
XRAY_CONFIG="${XRAY_ETC_DIR}/config.json"

EXIT_HOST="s1.bytestand.fun"
EXIT_PORT="443"
EXIT_PATH="/bridge-xh"
BRIDGE_UUID_FOR_EXIT="7d28c9a1-e5f3-4b90-8a2f-d3e4b7c9f8a0"
EXIT_SERVER_NAME="s1.bytestand.fun"

APP_DST="/opt/bridge-manager"
APP_ENV_FILE="/etc/bridge-manager/env"
APP_SERVICE="/etc/systemd/system/bridge-manager.service"
XRAY_SERVICE="/etc/systemd/system/xray.service"

usage() {
  cat <<'EOF'
Usage:
  sudo BRIDGE_DOMAIN=... ACME_EMAIL=... API_TOKEN=... ./scripts/bootstrap_bridge.sh

Optional env:
  API_PUBLIC=false|true   (default: false)
  DISABLE_IPV6=true|false (default: true)

Notes:
  - Script configures bridge->exit chain and Bridge Manager API.
  - If API_PUBLIC=true, UFW opens 8080/tcp.
EOF
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 1
  fi
}

require_vars() {
  : "${BRIDGE_DOMAIN:?BRIDGE_DOMAIN is required}"
  : "${ACME_EMAIL:?ACME_EMAIL is required}"
  : "${API_TOKEN:?API_TOKEN is required}"
}

setup_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y curl socat cron unzip openssl dnsutils net-tools jq git ufw ca-certificates python3 python3-venv python3-pip rsync
}

setup_time_and_sysctl() {
  timedatectl set-ntp true || true
  systemctl enable --now systemd-timesyncd || true

  if [[ "${DISABLE_IPV6}" == "true" ]]; then
    cat > /etc/sysctl.d/99-disable-ipv6.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
    sysctl --system >/dev/null
  fi
}

setup_ufw() {
  ufw allow 22/tcp
  ufw allow 80/tcp
  ufw allow 443/tcp
  if [[ "${API_PUBLIC}" == "true" ]]; then
    ufw allow 8080/tcp
  fi
  ufw --force enable
}

install_xray() {
  local tmpd zip_path
  tmpd="$(mktemp -d)"
  zip_path="${tmpd}/xray.zip"

  curl -fL "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip" -o "${zip_path}"
  unzip -o "${zip_path}" -d "${tmpd}"

  install -m 755 "${tmpd}/xray" "${XRAY_BIN}"
  mkdir -p "${XRAY_SHARE_DIR}" "${XRAY_ETC_DIR}"
  [[ -f "${tmpd}/geoip.dat" ]] && install -m 644 "${tmpd}/geoip.dat" "${XRAY_SHARE_DIR}/geoip.dat"
  [[ -f "${tmpd}/geosite.dat" ]] && install -m 644 "${tmpd}/geosite.dat" "${XRAY_SHARE_DIR}/geosite.dat"
}

write_xray_service() {
  cat > "${XRAY_SERVICE}" <<'EOF'
[Unit]
Description=Xray Service
After=network.target nss-lookup.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json
Restart=on-failure
RestartSec=2
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
}

issue_cert() {
  systemctl stop nginx 2>/dev/null || true
  systemctl stop apache2 2>/dev/null || true

  if [[ ! -x /root/.acme.sh/acme.sh ]]; then
    curl -fsSL https://get.acme.sh | sh -s email="${ACME_EMAIL}"
  fi

  /root/.acme.sh/acme.sh --set-default-ca --server letsencrypt
  /root/.acme.sh/acme.sh --issue -d "${BRIDGE_DOMAIN}" --standalone --keylength ec-256
  /root/.acme.sh/acme.sh --install-cert -d "${BRIDGE_DOMAIN}" --ecc \
    --key-file "${XRAY_ETC_DIR}/private.key" \
    --fullchain-file "${XRAY_ETC_DIR}/fullchain.crt" \
    --reloadcmd "systemctl restart xray"

  chmod 644 "${XRAY_ETC_DIR}/private.key" "${XRAY_ETC_DIR}/fullchain.crt"
}

write_xray_config() {
  cat > "${XRAY_CONFIG}" <<EOF
{
  "log": {
    "loglevel": "warning"
  },
  "stats": {},
  "api": {
    "tag": "api",
    "services": [
      "HandlerService",
      "StatsService",
      "LoggerService",
      "RoutingService"
    ]
  },
  "policy": {
    "levels": {
      "0": {
        "statsUserUplink": true,
        "statsUserDownlink": true
      }
    },
    "system": {
      "statsInboundUplink": true,
      "statsInboundDownlink": true
    }
  },
  "inbounds": [
    {
      "listen": "127.0.0.1",
      "port": 10085,
      "protocol": "dokodemo-door",
      "tag": "api",
      "settings": {
        "address": "127.0.0.1"
      }
    },
    {
      "listen": "0.0.0.0",
      "port": 443,
      "protocol": "vless",
      "tag": "inbound-from-users",
      "settings": {
        "decryption": "none",
        "clients": []
      },
      "streamSettings": {
        "network": "xhttp",
        "security": "tls",
        "xhttpSettings": {
          "path": "/user-xh"
        },
        "tlsSettings": {
          "certificates": [
            {
              "certificateFile": "/usr/local/etc/xray/fullchain.crt",
              "keyFile": "/usr/local/etc/xray/private.key"
            }
          ]
        }
      }
    },
    {
      "listen": "127.0.0.1",
      "port": 1080,
      "protocol": "socks",
      "tag": "socks-test",
      "settings": {
        "auth": "noauth",
        "udp": false
      }
    }
  ],
  "outbounds": [
    {
      "protocol": "vless",
      "tag": "to-exit",
      "settings": {
        "vnext": [
          {
            "address": "${EXIT_HOST}",
            "port": ${EXIT_PORT},
            "users": [
              {
                "id": "${BRIDGE_UUID_FOR_EXIT}",
                "encryption": "none"
              }
            ]
          }
        ]
      },
      "streamSettings": {
        "network": "xhttp",
        "security": "tls",
        "xhttpSettings": {
          "path": "${EXIT_PATH}"
        },
        "tlsSettings": {
          "serverName": "${EXIT_SERVER_NAME}"
        }
      }
    },
    {
      "protocol": "freedom",
      "tag": "direct",
      "settings": {}
    },
    {
      "protocol": "blackhole",
      "tag": "blocked",
      "settings": {}
    }
  ],
  "routing": {
    "rules": [
      {
        "type": "field",
        "inboundTag": [
          "api"
        ],
        "outboundTag": "api"
      },
      {
        "type": "field",
        "inboundTag": [
          "inbound-from-users"
        ],
        "outboundTag": "to-exit"
      },
      {
        "type": "field",
        "inboundTag": [
          "socks-test"
        ],
        "outboundTag": "to-exit"
      }
    ]
  }
}
EOF
}

activate_xray() {
  "${XRAY_BIN}" run -test -config "${XRAY_CONFIG}"
  systemctl daemon-reload
  systemctl enable --now xray
}

sync_app_code() {
  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  mkdir -p "${APP_DST}"

  if [[ "${repo_root}" != "${APP_DST}" ]]; then
    rsync -a --delete \
      --exclude ".git" \
      --exclude ".venv" \
      --exclude "__pycache__" \
      --exclude "data" \
      "${repo_root}/" "${APP_DST}/"
  fi
}

install_app_venv() {
  python3 -m venv "${APP_DST}/.venv"
  "${APP_DST}/.venv/bin/pip" install --upgrade pip
  "${APP_DST}/.venv/bin/pip" install -r "${APP_DST}/requirements.txt"
}

write_app_env() {
  local api_bind
  api_bind="127.0.0.1"
  if [[ "${API_PUBLIC}" == "true" ]]; then
    api_bind="0.0.0.0"
  fi

  mkdir -p "$(dirname "${APP_ENV_FILE}")" "${APP_DST}/data"
  cat > "${APP_ENV_FILE}" <<EOF
BRIDGE_DOMAIN=${BRIDGE_DOMAIN}
USER_PORT=443
USER_PATH=/user-xh
XRAY_CONFIG=/usr/local/etc/xray/config.json
XRAY_SERVICE=xray
XRAY_API_ADDR=127.0.0.1:10085
XRAY_BIN=/usr/local/bin/xray
API_TOKEN=${API_TOKEN}
API_BIND=${api_bind}
API_PORT=8080
DB_PATH=/opt/bridge-manager/data/bridge_manager.db
EOF
  chmod 600 "${APP_ENV_FILE}"
}

write_app_service() {
  cat > "${APP_SERVICE}" <<'EOF'
[Unit]
Description=Bridge Manager API
After=network.target xray.service
Wants=xray.service

[Service]
Type=simple
WorkingDirectory=/opt/bridge-manager
EnvironmentFile=/etc/bridge-manager/env
ExecStart=/bin/bash -lc '/opt/bridge-manager/.venv/bin/uvicorn app.main:app --host "${API_BIND}" --port "${API_PORT}"'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
}

activate_app() {
  systemctl daemon-reload
  systemctl enable --now bridge-manager
}

final_checks() {
  systemctl is-active xray
  systemctl is-active bridge-manager
  ss -lntp | egrep '(:443|:1080|:10085|:8080)\s'
  timeout 3 bash -c "echo > /dev/tcp/${EXIT_HOST}/${EXIT_PORT}"
}

main() {
  API_PUBLIC="${API_PUBLIC:-false}"
  DISABLE_IPV6="${DISABLE_IPV6:-true}"

  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  require_root
  require_vars

  setup_packages
  setup_time_and_sysctl
  setup_ufw
  install_xray
  write_xray_service
  issue_cert
  write_xray_config
  activate_xray

  sync_app_code
  install_app_venv
  write_app_env
  write_app_service
  activate_app
  final_checks

  echo "DONE: bridge bootstrap completed."
  echo "Health: curl -s http://127.0.0.1:8080/health | jq ."
}

main "$@"

