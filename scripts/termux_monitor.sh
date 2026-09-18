#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

monitor_base_url="${OPENFOTOS_MONITOR_BASE_URL:-https://balladsoflove.onenodeai.com}"
monitor_base_url="${monitor_base_url%/}"

check_endpoint() {
  local endpoint="$1"
  local expected_body="$2"
  local response_file
  response_file="${TMPDIR:-/tmp}/openfotos-monitor-response.json"
  local status
  status="$(curl --silent --show-error --max-time 15 --output "${response_file}" --write-out '%{http_code}' "${monitor_base_url}${endpoint}")" || return 1
  [[ "${status}" == "200" ]] || return 1
  [[ "$(tr -d '\r\n ' < "${response_file}")" == "${expected_body}" ]]
}

if check_endpoint "/health/live/" '{"status":"ok"}' && \
   check_endpoint "/health/ready/" '{"status":"ok"}'; then
  exit 0
fi

if command -v termux-notification >/dev/null 2>&1; then
  termux-notification \
    --id openfotos-monitor \
    --title "OpenFotos production alert" \
    --content "The live or readiness check failed. Open Railway and Supabase now." \
    --priority high
fi
exit 1
