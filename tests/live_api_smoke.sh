#!/usr/bin/env bash
set -euo pipefail

# Temporary live smoke test.
# Prompts for credentials, executes the real Vattenfall login flow,
# and verifies that consumption API returns HTTP 200.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

read -r -p "Customer ID: " customer_id
read -r -s -p "Password: " password
echo
read -r -p "Metering point ID: " metering_point_id
read -r -p "Subscription key: " subscription_key

if [[ -z "$customer_id" || -z "$password" || -z "$metering_point_id" || -z "$subscription_key" ]]; then
  echo "ERROR: customerId, password, metering_point_id and subscription_key are required" >&2
  exit 1
fi

auth_start_url="https://services.vattenfalleldistribution.se/auth/login?returnUrl=https%3a%2f%2fwww.vattenfalleldistribution.se%2flogga-in%2f"
common_auth_url="https://accounts.vattenfall.com/iamng/seb2c/dso/commonauth"

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
cookiejar="$work_dir/cookies.txt"
headers="$work_dir/headers.txt"
body="$work_dir/body.txt"

extract_location() {
  grep -i '^location:' "$1" | head -n1 | sed -E 's/^[Ll]ocation:[[:space:]]*//; s/\r$//' || true
}

extract_authorize_from_html() {
  # Fallback: find accounts authorize URL in HTML response body.
  grep -oE 'https://accounts\.vattenfall\.com[^"'"'"'[:space:]]+/oauth2/authorize[^"'"'"'[:space:]]*' "$1" | head -n1 || true
}

# Step 1: init auth
curl -sS -D "$headers" -o "$body" -c "$cookiejar" "$auth_start_url"
location_1="$(extract_location "$headers")"
if [[ -z "$location_1" ]]; then
  location_1="$(extract_authorize_from_html "$body")"
fi
if [[ -z "$location_1" ]]; then
  echo "ERROR: Missing redirect location at step 1 and no authorize URL in body" >&2
  sed -n '1,20p' "$headers" >&2 || true
  sed -n '1,30p' "$body" >&2 || true
  exit 1
fi

# Step 2: open authorize page
curl -sS -D "$headers" -o /dev/null -b "$cookiejar" -c "$cookiejar" "$location_1"

session_data_key="$(awk '$6 ~ /^sessionNonceCookie-/ {k=$6; sub(/^sessionNonceCookie-/, "", k); print k; exit}' "$cookiejar")"
if [[ -z "$session_data_key" ]]; then
  echo "ERROR: Missing sessionNonceCookie-* in cookie jar" >&2
  exit 1
fi

# Step 3: authenticate
curl -sS -D "$headers" -o /dev/null -b "$cookiejar" -c "$cookiejar" \
  -H 'content-type: application/x-www-form-urlencoded' \
  --data "customerId=$customer_id&password=$password&auth_method=customerid_password&tenantDomain=se.b2c&sessionDataKey=$session_data_key" \
  "$common_auth_url"
location_2="$(extract_location "$headers")"
if [[ -z "$location_2" ]]; then
  echo "ERROR: Missing redirect location at step 3" >&2
  exit 1
fi

# Step 4: OAuth authorize continuation
curl -sS -D "$headers" -o /dev/null -b "$cookiejar" -c "$cookiejar" "$location_2"
location_3="$(extract_location "$headers")"
if [[ -z "$location_3" ]]; then
  echo "ERROR: Missing redirect location at step 4" >&2
  exit 1
fi

# Step 5: callback sets VF cookies
curl -sS -D "$headers" -o /dev/null -b "$cookiejar" -c "$cookiejar" "$location_3"

has_security_cookie="$(awk '$6=="VF_SecurityCookie"{print "yes"; exit}' "$cookiejar")"
has_access_cookie="$(awk '$6=="VF_AccessCookie"{print "yes"; exit}' "$cookiejar")"
if [[ "$has_security_cookie" != "yes" || "$has_access_cookie" != "yes" ]]; then
  echo "ERROR: Missing VF auth cookies after callback" >&2
  exit 1
fi

start_date="$(date -u -v-7d +%Y-%m-%d 2>/dev/null || date -u -d '7 days ago' +%Y-%m-%d)"
end_date="$(date -u +%Y-%m-%d)"
consumption_url="https://services.vattenfalleldistribution.se/consumption/consumption/$metering_point_id/$start_date/$end_date/Daily/Measured"

http_code="$(curl -sS -o "$body" -w '%{http_code}' -b "$cookiejar" \
  -H 'accept: application/json, text/plain, */*' \
  -H "ocp-apim-subscription-key: $subscription_key" \
  -H 'origin: https://www.vattenfalleldistribution.se' \
  -H 'referer: https://www.vattenfalleldistribution.se/' \
  "$consumption_url")"

if [[ "$http_code" != "200" ]]; then
  echo "ERROR: Consumption API returned HTTP $http_code" >&2
  sed -n '1,5p' "$body" >&2 || true
  exit 1
fi

if [[ ! -s "$body" ]]; then
  echo "ERROR: Consumption API returned empty body" >&2
  exit 1
fi

metrics="$(
  python3 - "$body" "$metering_point_id" "$start_date" "$end_date" <<'PY'
import json
import sys

body_path, metering_point_id, start_date, end_date = sys.argv[1:5]
payload = json.load(open(body_path, "r", encoding="utf-8"))

def flatten_points(obj):
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for key in ("consumption", "data", "items", "values", "result", "results", "timeSeries", "timeSeriesValues"):
            val = obj.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        out = []
        for val in obj.values():
            if isinstance(val, dict):
                out.extend(flatten_points(val))
            elif isinstance(val, list):
                out.extend([x for x in val if isinstance(x, dict)])
        return out
    return []

def pick(item, *keys):
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None

points = []
for item in flatten_points(payload):
    d = pick(item, "date", "Date", "period", "Period", "from", "From")
    v = pick(item, "value", "Value", "consumption", "Consumption", "quantity", "Quantity")
    try:
        if d is not None and v is not None:
            points.append((str(d), float(v)))
    except (TypeError, ValueError):
        pass

points.sort(key=lambda p: p[0])
if not points:
    print("ERROR: API response had no parsable points")
    sys.exit(2)

values = [v for _, v in points]
latest_date, latest_value = points[-1]
print(f"Metering point: {metering_point_id}")
print(f"Range: {start_date} -> {end_date}")
print(f"Points: {len(points)}")
print(f"Total kWh: {sum(values):.3f}")
print(f"Average kWh/day: {sum(values)/len(values):.3f}")
print(f"Min kWh: {min(values):.3f}")
print(f"Max kWh: {max(values):.3f}")
print(f"Latest: {latest_date} = {latest_value:.3f} kWh")
PY
)"

echo "OK: Live API smoke test passed (HTTP 200, non-empty payload)."
echo "$metrics"
