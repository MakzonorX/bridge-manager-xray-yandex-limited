#!/usr/bin/env bash
set -euo pipefail

XRAY_VERSION="v26.2.6"
XRAY_BIN="/usr/local/bin/xray"
XRAY_ETC_DIR="/usr/local/etc/xray"
XRAY_SHARE_DIR="/usr/local/share/xray"
XRAY_CONFIG="${XRAY_ETC_DIR}/config.json"

# Default exit-node constants (overridable via env)
_DEFAULT_EXIT_HOST="s1.bytestand.fun"
_DEFAULT_EXIT_PORT="443"
_DEFAULT_EXIT_PATH="/bridge-xh"
_DEFAULT_BRIDGE_UUID_FOR_EXIT="7d28c9a1-e5f3-4b90-8a2f-d3e4b7c9f8a0"
_DEFAULT_EXIT_SERVER_NAME="s1.bytestand.fun"

APP_DST="/opt/bridge-manager"
APP_ENV_FILE="/etc/bridge-manager/env"
APP_SERVICE="/etc/systemd/system/bridge-manager.service"
XRAY_SERVICE="/etc/systemd/system/xray.service"

usage() {
  cat <<'EOF_USAGE'
Usage:
  sudo BRIDGE_DOMAIN=... API_TOKEN=... ./scripts/bootstrap_bridge.sh

Required env:
  BRIDGE_DOMAIN
  API_TOKEN

Required only in xhttp mode:
  ACME_EMAIL

Optional env (User->Bridge inbound):
  USER_MODE=reality|xhttp          (default: reality)
  USER_PORT=443                    (default: 443)
  USER_PATH=/user-xh               (default: /user-xh, xhttp mode only)
  USER_FLOW=xtls-rprx-vision       (default: xtls-rprx-vision)
  USER_HOST_FOR_URI=<host-or-ip>   (override host in generated vless:// links)

  REALITY_SERVER_NAME=ads.x5.ru    (default: ads.x5.ru)
  REALITY_DEST=ads.x5.ru:443       (default: REALITY_SERVER_NAME:443)
  REALITY_SHORT_ID=<hex>           (default: random hex)
  REALITY_PRIVATE_KEY=<x25519 private key>
  REALITY_PUBLIC_KEY=<x25519 public key, auto if omitted>
  REALITY_FINGERPRINT=chrome       (default: chrome)
  REALITY_SPIDER_X=/               (default: /)

Optional env (Bridge->Exit outbound):
  EXIT_HOST=s1.bytestand.fun       (default: s1.bytestand.fun)
  EXIT_PORT=443                    (default: 443)
  EXIT_PATH=/bridge-xh             (default: /bridge-xh)
  EXIT_SERVER_NAME=s1.bytestand.fun (default: same as EXIT_HOST)
  BRIDGE_UUID_FOR_EXIT=<uuid>      (default: built-in uuid)

Optional env (API / system):
  API_PUBLIC=false|true            (default: false)
  DISABLE_IPV6=true|false          (default: true)

Notes:
  - User->Bridge mode is REALITY by default.
  - Bridge->Exit always uses VLESS + XHTTP + TLS.
  - If API_PUBLIC=true, UFW opens 8080/tcp.
EOF_USAGE
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 1
  fi
}

require_vars() {
  : "${BRIDGE_DOMAIN:?BRIDGE_DOMAIN is required}"
  : "${API_TOKEN:?API_TOKEN is required}"

  if [[ "${USER_MODE}" == "xhttp" ]]; then
    : "${ACME_EMAIL:?ACME_EMAIL is required for USER_MODE=xhttp}"
  fi
}

setup_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y curl socat cron unzip openssl dnsutils net-tools jq git ufw ca-certificates python3 python3-venv python3-pip rsync iproute2
}

setup_time_and_sysctl() {
  timedatectl set-ntp true || true
  systemctl enable --now systemd-timesyncd || true

  if [[ "${DISABLE_IPV6}" == "true" ]]; then
    cat > /etc/sysctl.d/99-disable-ipv6.conf <<'EOF_IPV6'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF_IPV6
    sysctl --system >/dev/null
  fi
}

