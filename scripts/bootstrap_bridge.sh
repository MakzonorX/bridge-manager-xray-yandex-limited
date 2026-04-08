#!/usr/bin/env bash
set -euo pipefail

XRAY_VERSION="v26.2.6"
XRAY_BIN="/usr/local/bin/xray"
XRAY_ETC_DIR="/usr/local/etc/xray"
XRAY_SHARE_DIR="/usr/local/share/xray"
XRAY_CONFIG="${XRAY_ETC_DIR}/config.json"

# Default reality profiles (conservative presets, fully overridable via env)
_DEFAULT_REALITY_PROFILE="legacy_x5"
_DEFAULT_REALITY_SERVER_NAME="ads.x5.ru"

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
TC_SERVICE="/etc/systemd/system/bridge-manager-tc.service"

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

  REALITY_PROFILE=legacy_x5        (default: legacy_x5)
  REALITY_SERVER_NAME=ads.x5.ru    (default: from REALITY_PROFILE)
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
  API_ALLOW_FROM=1.2.3.4,1.2.3.0/24 (optional UFW allow-list for 8080 when API_PUBLIC=true)
  DISABLE_IPV6=true|false          (default: true)

Notes:
  - User->Bridge mode is REALITY by default.
  - Bridge->Exit always uses VLESS + XHTTP + TLS.
  - If API_PUBLIC=true and API_ALLOW_FROM is empty, UFW opens 8080/tcp to all.
  - If API_PUBLIC=true and API_ALLOW_FROM is set, UFW opens 8080/tcp only for listed CIDRs/IPs.
  - REALITY_PROFILE presets are optional. Explicit REALITY_SERVER_NAME/REALITY_DEST always win.
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

read_env_file_value() {
  local key="$1"
  if [[ ! -f "${APP_ENV_FILE}" ]]; then
    return 1
  fi
  sed -n "s/^${key}=//p" "${APP_ENV_FILE}" | head -n 1
}

resolve_var_from_existing_or_default() {
  local var_name="$1"
  local env_key="$2"
  local default_value="$3"
  local existing_value

  if [[ "${!var_name+x}" == "x" ]]; then
    return
  fi

  existing_value="$(read_env_file_value "${env_key}" || true)"
  if [[ -n "${existing_value}" ]]; then
    printf -v "${var_name}" '%s' "${existing_value}"
    return
  fi

  printf -v "${var_name}" '%s' "${default_value}"
}

parse_xray_config_value() {
  local jq_expr="$1"
  if [[ ! -f "${XRAY_CONFIG}" ]]; then
    return 1
  fi

  jq -r "${jq_expr}" "${XRAY_CONFIG}" 2>/dev/null | awk 'NF && $0 != "null" { print; exit }'
}

inherit_from_xray_config_if_empty() {
  local var_name="$1"
  local jq_expr="$2"
  local existing_value

  if [[ -n "${!var_name:-}" ]]; then
    return
  fi

  existing_value="$(parse_xray_config_value "${jq_expr}" || true)"
  if [[ -n "${existing_value}" ]]; then
    printf -v "${var_name}" '%s' "${existing_value}"
  fi
}

sync_from_xray_config_unless_explicit() {
  local var_name="$1"
  local jq_expr="$2"
  local explicit_flag="$3"
  local existing_value

  if [[ "${explicit_flag}" == "true" ]]; then
    return
  fi

  existing_value="$(parse_xray_config_value "${jq_expr}" || true)"
  if [[ -n "${existing_value}" ]]; then
    printf -v "${var_name}" '%s' "${existing_value}"
  fi
}

resolve_reality_profile_defaults() {
  local profile="$1"

  case "${profile}" in
    legacy_x5)
      printf '%s\n%s\n' "${_DEFAULT_REALITY_SERVER_NAME}" "${_DEFAULT_REALITY_SERVER_NAME}:443"
      ;;
    max_ru)
      printf '%s\n%s\n' "max.ru" "max.ru:443"
      ;;
    mail_ru)
      printf '%s\n%s\n' "mail.ru" "mail.ru:443"
      ;;
    vk_com)
      printf '%s\n%s\n' "vk.com" "vk.com:443"
      ;;
    *)
      echo "ERROR: unsupported REALITY_PROFILE='${profile}'" >&2
      echo "Supported values: legacy_x5, max_ru, mail_ru, vk_com, auto_ru" >&2
      exit 1
      ;;
  esac
}

