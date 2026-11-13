#!/usr/bin/env python3
"""Standalone live probe for the Vattenfall "Min energi" (vattenfall.se) API.

Runs the real login flow and a handful of API calls, and writes a redacted report
(no passwords, cookie values, tokens or premise IDs) that is safe to share.

Requires only httpx with HTTP/2 support:

    python3 -m venv .venv && .venv/bin/pip install 'httpx[http2]'
    .venv/bin/python min_energi_probe.py

The report is printed and written to `min_energi_report.txt` next to the script.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import getpass
import html
import json
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

LOGIN_START_URL = "https://www.vattenfall.se/iam/se/login?ReturnUrl=%2Fmina-sidor"
ACCOUNTS_ORIGIN = "https://accounts.vattenfall.se"
COMMONAUTH_URL = f"{ACCOUNTS_ORIGIN}/iamng/seb2c/sales/commonauth"
WEB_ORIGIN = "https://www.vattenfall.se"
API_BASE = "https://selfserviceapi.www.vattenfall.se/elements"
SESSION_NONCE_PREFIX = "sessionNonceCookie-"
AUTH_COOKIES = (".VFIdToken", ".VFState")
MAX_REDIRECTS = 15

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)

SAFE_PARAMS = {
    "response_type", "scope", "auth_method", "tenantDomain", "resolution",
    "measurementType", "client_id", "redirect_uri", "response_mode", "prompt",
    "ui_locales", "ReturnUrl", "returnUrl", "includeFutureContracts",
    "contractTypeFilter", "excludeContractTypes", "combineMicroProductionPremises",
    "combineBatteryPremises", "authFailure", "authFailureMsg", "errorCode",
}

REPORT: List[str] = []
SECRETS: List[str] = []


def out(line: str = "") -> None:
    """Print a report line, scrubbing any known secret values first."""
    for secret in SECRETS:
        if secret and len(secret) >= 4:
            line = line.replace(secret, "<secret>")
    REPORT.append(line)
    print(line)


def red_url(url: str) -> str:
    """Redact query values (except whitelisted) and ID-looking path segments."""
    parts = urlsplit(url)
    path = re.sub(r"/[A-Za-z]{0,4}\d{6,}", "/<id>", parts.path)
    path = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{27,}", "/<uuid>", path, flags=re.I)

    def red_qs(qs: str) -> str:
        pairs = parse_qsl(qs, keep_blank_values=True)
        return "&".join(f"{k}={v if k in SAFE_PARAMS else '***'}" for k, v in pairs)

    result = f"{parts.scheme}://{parts.netloc}{path}"
    if parts.query:
        result += "?" + red_qs(parts.query)
    if parts.fragment:
        frag = parts.fragment
        result += "#" + (red_qs(frag.split("?", 1)[-1]) if "=" in frag else "<fragment>")
    return result


def shape(obj: Any, depth: int = 0) -> Any:
    """Return the JSON structure with values replaced by their type names."""
    if depth > 5:
        return "..."
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1), f"<len {len(obj)}>"] if obj else []
    return type(obj).__name__


def cookie_names(client: httpx.AsyncClient, domain_hint: str) -> List[str]:
    return sorted({c.name for c in client.cookies.jar if domain_hint in (c.domain or "")})


def browser_headers(**extra: str) -> Dict[str, str]:
    headers = {
        "user-agent": USER_AGENT,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "sv-SE,sv;q=0.9,en;q=0.8",
    }
    headers.update(extra)
    return headers


def api_headers() -> Dict[str, str]:
    return {
        "user-agent": USER_AGENT,
        "accept": "application/json",
        "accept-language": "sv-SE,sv;q=0.9,en;q=0.8",
        "origin": WEB_ORIGIN,
        "referer": f"{WEB_ORIGIN}/",
        "cache-control": "no-cache",
        "pragma": "no-cache",
    }


async def follow(
    client: httpx.AsyncClient, response: httpx.Response, label: str
) -> httpx.Response:
    """Follow redirects manually, logging each hop."""
    hop = 0
    while response.status_code in (301, 302, 303, 307, 308):
        hop += 1
        if hop > MAX_REDIRECTS:
            raise RuntimeError(f"{label}: too many redirects")
        location = response.headers.get("location", "")
        next_url = urljoin(str(response.url), location)
        set_cookies = [
            v.split("=", 1)[0] for k, v in response.headers.multi_items() if k == "set-cookie"
        ]
        out(f"  {label} hop {hop}: {response.status_code} -> {red_url(next_url)}")
        if set_cookies:
            out(f"      set-cookie: {', '.join(set_cookies)}")
        response = await client.get(next_url, headers=browser_headers())
    set_cookies = [v.split("=", 1)[0] for k, v in response.headers.multi_items() if k == "set-cookie"]
    out(
        f"  {label} final: {response.status_code} {red_url(str(response.url))} "
        f"[{response.headers.get('content-type', '').split(';')[0]}]"
    )
    if set_cookies:
        out(f"      set-cookie: {', '.join(set_cookies)}")
    return response


def find_session_data_key(client: httpx.AsyncClient, last_url: str) -> Optional[str]:
    for cookie in client.cookies.jar:
        if cookie.name.startswith(SESSION_NONCE_PREFIX):
            return cookie.name[len(SESSION_NONCE_PREFIX):]
    match = re.search(r"sessionDataKey=([^&#]+)", last_url)
    return match.group(1) if match else None


def red_text(text: str) -> str:
    """Redact URLs and long opaque tokens inside a text snippet."""
    text = re.sub(r"https?://[^\s\"'<>]+", lambda m: red_url(html.unescape(m.group(0))), text)
    return re.sub(r"[A-Za-z0-9_\-]{24,}", "<token>", text)


def describe_page(body: str) -> None:
    """Log a redacted outline of an HTML page: forms, refreshes, scripts, redirects."""
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    out(f"  page: {len(body)} chars, title={title.group(1).strip()[:80] if title else None!r}")
    for form in re.finditer(r"<form\b([^>]*)>(.*?)</form>", body, re.S | re.I):
        attrs = dict(re.findall(r'(\w+)\s*=\s*["\']([^"\']*)["\']', form.group(1)))
        names = re.findall(r'<input\b[^>]*\bname\s*=\s*["\']([^"\']+)["\']', form.group(2), re.I)
        action = html.unescape(attrs.get("action", ""))
        out(f"  form: method={attrs.get('method', 'get')} action={red_url(action) if action.startswith('http') else action[:80]} inputs={names}")
    for meta in re.findall(r"<meta[^>]+http-equiv=[\"']?refresh[^>]*>", body, re.I):
        out(f"  meta refresh: {red_text(meta)}")
    for src in re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", body, re.I):
        out(f"  script src: {red_url(urljoin(WEB_ORIGIN, html.unescape(src)))}")
    for script in re.findall(r"<script\b[^>]*>(.*?)</script>", body, re.S | re.I):
        for match in re.finditer(r"location|authorize|accounts\.vattenfall|submit\(|redirect", script, re.I):
            start = max(0, match.start() - 80)
            out(f"  inline script: ...{red_text(script[start:match.end() + 120])!r}...")
            break
    for url in sorted(set(re.findall(r"https?://accounts\.vattenfall\.[a-z]+[^\s\"'<>]*", body))):
        out(f"  accounts url: {red_url(html.unescape(url))}")


def client_side_redirect(body: str, base_url: str) -> Optional[tuple]:
    """Find a browser-side redirect: auto-submitted form, meta refresh or JS location."""
    form = re.search(r"<form\b([^>]*)>(.*?)</form>", body, re.S | re.I)
    if form:
        attrs = dict(re.findall(r'(\w+)\s*=\s*["\']([^"\']*)["\']', form.group(1)))
        fields = {}
        for tag in re.findall(r"<input\b[^>]*>", form.group(2), re.I):
            name = re.search(r'\bname\s*=\s*["\']([^"\']*)["\']', tag)
            value = re.search(r'\bvalue\s*=\s*["\']([^"\']*)["\']', tag)
            if name:
                fields[name.group(1)] = html.unescape(value.group(1)) if value else ""
        action = urljoin(base_url, html.unescape(attrs.get("action", base_url)))
        return ("form", attrs.get("method", "get").upper(), action, fields)
    patterns = [
        ("meta refresh", r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+url=([^\"'>]+)"),
        ("js location", r"location(?:\.href)?\s*=\s*[\"']([^\"']+)[\"']"),
        ("js location", r"location\.(?:replace|assign)\(\s*[\"']([^\"']+)[\"']"),
        ("authorize url", r"[\"'](https?://[^\"']*/oauth2/authorize[^\"']*)[\"']"),
    ]
    for kind, pattern in patterns:
        match = re.search(pattern, body, re.I)
        if match:
            return (kind, "GET", urljoin(base_url, html.unescape(match.group(1)).replace("\\/", "/")), None)
    return None


async def login(client: httpx.AsyncClient, customer_id: str, password: str) -> bool:
    out("== 1. Login start")
    response = await client.get(LOGIN_START_URL, headers=browser_headers())
    if response.status_code == 403:
        out("  HTTP 403 on www.vattenfall.se -> blocked by WAF (wrong country IP?). Stopping.")
        return False
    response = await follow(client, response, "start")

    for attempt in range(1, 4):
        if find_session_data_key(client, str(response.url)) or "html" not in response.headers.get("content-type", ""):
            break
        out(f"  -- no sessionDataKey yet, analysing HTML page (attempt {attempt})")
        describe_page(response.text)
        redirect = client_side_redirect(response.text, str(response.url))
        if not redirect:
            out("  no client-side redirect found in page")
            break
        kind, method, url, fields = redirect
        out(f"  following {kind}: {method} {red_url(url)} fields={sorted(fields) if fields else None}")
        if method == "POST":
            response = await client.post(url, data=fields, headers=browser_headers(referer=str(response.url)))
        else:
            response = await client.get(url, params=fields or None, headers=browser_headers(referer=str(response.url)))
        response = await follow(client, response, f"client redirect {attempt}")
    out(f"  cookies accounts.vattenfall.se: {cookie_names(client, 'accounts.vattenfall')}")

    session_data_key = find_session_data_key(client, str(response.url))
    if not session_data_key:
        out("  ERROR: no sessionDataKey (no sessionNonceCookie-* cookie, not in URL)")
        return False
    SECRETS.append(session_data_key)
    out("  sessionDataKey: found")

    out("== 2. commonauth POST")
    form = {
        "customerId": customer_id,
        "password": password,
        "auth_method": "customerid_password",
        "tenantDomain": "se.b2c",
        "sessionDataKey": session_data_key,
    }
    response = await client.post(
        COMMONAUTH_URL,
        data=form,
        headers=browser_headers(
            origin=ACCOUNTS_ORIGIN,
            referer=f"{ACCOUNTS_ORIGIN}/iamng/seb2c/sales/web/",
            **{"content-type": "application/x-www-form-urlencoded"},
        ),
    )
    out(f"  commonauth: HTTP {response.status_code}")
    response = await follow(client, response, "callback")
    if "authFailure" in str(response.url) or "login.do" in str(response.url):
        out("  Login looks rejected (authFailure in URL) -> wrong credentials?")

    out(f"  cookies vattenfall.se: {cookie_names(client, 'vattenfall.se')}")
    missing = [c for c in AUTH_COOKIES if c not in {k.name for k in client.cookies.jar}]
    if missing:
        out(f"  ERROR: missing auth cookies after login: {missing}")
        return False
    for c in client.cookies.jar:
        if c.name in AUTH_COOKIES:
            out(f"  {c.name}: domain={c.domain} path={c.path} expires={c.expires}")
    return True


async def api_get(client: httpx.AsyncClient, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{API_BASE}/{path}"
    response = await client.get(url, params=params, headers=api_headers())
    out(f"  GET {red_url(str(response.url))} -> {response.status_code}")
    if response.status_code != 200:
        out(f"      body: {response.text[:200]!r}")
        return None
    try:
        return response.json()
    except ValueError:
        out(f"      non-JSON body: {response.text[:200]!r}")
        return None


def epoch_utc_midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def summarize_measurement(label: str, payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    items = payload.get("items") or []
    keys = {k: v for k, v in payload.items() if k not in ("items", "premiseId")}
    out(f"  [{label}] meta: {json.dumps(keys)}")
    out(f"  [{label}] items: {len(items)}")
    if not items:
        return
    out(f"  [{label}] item shape: {json.dumps(shape(items[0]))}")
    stamp = lambda it: "{:04d}-{:02d}-{:02d} {:02d}:{:02d}".format(  # noqa: E731
        it.get("year", 0), it.get("month", 0), it.get("day", 0), it.get("hour", 0), it.get("minute", 0)
    )
    out(f"  [{label}] first: {stamp(items[0])}  last: {stamp(items[-1])}")
    values = [(it.get("measurement") or {}).get("value") for it in items]
    nulls = sum(1 for v in values if v is None)
    total = sum(v for v in values if isinstance(v, (int, float)))
    out(f"  [{label}] null values: {nulls}  sum: {total:.3f}")
    stamps = [stamp(it) for it in items]
    dupes = sorted({s for s in stamps if stamps.count(s) > 1})
    if dupes:
        out(f"  [{label}] duplicate timestamps (DST?): {dupes}")
    extra_keys = sorted({k for it in items for k in it} - {"year", "month", "day", "hour", "minute", "measurement"})
    if extra_keys:
        out(f"  [{label}] extra item keys: {extra_keys}")


async def probe_api(client: httpx.AsyncClient) -> None:
    out("== 3. Premises")
    premises = await api_get(
        client,
        "my-premises-select",
        {
            "includeFutureContracts": "true",
            "contractTypeFilter": "Heat,Battery",
            "excludeContractTypes": "true",
            "combineMicroProductionPremises": "true",
            "combineBatteryPremises": "false",
        },
    )
    if not premises:
        return
    out(f"  shape: {json.dumps(shape(premises))}")
    for index, premise in enumerate(premises):
        SECRETS.extend(str(premise.get(k, "")) for k in ("id", "name", "zipCode", "city"))
        out(
            f"  premise #{index}: serviceGroup={premise.get('serviceGroup')} "
            f"gridOwner={premise.get('gridOwner')}"
        )

    yesterday = date.today() - timedelta(days=1)
    for index, premise in enumerate(premises):
        premise_id = premise["id"]
        out(f"== 4. Premise #{index}")
        ranges = await api_get(client, f"my-energy/premises/{premise_id}/measurement-ranges")
        for r in ranges or []:
            first = datetime.fromtimestamp(r["firstAvailableMeasurementDate"], timezone.utc).date()
            last = datetime.fromtimestamp(r["lastAvailableMeasurementDate"], timezone.utc).date()
            out(f"  range {r['measurementType']:<28} {r['resolution']:<12} {r['unit']:<14} {first} .. {last}")

        cases = [
            ("QuarterHour yesterday", "QuarterHour", "ElectricityConsumption", yesterday, yesterday),
            ("Hourly yesterday", "Hourly", "ElectricityConsumption", yesterday, yesterday),
            ("Daily yesterday", "Daily", "ElectricityConsumption", yesterday, yesterday),
            ("Hourly 3 days", "Hourly", "ElectricityConsumption", yesterday - timedelta(days=2), yesterday),
            ("Daily 7 days", "Daily", "ElectricityConsumption", yesterday - timedelta(days=6), yesterday),
            ("Hourly DST end 2025-10-26", "Hourly", "ElectricityConsumption", date(2025, 10, 26), date(2025, 10, 26)),
            ("QuarterHour DST end 2025-10-26", "QuarterHour", "ElectricityConsumption", date(2025, 10, 26), date(2025, 10, 26)),
            ("Hourly DST start 2026-03-29", "Hourly", "ElectricityConsumption", date(2026, 3, 29), date(2026, 3, 29)),
            ("Hourly temperature yesterday", "Hourly", "Temperature", yesterday, yesterday),
            ("Daily cost 7 days", "Daily", "ElectricityConsumptionCost", yesterday - timedelta(days=6), yesterday),
            ("Hourly today (partial)", "Hourly", "ElectricityConsumption", date.today(), date.today()),
        ]
        for label, resolution, mtype, start, end in cases:
            payload = await api_get(
                client,
                f"my-energy/premises/{premise_id}/measurement",
                {
                    "resolution": resolution,
                    "measurementType": mtype,
                    "startDate": epoch_utc_midnight(start),
                    "endDate": epoch_utc_midnight(end),
                },
            )
            summarize_measurement(label, payload)


async def main() -> int:
    customer_id = input("Customer ID (kundnummer/personnummer): ").strip()
    password = getpass.getpass("Password: ")
    if not customer_id or not password:
        print("Customer ID and password are required")
        return 1
    SECRETS.extend([customer_id, password])

    out(f"Min energi probe {datetime.now().isoformat(timespec='seconds')} python {sys.version.split()[0]} httpx {httpx.__version__}")
    async with httpx.AsyncClient(http2=True, timeout=30.0, follow_redirects=False) as client:
        try:
            if await login(client, customer_id, password):
                await probe_api(client)
                out("== 5. Re-login after clearing cookies (as the integration does)")
                client.cookies.clear()
                out(f"  second login ok: {await login(client, customer_id, password)}")
        except Exception as err:  # noqa: BLE001
            out(f"EXCEPTION: {type(err).__name__}: {err}")

    report_path = Path(__file__).with_name("min_energi_report.txt")
    report_path.write_text("\n".join(REPORT) + "\n", encoding="utf-8")
    print(f"\nReport written to {report_path} (review it before sharing)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