setup_ufw() {
  ufw allow 22/tcp
  ufw allow 80/tcp
  ufw allow "${USER_PORT}/tcp"
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
  cat > "${XRAY_SERVICE}" <<'EOF_SERVICE'
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
EOF_SERVICE
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

prepare_reality_materials() {
  if [[ "${USER_MODE}" != "reality" ]]; then
    return
  fi

  if [[ -z "${REALITY_SHORT_ID}" ]]; then
    REALITY_SHORT_ID="$(openssl rand -hex 8)"
  fi

  if [[ -z "${REALITY_PRIVATE_KEY}" ]]; then
    local out
    out="$("${XRAY_BIN}" x25519)"
    REALITY_PRIVATE_KEY="$(echo "${out}" | awk -F': ' '/PrivateKey/{print $2; exit}')"
    REALITY_PUBLIC_KEY="$(echo "${out}" | awk -F': ' '/PublicKey|Password/{print $2; exit}')"
  else
    local out
    out="$("${XRAY_BIN}" x25519 -i "${REALITY_PRIVATE_KEY}")"
    if [[ -z "${REALITY_PUBLIC_KEY}" ]]; then
      REALITY_PUBLIC_KEY="$(echo "${out}" | awk -F': ' '/PublicKey|Password/{print $2; exit}')"
    fi
  fi

  if [[ -z "${REALITY_PRIVATE_KEY}" || -z "${REALITY_PUBLIC_KEY}" ]]; then
    echo "ERROR: failed to prepare REALITY key pair" >&2
    exit 1
  fi
}

write_xray_config() {
  if [[ "${USER_MODE}" == "reality" ]]; then
    cat > "${XRAY_CONFIG}" <<EOF_REALITY
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
      "port": ${USER_PORT},
      "protocol": "vless",
      "tag": "inbound-from-users",
      "settings": {
        "decryption": "none",
        "clients": []
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "${REALITY_DEST}",
          "xver": 0,
          "serverNames": ["${REALITY_SERVER_NAME}"],
          "privateKey": "${REALITY_PRIVATE_KEY}",
          "shortIds": ["${REALITY_SHORT_ID}"]
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
EOF_REALITY
    return
  fi

  cat > "${XRAY_CONFIG}" <<EOF_XHTTP
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
      "port": ${USER_PORT},
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
          "path": "${USER_PATH}"
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
EOF_XHTTP
}

activate_xray() {
  "${XRAY_BIN}" run -test -config "${XRAY_CONFIG}"
  systemctl daemon-reload
  systemctl enable --now xray
}

setup_traffic_shaping() {
  if [[ "${LIMITED_TC_ENABLED}" != "true" ]]; then
    echo "tc: traffic shaping disabled (LIMITED_TC_ENABLED!=true)"
    return
  fi

  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

  LIMITED_TC_EGRESS_IFACE="${LIMITED_TC_EGRESS_IFACE}" \
  LIMITED_TC_MARK="${LIMITED_TC_MARK}" \
  LIMITED_TC_CLASS_ID="${LIMITED_TC_CLASS_ID}" \
  LIMITED_THROTTLE_RATE_BYTES_PER_SEC="${LIMITED_THROTTLE_RATE_BYTES_PER_SEC}" \
    bash "${repo_root}/scripts/setup_tc.sh"
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
  local api_bind user_host_for_uri user_security user_network
  api_bind="127.0.0.1"
  if [[ "${API_PUBLIC}" == "true" ]]; then
    api_bind="0.0.0.0"
  fi

  user_host_for_uri="${USER_HOST_FOR_URI:-${BRIDGE_DOMAIN}}"
  user_security="tls"
  user_network="xhttp"
  if [[ "${USER_MODE}" == "reality" ]]; then
    user_security="reality"
    user_network="tcp"
  fi

  mkdir -p "$(dirname "${APP_ENV_FILE}")" "${APP_DST}/data"
  cat > "${APP_ENV_FILE}" <<EOF_ENV
BRIDGE_DOMAIN=${BRIDGE_DOMAIN}
USER_PORT=${USER_PORT}
USER_HOST_FOR_URI=${user_host_for_uri}
USER_PATH=${USER_PATH}
USER_TRANSPORT_MODE=${USER_MODE}
USER_NETWORK=${user_network}
USER_SECURITY=${user_security}
USER_FLOW=${USER_FLOW}
REALITY_SERVER_NAME=${REALITY_SERVER_NAME}
REALITY_PUBLIC_KEY=${REALITY_PUBLIC_KEY}
REALITY_SHORT_ID=${REALITY_SHORT_ID}
REALITY_FINGERPRINT=${REALITY_FINGERPRINT}
REALITY_SPIDER_X=${REALITY_SPIDER_X}
XRAY_CONFIG=/usr/local/etc/xray/config.json
XRAY_SERVICE=xray
XRAY_API_ADDR=127.0.0.1:10085
XRAY_BIN=/usr/local/bin/xray
API_TOKEN=${API_TOKEN}
API_BIND=${api_bind}
API_PORT=8080
DB_PATH=/opt/bridge-manager/data/bridge_manager.db
LIMITED_THROTTLE_RATE_BYTES_PER_SEC=${LIMITED_THROTTLE_RATE_BYTES_PER_SEC}
LIMITED_TC_ENABLED=${LIMITED_TC_ENABLED}
LIMITED_TC_EGRESS_IFACE=${LIMITED_TC_EGRESS_IFACE}
LIMITED_TC_MARK=${LIMITED_TC_MARK}
LIMITED_TC_CLASS_ID=${LIMITED_TC_CLASS_ID}
LIMIT_POLL_INTERVAL_SECONDS=${LIMIT_POLL_INTERVAL_SECONDS}
EOF_ENV
  chmod 600 "${APP_ENV_FILE}"
}

write_app_service() {
  cat > "${APP_SERVICE}" <<'EOF_APP_SERVICE'
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
EOF_APP_SERVICE
}

activate_app() {
  systemctl daemon-reload
  systemctl enable --now bridge-manager
}

final_checks() {
  systemctl is-active xray
  systemctl is-active bridge-manager
  ss -lntp | egrep "(:${USER_PORT}|:1080|:10085|:8080)\\s"
  timeout 3 bash -c "echo > /dev/tcp/${EXIT_HOST}/${EXIT_PORT}"
}

main() {
  API_PUBLIC="${API_PUBLIC:-false}"
  DISABLE_IPV6="${DISABLE_IPV6:-true}"

  USER_MODE="${USER_MODE:-reality}"
  USER_PORT="${USER_PORT:-443}"
  USER_PATH="${USER_PATH:-/user-xh}"
  USER_FLOW="${USER_FLOW:-xtls-rprx-vision}"
  USER_HOST_FOR_URI="${USER_HOST_FOR_URI:-}"

  REALITY_SERVER_NAME="${REALITY_SERVER_NAME:-ads.x5.ru}"
  REALITY_DEST="${REALITY_DEST:-${REALITY_SERVER_NAME}:443}"
  REALITY_SHORT_ID="${REALITY_SHORT_ID:-}"
  REALITY_PRIVATE_KEY="${REALITY_PRIVATE_KEY:-}"
  REALITY_PUBLIC_KEY="${REALITY_PUBLIC_KEY:-}"
  REALITY_FINGERPRINT="${REALITY_FINGERPRINT:-chrome}"
  REALITY_SPIDER_X="${REALITY_SPIDER_X:-/}"

  EXIT_HOST="${EXIT_HOST:-${_DEFAULT_EXIT_HOST}}"
  EXIT_PORT="${EXIT_PORT:-${_DEFAULT_EXIT_PORT}}"
  EXIT_PATH="${EXIT_PATH:-${_DEFAULT_EXIT_PATH}}"
  EXIT_SERVER_NAME="${EXIT_SERVER_NAME:-${EXIT_HOST}}"
  BRIDGE_UUID_FOR_EXIT="${BRIDGE_UUID_FOR_EXIT:-${_DEFAULT_BRIDGE_UUID_FOR_EXIT}}"

  LIMITED_THROTTLE_RATE_BYTES_PER_SEC="${LIMITED_THROTTLE_RATE_BYTES_PER_SEC:-102400}"
  LIMITED_TC_ENABLED="${LIMITED_TC_ENABLED:-true}"
  LIMITED_TC_EGRESS_IFACE="${LIMITED_TC_EGRESS_IFACE:-}"
  LIMITED_TC_MARK="${LIMITED_TC_MARK:-100}"
  LIMITED_TC_CLASS_ID="${LIMITED_TC_CLASS_ID:-1:10}"
  LIMIT_POLL_INTERVAL_SECONDS="${LIMIT_POLL_INTERVAL_SECONDS:-15}"

  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  if [[ "${USER_MODE}" != "reality" && "${USER_MODE}" != "xhttp" ]]; then
    echo "ERROR: USER_MODE must be 'reality' or 'xhttp'" >&2
    exit 1
  fi

  require_root
  require_vars

  setup_packages
  setup_time_and_sysctl
  setup_ufw
  install_xray
  write_xray_service

  if [[ "${USER_MODE}" == "xhttp" ]]; then
    issue_cert
  fi

  prepare_reality_materials
  write_xray_config
  activate_xray

  setup_traffic_shaping

  sync_app_code
  install_app_venv
  write_app_env
  write_app_service
  activate_app
  final_checks

  echo "DONE: bridge bootstrap completed."
  echo "Health: curl -s http://127.0.0.1:8080/health | jq ."
  if [[ "${USER_MODE}" == "reality" ]]; then
    echo "REALITY public key: ${REALITY_PUBLIC_KEY}"
    echo "REALITY short id: ${REALITY_SHORT_ID}"
  fi
}

main "$@"