select_auto_reality_profile() {
  local seed checksum profiles profile_count index
  seed="${BRIDGE_DOMAIN}"
  checksum="$(printf '%s' "${seed}" | cksum | awk '{print $1}')"
  profiles=("max_ru" "mail_ru" "vk_com")
  profile_count="${#profiles[@]}"
  index=$(( checksum % profile_count ))
  printf '%s\n' "${profiles[${index}]}"
}

apply_reality_profile_defaults() {
  local profile_explicit="$1"
  local server_explicit="$2"
  local dest_explicit="$3"
  local requested_profile effective_profile profile_server_name profile_dest
  local original_server_name original_dest
  local profile_values

  if [[ "${USER_MODE}" != "reality" ]]; then
    REALITY_PROFILE_SOURCE="disabled"
    REALITY_EFFECTIVE_PROFILE=""
    return
  fi

  requested_profile="${REALITY_PROFILE:-${_DEFAULT_REALITY_PROFILE}}"
  effective_profile="${requested_profile}"
  if [[ "${requested_profile}" == "auto_ru" ]]; then
    effective_profile="$(select_auto_reality_profile)"
  fi

  mapfile -t profile_values < <(resolve_reality_profile_defaults "${effective_profile}")
  profile_server_name="${profile_values[0]}"
  profile_dest="${profile_values[1]}"
  original_server_name="${REALITY_SERVER_NAME}"
  original_dest="${REALITY_DEST}"

  if [[ "${server_explicit}" != "true" ]]; then
    if [[ -z "${REALITY_SERVER_NAME}" || "${profile_explicit}" == "true" ]]; then
      REALITY_SERVER_NAME="${profile_server_name}"
    fi
  fi

  if [[ "${dest_explicit}" != "true" ]]; then
    if [[ -z "${REALITY_DEST}" || "${profile_explicit}" == "true" ]]; then
      REALITY_DEST="${profile_dest}"
    fi
  fi

  if [[ -z "${REALITY_DEST}" ]]; then
    REALITY_DEST="${REALITY_SERVER_NAME}:443"
  fi

  if [[ "${server_explicit}" == "true" || "${dest_explicit}" == "true" ]]; then
    REALITY_PROFILE_SOURCE="env-override"
    REALITY_EFFECTIVE_PROFILE="custom"
    return
  fi

  if [[ "${profile_explicit}" != "true" && ( -n "${original_server_name}" || -n "${original_dest}" ) ]]; then
    if [[ "${REALITY_SERVER_NAME}" != "${profile_server_name}" || "${REALITY_DEST}" != "${profile_dest}" ]]; then
      REALITY_PROFILE_SOURCE="existing-config"
      REALITY_EFFECTIVE_PROFILE="custom"
      return
    fi
  fi

  REALITY_EFFECTIVE_PROFILE="${effective_profile}"
  if [[ "${requested_profile}" == "auto_ru" ]]; then
    REALITY_PROFILE_SOURCE="profile:auto_ru->${effective_profile}"
  else
    REALITY_PROFILE_SOURCE="profile:${effective_profile}"
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
  if [[ "${USER_MODE}" == "xhttp" ]]; then
    ufw allow 80/tcp
  fi
  ufw allow "${USER_PORT}/tcp"
  if [[ "${API_PUBLIC}" == "true" ]]; then
    if [[ -n "${API_ALLOW_FROM}" ]]; then
      local cidr
      IFS=',' read -r -a api_cidrs <<< "${API_ALLOW_FROM}"
      for cidr in "${api_cidrs[@]}"; do
        cidr="$(echo "${cidr}" | xargs)"
        [[ -z "${cidr}" ]] && continue
        ufw allow from "${cidr}" to any port 8080 proto tcp
      done
    else
      ufw allow 8080/tcp
    fi
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

  chmod 600 "${XRAY_ETC_DIR}/private.key"
  chmod 644 "${XRAY_ETC_DIR}/fullchain.crt"
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
    local out derived_public_key
    out="$("${XRAY_BIN}" x25519 -i "${REALITY_PRIVATE_KEY}")"
    derived_public_key="$(echo "${out}" | awk -F': ' '/PublicKey|Password/{print $2; exit}')"
    if [[ -n "${REALITY_PUBLIC_KEY}" && "${REALITY_PUBLIC_KEY}" != "${derived_public_key}" ]]; then
      echo "WARN: REALITY_PUBLIC_KEY does not match REALITY_PRIVATE_KEY; overriding with derived public key." >&2
    fi
    REALITY_PUBLIC_KEY="${derived_public_key}"
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
  systemctl enable xray
  systemctl restart xray
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
REALITY_PROFILE=${REALITY_PROFILE}
REALITY_EFFECTIVE_PROFILE=${REALITY_EFFECTIVE_PROFILE}
REALITY_PROFILE_SOURCE=${REALITY_PROFILE_SOURCE}
REALITY_SERVER_NAME=${REALITY_SERVER_NAME}
REALITY_DEST=${REALITY_DEST}
REALITY_PRIVATE_KEY=${REALITY_PRIVATE_KEY}
REALITY_PUBLIC_KEY=${REALITY_PUBLIC_KEY}
REALITY_SHORT_ID=${REALITY_SHORT_ID}
REALITY_FINGERPRINT=${REALITY_FINGERPRINT}
REALITY_SPIDER_X=${REALITY_SPIDER_X}
EXIT_HOST=${EXIT_HOST}
EXIT_PORT=${EXIT_PORT}
EXIT_PATH=${EXIT_PATH}
EXIT_SERVER_NAME=${EXIT_SERVER_NAME}
BRIDGE_UUID_FOR_EXIT=${BRIDGE_UUID_FOR_EXIT}
XRAY_CONFIG=/usr/local/etc/xray/config.json
XRAY_SERVICE=xray
XRAY_API_ADDR=127.0.0.1:10085
XRAY_BIN=/usr/local/bin/xray
API_TOKEN=${API_TOKEN}
API_PUBLIC=${API_PUBLIC}
API_BIND=${api_bind}
API_PORT=8080
API_ALLOW_FROM=${API_ALLOW_FROM}
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

write_tc_service() {
  cat > "${TC_SERVICE}" <<'EOF_TC_SERVICE'
[Unit]
Description=Bridge Manager tc shaping
After=network-online.target xray.service
Wants=network-online.target xray.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/bridge-manager
EnvironmentFile=/etc/bridge-manager/env
ExecStart=/bin/bash -lc '/opt/bridge-manager/scripts/setup_tc.sh'
ExecStop=/bin/bash -lc '/opt/bridge-manager/scripts/setup_tc.sh teardown'

[Install]
WantedBy=multi-user.target
EOF_TC_SERVICE
}

activate_app() {
  systemctl daemon-reload
  systemctl enable --now bridge-manager
}

activate_tc_service() {
  systemctl daemon-reload

  if [[ "${LIMITED_TC_ENABLED}" == "true" ]]; then
    systemctl enable --now bridge-manager-tc
    return
  fi

  systemctl disable --now bridge-manager-tc 2>/dev/null || true
}

final_checks() {
  systemctl is-active xray
  systemctl is-active bridge-manager
  if [[ "${LIMITED_TC_ENABLED}" == "true" ]]; then
    systemctl is-active bridge-manager-tc
  fi
  ss -lntp | egrep "(:${USER_PORT}|:1080|:10085|:8080)\\s"
  timeout 3 bash -c "echo > /dev/tcp/${EXIT_HOST}/${EXIT_PORT}"
  curl -fsS --retry 6 --retry-connrefused --retry-delay 1 http://127.0.0.1:8080/healthz >/dev/null
}

main() {
  local reality_profile_explicit="false"
  local reality_server_name_explicit="false"
  local reality_dest_explicit="false"
  local reality_short_id_explicit="false"
  local reality_private_key_explicit="false"
  local reality_public_key_explicit="false"
  local exit_host_explicit="false"
  local exit_port_explicit="false"
  local exit_path_explicit="false"
  local exit_server_name_explicit="false"
  local bridge_uuid_for_exit_explicit="false"

  if [[ "${REALITY_PROFILE+x}" == "x" ]]; then
    reality_profile_explicit="true"
  fi
  if [[ "${REALITY_SERVER_NAME+x}" == "x" ]]; then
    reality_server_name_explicit="true"
  fi
  if [[ "${REALITY_DEST+x}" == "x" ]]; then
    reality_dest_explicit="true"
  fi
  if [[ "${REALITY_SHORT_ID+x}" == "x" ]]; then
    reality_short_id_explicit="true"
  fi
  if [[ "${REALITY_PRIVATE_KEY+x}" == "x" ]]; then
    reality_private_key_explicit="true"
  fi
  if [[ "${REALITY_PUBLIC_KEY+x}" == "x" ]]; then
    reality_public_key_explicit="true"
  fi
  if [[ "${EXIT_HOST+x}" == "x" ]]; then
    exit_host_explicit="true"
  fi
  if [[ "${EXIT_PORT+x}" == "x" ]]; then
    exit_port_explicit="true"
  fi
  if [[ "${EXIT_PATH+x}" == "x" ]]; then
    exit_path_explicit="true"
  fi
  if [[ "${EXIT_SERVER_NAME+x}" == "x" ]]; then
    exit_server_name_explicit="true"
  fi
  if [[ "${BRIDGE_UUID_FOR_EXIT+x}" == "x" ]]; then
    bridge_uuid_for_exit_explicit="true"
  fi

  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  resolve_var_from_existing_or_default API_PUBLIC API_PUBLIC false
  resolve_var_from_existing_or_default API_ALLOW_FROM API_ALLOW_FROM ""
  resolve_var_from_existing_or_default DISABLE_IPV6 DISABLE_IPV6 true

  resolve_var_from_existing_or_default USER_MODE USER_TRANSPORT_MODE reality
  resolve_var_from_existing_or_default USER_PORT USER_PORT 443
  resolve_var_from_existing_or_default USER_PATH USER_PATH /user-xh
  resolve_var_from_existing_or_default USER_FLOW USER_FLOW xtls-rprx-vision
  resolve_var_from_existing_or_default USER_HOST_FOR_URI USER_HOST_FOR_URI ""

  resolve_var_from_existing_or_default REALITY_PROFILE REALITY_PROFILE "${_DEFAULT_REALITY_PROFILE}"
  resolve_var_from_existing_or_default REALITY_SERVER_NAME REALITY_SERVER_NAME ""
  resolve_var_from_existing_or_default REALITY_DEST REALITY_DEST ""
  resolve_var_from_existing_or_default REALITY_SHORT_ID REALITY_SHORT_ID ""
  resolve_var_from_existing_or_default REALITY_PRIVATE_KEY REALITY_PRIVATE_KEY ""
  resolve_var_from_existing_or_default REALITY_PUBLIC_KEY REALITY_PUBLIC_KEY ""
  resolve_var_from_existing_or_default REALITY_FINGERPRINT REALITY_FINGERPRINT chrome
  resolve_var_from_existing_or_default REALITY_SPIDER_X REALITY_SPIDER_X /
  resolve_var_from_existing_or_default REALITY_PROFILE_SOURCE REALITY_PROFILE_SOURCE ""
  resolve_var_from_existing_or_default REALITY_EFFECTIVE_PROFILE REALITY_EFFECTIVE_PROFILE ""

  resolve_var_from_existing_or_default EXIT_HOST EXIT_HOST "${_DEFAULT_EXIT_HOST}"
  resolve_var_from_existing_or_default EXIT_PORT EXIT_PORT "${_DEFAULT_EXIT_PORT}"
  resolve_var_from_existing_or_default EXIT_PATH EXIT_PATH "${_DEFAULT_EXIT_PATH}"
  resolve_var_from_existing_or_default EXIT_SERVER_NAME EXIT_SERVER_NAME ""
  resolve_var_from_existing_or_default BRIDGE_UUID_FOR_EXIT BRIDGE_UUID_FOR_EXIT "${_DEFAULT_BRIDGE_UUID_FOR_EXIT}"

  resolve_var_from_existing_or_default LIMITED_THROTTLE_RATE_BYTES_PER_SEC LIMITED_THROTTLE_RATE_BYTES_PER_SEC 102400
  resolve_var_from_existing_or_default LIMITED_TC_ENABLED LIMITED_TC_ENABLED true
  resolve_var_from_existing_or_default LIMITED_TC_EGRESS_IFACE LIMITED_TC_EGRESS_IFACE ""
  resolve_var_from_existing_or_default LIMITED_TC_MARK LIMITED_TC_MARK 100
  resolve_var_from_existing_or_default LIMITED_TC_CLASS_ID LIMITED_TC_CLASS_ID 1:10
  resolve_var_from_existing_or_default LIMIT_POLL_INTERVAL_SECONDS LIMIT_POLL_INTERVAL_SECONDS 15

  if [[ -z "${EXIT_SERVER_NAME}" ]]; then
    EXIT_SERVER_NAME="${EXIT_HOST}"
  fi

  if [[ "${USER_MODE}" != "reality" && "${USER_MODE}" != "xhttp" ]]; then
    echo "ERROR: USER_MODE must be 'reality' or 'xhttp'" >&2
    exit 1
  fi

  require_root
  require_vars

  setup_packages
  setup_time_and_sysctl
  sync_from_xray_config_unless_explicit REALITY_PRIVATE_KEY '.inbounds[] | select(.tag == "inbound-from-users") | .streamSettings.realitySettings.privateKey // empty' "${reality_private_key_explicit}"
  sync_from_xray_config_unless_explicit REALITY_SHORT_ID '.inbounds[] | select(.tag == "inbound-from-users") | .streamSettings.realitySettings.shortIds[0] // empty' "${reality_short_id_explicit}"
  sync_from_xray_config_unless_explicit REALITY_SERVER_NAME '.inbounds[] | select(.tag == "inbound-from-users") | .streamSettings.realitySettings.serverNames[0] // empty' "${reality_server_name_explicit}"
  sync_from_xray_config_unless_explicit REALITY_DEST '.inbounds[] | select(.tag == "inbound-from-users") | .streamSettings.realitySettings.dest // empty' "${reality_dest_explicit}"
  sync_from_xray_config_unless_explicit EXIT_HOST '.outbounds[] | select(.tag == "to-exit") | .settings.vnext[0].address // empty' "${exit_host_explicit}"
  sync_from_xray_config_unless_explicit EXIT_PORT '.outbounds[] | select(.tag == "to-exit") | .settings.vnext[0].port // empty' "${exit_port_explicit}"
  sync_from_xray_config_unless_explicit EXIT_PATH '.outbounds[] | select(.tag == "to-exit") | .streamSettings.xhttpSettings.path // empty' "${exit_path_explicit}"
  sync_from_xray_config_unless_explicit EXIT_SERVER_NAME '.outbounds[] | select(.tag == "to-exit") | .streamSettings.tlsSettings.serverName // empty' "${exit_server_name_explicit}"
  sync_from_xray_config_unless_explicit BRIDGE_UUID_FOR_EXIT '.outbounds[] | select(.tag == "to-exit") | .settings.vnext[0].users[0].id // empty' "${bridge_uuid_for_exit_explicit}"
  apply_reality_profile_defaults "${reality_profile_explicit}" "${reality_server_name_explicit}" "${reality_dest_explicit}"
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
  write_tc_service
  activate_app
  activate_tc_service
  final_checks

  echo "DONE: bridge bootstrap completed."
  echo "Health: curl -s http://127.0.0.1:8080/health | jq ."
  echo "Healthz: curl -s http://127.0.0.1:8080/healthz"
  echo "Diagnostics: curl -s -H 'Authorization: Bearer <API_TOKEN>' http://127.0.0.1:8080/v1/system/diagnostics | jq ."
  if [[ "${USER_MODE}" == "reality" ]]; then
    echo "REALITY profile: ${REALITY_PROFILE} (effective: ${REALITY_EFFECTIVE_PROFILE}, source: ${REALITY_PROFILE_SOURCE})"
    echo "REALITY server name: ${REALITY_SERVER_NAME}"
    echo "REALITY dest: ${REALITY_DEST}"
    echo "REALITY public key: ${REALITY_PUBLIC_KEY}"
    echo "REALITY short id: ${REALITY_SHORT_ID}"
  fi
}

main "$@"
