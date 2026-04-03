#!/usr/bin/env bash
# Idempotent tc shaping setup for throttled-lane traffic.
# Uses fwmark-based classification to rate-limit packets from Xray's
# throttle outbound (marked with SO_MARK).
#
# Usage: sudo ./scripts/setup_tc.sh [teardown]
#
# Env vars (all optional, sane defaults provided):
#   LIMITED_TC_EGRESS_IFACE  - egress interface (auto-detected if empty)
#   LIMITED_TC_MARK          - fwmark value (default: 100)
#   LIMITED_TC_CLASS_ID      - tc class id (default: 1:10)
#   LIMITED_THROTTLE_RATE_BYTES_PER_SEC - rate limit (default: 102400 = 100 KB/s)

set -euo pipefail

LIMITED_TC_MARK="${LIMITED_TC_MARK:-100}"
LIMITED_TC_CLASS_ID="${LIMITED_TC_CLASS_ID:-1:10}"
LIMITED_THROTTLE_RATE_BYTES_PER_SEC="${LIMITED_THROTTLE_RATE_BYTES_PER_SEC:-102400}"

detect_egress_iface() {
  if [[ -n "${LIMITED_TC_EGRESS_IFACE:-}" ]]; then
    echo "${LIMITED_TC_EGRESS_IFACE}"
    return
  fi
  local iface
  iface="$(ip route show default | awk '/default/{print $5; exit}')"
  if [[ -z "${iface}" ]]; then
    echo "ERROR: cannot detect egress interface" >&2
    exit 1
  fi
  echo "${iface}"
}

teardown_tc() {
  local iface="$1"
  # Remove existing htb qdisc if present (ignore errors)
  tc qdisc del dev "${iface}" root 2>/dev/null || true
  echo "tc: cleared qdisc on ${iface}"
}

setup_tc() {
  local iface="$1"
  local rate_bytes="$2"
  local mark="$3"
  local class_id="$4"

  # Convert bytes/sec to kbit/s for tc (1 byte = 8 bits, 1 kbit = 1000 bits)
  local rate_kbit=$(( (rate_bytes * 8 + 999) / 1000 ))
  if [[ "${rate_kbit}" -lt 8 ]]; then
    rate_kbit=8
  fi

  # Burst: at least 10 KB or rate/10
  local burst_bytes=$(( rate_bytes / 10 ))
  if [[ "${burst_bytes}" -lt 10240 ]]; then
    burst_bytes=10240
  fi

  # Teardown first for idempotency
  teardown_tc "${iface}"

  # Root HTB qdisc
  tc qdisc add dev "${iface}" root handle 1: htb default 99

  # Default class: unlimited
  tc class add dev "${iface}" parent 1: classid 1:99 htb rate 10gbit

  # Throttled class
  tc class add dev "${iface}" parent 1: classid "${class_id}" htb rate "${rate_kbit}kbit" burst "${burst_bytes}"

  # Match packets with fwmark -> throttled class
  tc filter add dev "${iface}" parent 1:0 protocol ip prio 1 handle "${mark}" fw flowid "${class_id}"

  echo "tc: configured on ${iface}: mark=${mark} -> class=${class_id} rate=${rate_kbit}kbit (${rate_bytes} B/s) burst=${burst_bytes}b"
}

main() {
  local iface
  iface="$(detect_egress_iface)"

  if [[ "${1:-}" == "teardown" ]]; then
    teardown_tc "${iface}"
    exit 0
  fi

  setup_tc "${iface}" "${LIMITED_THROTTLE_RATE_BYTES_PER_SEC}" "${LIMITED_TC_MARK}" "${LIMITED_TC_CLASS_ID}"
}

main "$@"
