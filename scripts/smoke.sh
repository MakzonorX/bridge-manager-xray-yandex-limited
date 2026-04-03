#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:8080}"
API_TOKEN="${API_TOKEN:?API_TOKEN is required}"
TEST_USER_ID="${TEST_USER_ID:-smoke-user}"

echo "[1/8] health"
curl -sS "${API_BASE}/health" | jq .

echo "[2/8] create user"
curl -sS -X POST "${API_BASE}/v1/users" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"${TEST_USER_ID}\",\"label\":\"Smoke User\"}" | jq .

echo "[3/8] get user"
curl -sS "${API_BASE}/v1/users/${TEST_USER_ID}" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq .

echo "[4/8] traffic"
curl -sS "${API_BASE}/v1/users/${TEST_USER_ID}/traffic" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq .

echo "[5/8] get limit-policy (default unlimited)"
curl -sS "${API_BASE}/v1/users/${TEST_USER_ID}/limit-policy" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq .

echo "[6/8] set limit-policy (limited + throttle)"
curl -sS -X PUT "${API_BASE}/v1/users/${TEST_USER_ID}/limit-policy" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"mode":"limited","traffic_limit_bytes":10737418240,"post_limit_action":"throttle","throttle_rate_bytes_per_sec":102400}' | jq .

echo "[7/8] get limit-policy (verify)"
curl -sS "${API_BASE}/v1/users/${TEST_USER_ID}/limit-policy" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq .

echo "[8/8] delete user"
curl -sS -X DELETE "${API_BASE}/v1/users/${TEST_USER_ID}" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq .
