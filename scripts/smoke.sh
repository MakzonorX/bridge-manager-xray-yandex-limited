#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:8080}"
API_TOKEN="${API_TOKEN:?API_TOKEN is required}"
TEST_USER_ID="${TEST_USER_ID:-smoke-user}"

echo "[1/5] health"
curl -sS "${API_BASE}/health" | jq .

echo "[2/5] create user"
curl -sS -X POST "${API_BASE}/v1/users" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"${TEST_USER_ID}\",\"label\":\"Smoke User\"}" | jq .

echo "[3/5] get user"
curl -sS "${API_BASE}/v1/users/${TEST_USER_ID}" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq .

echo "[4/5] traffic"
curl -sS "${API_BASE}/v1/users/${TEST_USER_ID}/traffic" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq .

echo "[5/5] delete user"
curl -sS -X DELETE "${API_BASE}/v1/users/${TEST_USER_ID}" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq .
