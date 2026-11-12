"""API client for Vattenfall "Min energi" (the electricity retailer, vattenfall.se).

Uses the same Vattenfall account and login service as Eldistribution, but the `sales`
tenant on accounts.vattenfall.se, and the selfservice API, which is authenticated by
the `.VFIdToken`/`.VFState` cookies only (no subscription key).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
import html
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse
import zoneinfo

import httpx
from homeassistant.core import HomeAssistant

from .api import (
    ConsumptionPoint,
    HourlyConsumptionPoint,
    HourlyTemperaturePoint,
    VattenfallAccessDeniedError,
    VattenfallApiClient,
    VattenfallApiError,
    VattenfallAuthError,
)
from .const import (
    ACCOUNT_TYPE_MIN_ENERGI,
    CONF_ACCOUNT_TYPE,
    CONF_METERING_POINT_ID,
    CONF_SUBSCRIPTION_KEY,
    MIN_ENERGI_API_BASE_URL,
    MIN_ENERGI_AUTH_START_URL,
)

_LOGGER = logging.getLogger(__name__)

_ACCOUNTS_ORIGIN = "https://accounts.vattenfall.se"
_COMMONAUTH_URL = f"{_ACCOUNTS_ORIGIN}/iamng/seb2c/sales/commonauth"
_WEB_ORIGIN = "https://www.vattenfall.se"
_AUTH_COOKIES = (".VFIdToken", ".VFState")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 15
_MAX_CLIENT_REDIRECTS = 3
_API_TIMEZONE = zoneinfo.ZoneInfo("Europe/Stockholm")
_RANGES_TTL = timedelta(minutes=30)
# Longest range requested at once; larger ranges are split, and halved again on HTTP 400.
_MAX_CHUNK = {
    "QuarterHour": timedelta(days=7),
    "Hourly": timedelta(days=7),
    "Daily": timedelta(days=31),
}
_RESOLUTION_PERIODS = {
    "QuarterHour": timedelta(minutes=15),
    "Hourly": timedelta(hours=1),
    "Daily": timedelta(days=1),
}
# Explicit redirects: a meta refresh or a JavaScript location change.
_CLIENT_REDIRECT_PATTERNS = (
    r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+url=([^\"'>]+)",
    r"location(?:\.href)?\s*=\s*[\"']([^\"']+)[\"']",
    r"location\.(?:replace|assign)\(\s*[\"']([^\"']+)[\"']",
)
# Last resort: any quoted OAuth authorize URL on the page.
_AUTHORIZE_URL_PATTERN = r"[\"'](https?://[^\"']*/oauth2/authorize[^\"']*)[\"']"
# Form actions that belong to the login hand-over.
_AUTH_FORM_ACTION = re.compile(r"/(?:oauth2|authorize|commonauth|authcallback|signin)", re.I)

_PREMISES_PARAMS = {
    "includeFutureContracts": "true",
    "contractTypeFilter": "Heat,Battery",
    "excludeContractTypes": "true",
    "combineMicroProductionPremises": "true",
    "combineBatteryPremises": "false",
}


def create_api_client(hass: HomeAssistant, config: Any) -> VattenfallApiClient:
    """Create the API client matching the configured account type."""
    if config.get(CONF_ACCOUNT_TYPE) == ACCOUNT_TYPE_MIN_ENERGI:
        return VattenfallMinEnergiClient(hass=hass, config=config)
    return VattenfallApiClient(hass=hass, config=config)


def _client_side_redirect(
    body: str, base_url: str
) -> tuple[str, str, dict[str, str] | None] | None:
    """Find a browser-side redirect in an HTML page.

    In order of preference: a meta refresh or JavaScript location change, an
    auto-submitted login hand-over form, then any OAuth authorize URL on the page.
    Unrelated forms (search, consent, newsletter) are ignored.
    Returns `(method, url, form_fields)` or None.
    """
    for pattern in _CLIENT_REDIRECT_PATTERNS:
        match = re.search(pattern, body, re.I)
        if match:
            return "GET", _page_url(match.group(1), base_url), None

    form = _auth_form(body, base_url)
    if form is not None:
        return form

    match = re.search(_AUTHORIZE_URL_PATTERN, body, re.I)
    if match:
        return "GET", _page_url(match.group(1), base_url), None
    return None


def _page_url(raw: str, base_url: str) -> str:
    """Resolve a URL found in HTML or JavaScript."""
    return urljoin(base_url, html.unescape(raw).replace("\\/", "/"))


def _auth_form(body: str, base_url: str) -> tuple[str, str, dict[str, str]] | None:
    """Return the login hand-over form, if the page has one.

    A form qualifies when it posts to a login endpoint, or when it is the only form
    on a page that submits a form by script.
    """
    forms = re.findall(r"<form\b([^>]*)>(.*?)</form>", body, re.S | re.I)
    auto_submit = re.search(r"\.submit\s*\(", body) is not None
    for form_attrs, form_body in forms:
        attrs = dict(re.findall(r'(\w+)\s*=\s*["\']([^"\']*)["\']', form_attrs))
        action = html.unescape(attrs.get("action", ""))
        if not (_AUTH_FORM_ACTION.search(action) or (auto_submit and len(forms) == 1)):
            continue
        fields: dict[str, str] = {}
        for tag in re.findall(r"<input\b[^>]*>", form_body, re.I):
            name = re.search(r'\bname\s*=\s*["\']([^"\']*)["\']', tag)
            value = re.search(r'\bvalue\s*=\s*["\']([^"\']*)["\']', tag)
            if name:
                fields[name.group(1)] = html.unescape(value.group(1)) if value else ""
        return attrs.get("method", "get").upper(), urljoin(base_url, action or base_url), fields
    return None


class _VattenfallBadRequestError(VattenfallApiError):
    """The API rejected the request (HTTP 400), for example a too large date range."""


def _epoch_to_date(epoch: Any) -> date | None:
    """Decode an API date (UTC midnight, epoch seconds)."""
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).date()


def _parse_measurement_ranges(
    payload: list[Any],
) -> dict[tuple[str, str], tuple[date | None, date]]:
    """Map (resolution, measurement type) to the first and last day with published data.

    Entries without a last available day have no published data, so they are left out.
    """
    ranges: dict[tuple[str, str], tuple[date | None, date]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        resolution = item.get("resolution")
        measurement_type = item.get("measurementType")
        last_day = _epoch_to_date(item.get("lastAvailableMeasurementDate"))
        if not isinstance(resolution, str) or not isinstance(measurement_type, str) or last_day is None:
            continue
        ranges[(resolution, measurement_type)] = (
            _epoch_to_date(item.get("firstAvailableMeasurementDate")),
            last_day,
        )
    return ranges


def _item_start(item: dict[str, Any]) -> datetime:
    """Return a measurement item's local wall-clock start time (naive).

    Raises KeyError, TypeError or ValueError for a malformed item.
    """
    return datetime(
        int(item["year"]),
        int(item["month"]),
        int(item["day"]),
        int(item.get("hour") or 0),
        int(item.get("minute") or 0),
    )


def _epoch_utc_midnight(day: date) -> int:
    """Encode a date the way the selfservice API expects (UTC midnight, epoch seconds)."""
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


class VattenfallMinEnergiClient(VattenfallApiClient):
    """Client for the Min energi selfservice API.

    The metering point ID in the config entry holds the premise ID.
    """

    supports_quarter_hour = True

    def __init__(self, hass: HomeAssistant, config: Mapping[str, Any]) -> None:
        super().__init__(
            hass=hass,
            config={
                **config,
                # The setup flow logs in before a premise is picked.
                CONF_METERING_POINT_ID: config.get(CONF_METERING_POINT_ID, ""),
                # The selfservice API is authenticated by cookies only.
                CONF_SUBSCRIPTION_KEY: "",
            },
        )
        self._available_days: dict[tuple[str, str], tuple[date | None, date | None]] = {}
        self._ranges_fetched_at: datetime | None = None

    @property
    def _has_auth_cookies(self) -> bool:
        """Check if both selfservice auth cookies are present."""
        cookies = self._cookies_for_url(MIN_ENERGI_API_BASE_URL)
        return all(cookies.get(name) for name in _AUTH_COOKIES)

    def _cookies_for_url(self, url: str) -> dict[str, str]:
        """Return cookies applicable to the URL, honouring host-only cookies.

        vattenfall.se sets `.VFState` twice: host-only on www.vattenfall.se before login
        and on `.www.vattenfall.se` after it. Only the latter may reach the API host.
        When several cookies share a name, the most specific domain and path wins.
        """
        if self._client is None:
            return {}
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path or "/"

        cookies: dict[str, str] = {}
        by_specificity = sorted(
            self._client.cookies.jar,
            key=lambda c: (len((c.domain or "").lstrip(".")), len(c.path or "/")),
        )
        for cookie in by_specificity:
            if cookie.is_expired():
                continue
            domain = cookie.domain or ""
            if domain.startswith("."):
                if not (host == domain[1:] or host.endswith(domain)):
                    continue
            elif domain and host != domain:
                continue
            if not path.startswith(cookie.path or "/"):
                continue
            if cookie.secure and parsed.scheme != "https":
                continue
            cookies[cookie.name] = cookie.value
        return cookies

    def _with_cookie_header(self, url: str, headers: dict[str, str]) -> dict[str, str]:
        """Return headers carrying exactly the cookies that apply to the URL.

        httpx adds the client's whole cookie jar (host-only cookies included) to every
        request unless the request already has a Cookie header, so set one. With no
        applicable cookies, send none, like a browser (the jar is empty at login start).
        """
        cookies = self._cookies_for_url(url)
        if not cookies:
            return dict(headers)
        return {**headers, "cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())}

    async def async_get_premises(self) -> list[dict[str, Any]]:
        """Return the premises (metering points) available on the account."""
        payload = await self._async_fetch_with_auth(
            "Premises",
            lambda: self._async_api_get("my-premises-select", _PREMISES_PARAMS, "premises"),
            list,
            allow_stub=False,
        )
        if not isinstance(payload, list):
            raise VattenfallApiError("Unexpected premises payload shape")
        return [item for item in payload if isinstance(item, dict) and item.get("id")]

    async def _async_login_flow(self) -> None:
        """Run the vattenfall.se login flow and populate auth cookies in jar."""
        client = await self._async_get_client()
        client.cookies.clear()
        _LOGGER.debug("Starting Vattenfall Min energi auth flow")

        login_page_url, response = await self._async_follow_redirects(
            "GET", MIN_ENERGI_AUTH_START_URL, "auth init", headers=self._request_headers(origin=_WEB_ORIGIN)
        )
        # vattenfall.se hands over to accounts.vattenfall.se from inside an HTML page
        # rather than with an HTTP redirect, so follow that hand-over like a browser.
        for hop in range(1, _MAX_CLIENT_REDIRECTS + 1):
            if self._has_session_data_key(login_page_url):
                break
            redirect = _client_side_redirect(response.text, login_page_url)
            if redirect is None:
                break
            method, url, fields = redirect
            login_page_url, response = await self._async_follow_redirects(
                method,
                url,
                f"auth init page redirect {hop}",
                headers=self._request_headers(
                    content_type_form=method == "POST", origin=_WEB_ORIGIN, referer=login_page_url
                ),
                data=fields,
            )

        session_data_key = self._find_session_data_key(login_page_url)
        form_data = {
            "customerId": self._customer_id,
            "password": self._password,
            "auth_method": "customerid_password",
            "tenantDomain": "se.b2c",
            "sessionDataKey": session_data_key,
        }
        headers = self._request_headers(
            content_type_form=True,
            origin=_ACCOUNTS_ORIGIN,
            referer=f"{_ACCOUNTS_ORIGIN}/iamng/seb2c/sales/web/",
        )
        final_url, _ = await self._async_follow_redirects(
            "POST", _COMMONAUTH_URL, "commonauth", headers=headers, data=form_data
        )

        if "authFailure" in final_url:
            raise VattenfallAccessDeniedError("Invalid Vattenfall credentials")
        if not self._has_auth_cookies:
            raise VattenfallAuthError("Authentication completed without API cookies")

    async def _async_follow_redirects(
        self,
        method: str,
        url: str,
        label: str,
        *,
        headers: dict[str, str],
        data: dict[str, str] | None = None,
    ) -> tuple[str, httpx.Response]:
        """Send a request and follow its redirect chain, returning the final URL and response."""
        client = await self._async_get_client()
        for hop in range(_MAX_REDIRECTS):
            step = label if hop == 0 else f"{label} redirect {hop}"
            self._debug_log_request(method, url, headers, data=data)
            request_headers = self._with_cookie_header(url, headers)
            if method == "POST":
                response = await client.post(
                    url, data=data, headers=request_headers, follow_redirects=False
                )
            else:
                response = await client.get(
                    url, params=data or None, headers=request_headers, follow_redirects=False
                )
            self._debug_log_response(step, response)

            if response.status_code in (401, 403):
                raise VattenfallAuthError(f"{step} rejected with HTTP {response.status_code}")
            if response.status_code not in _REDIRECT_STATUSES:
                if response.status_code >= 400:
                    raise VattenfallAuthError(
                        f"{step} failed with HTTP {response.status_code}: {response.text[:200]}"
                    )
                return url, response

            url = self._resolve_url(url, self._redirect_location(response, step))
            method, data = "GET", None
            headers = self._request_headers(origin=_WEB_ORIGIN)
        raise VattenfallAuthError(f"Too many redirects during {label}")

    def _has_session_data_key(self, login_page_url: str) -> bool:
        """Check whether the login page has been reached."""
        try:
            self._find_session_data_key(login_page_url)
        except VattenfallAuthError:
            return False
        return True

    def _find_session_data_key(self, login_page_url: str) -> str:
        """Read `sessionDataKey` from the nonce cookie, falling back to the login page URL."""
        try:
            return self._extract_session_data_key()
        except VattenfallAuthError:
            match = re.search(r"sessionDataKey=([^&#]+)", login_page_url)
            if match:
                return match.group(1)
            raise

    async def _async_api_get(
        self, path: str, params: dict[str, Any], label: str
    ) -> Any:
        """GET a selfservice API resource and return its JSON payload."""
        cookies = self._cookies_for_url(MIN_ENERGI_API_BASE_URL)
        if not all(cookies.get(name) for name in _AUTH_COOKIES):
            raise VattenfallAuthError(f"Missing API auth cookies before {label} request")

        headers = self._request_headers(
            origin=_WEB_ORIGIN,
            sec_fetch_dest="empty",
            sec_fetch_mode="cors",
            sec_fetch_site="same-site",
        )
        headers["accept"] = "application/json"
        query = "&".join(f"{key}={value}" for key, value in params.items())
        endpoint = f"{MIN_ENERGI_API_BASE_URL}/{path}" + (f"?{query}" if query else "")

        response = await self._async_get_with_retry(
            endpoint, self._with_cookie_header(endpoint, headers), None, label
        )

        if response.status_code in (401, 403):
            raise VattenfallAccessDeniedError(
                f"Unauthorized response from Vattenfall API (HTTP {response.status_code}) for {label}"
            )
        if response.status_code in _REDIRECT_STATUSES:
            # An expired session is redirected to the login page.
            raise VattenfallAuthError(
                f"Unauthorized response from Vattenfall API (HTTP {response.status_code}) for {label}"
            )
        if response.status_code >= 400:
            error_class = (
                _VattenfallBadRequestError if response.status_code == 400 else VattenfallApiError
            )
            raise error_class(
                f"Vattenfall API returned HTTP {response.status_code} when fetching {label}: "
                f"{response.text[:200] or '<empty response body>'}"
            )
        try:
            return response.json()
        except ValueError as err:
            raise VattenfallApiError(f"Vattenfall API returned non-JSON body for {label}") from err

    async def _async_fetch_measurement_items(
        self,
        resolution: str,
        measurement_type: str,
        start_date: date,
        end_date: date,
        label: str,
    ) -> list[dict[str, Any]]:
        """Fetch measurement items with a non-null value for an inclusive date range."""
        # Outside the available range the API errors, or pads with zeros/partial values.
        available = await self._async_available_days(resolution, measurement_type)
        if available is None:
            _LOGGER.debug("No published %s on this premise", label)
            return []
        first_day, last_day = available
        if first_day is not None:
            start_date = max(start_date, first_day)
        end_date = min(end_date, last_day)

        raw_items: list[Any] = []
        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(chunk_start + _MAX_CHUNK[resolution] - timedelta(days=1), end_date)
            if resolution == "Daily":
                # Like the site's month view, never cross a calendar month.
                next_month = date(chunk_start.year + chunk_start.month // 12, chunk_start.month % 12 + 1, 1)
                chunk_end = min(chunk_end, next_month - timedelta(days=1))
            raw_items.extend(
                await self._async_fetch_raw_items(
                    resolution, measurement_type, chunk_start, chunk_end, label
                )
            )
            chunk_start = chunk_end + timedelta(days=1)

        # The API pads periods that have not ended yet with zeros, so drop them.
        now = self._now()
        period = _RESOLUTION_PERIODS[resolution]
        items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            measurement = item.get("measurement")
            value = measurement.get("value") if isinstance(measurement, dict) else None
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            try:
                start = _item_start(item).replace(tzinfo=_API_TIMEZONE)
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Skipping malformed %s item: %s", label, item)
                continue
            if start + period <= now:
                items.append(item)
        return items

    async def _async_fetch_raw_items(
        self,
        resolution: str,
        measurement_type: str,
        start_date: date,
        end_date: date,
        label: str,
    ) -> list[Any]:
        """Fetch raw items, halving the range when the API rejects it as too large."""
        try:
            payload = await self._async_api_get(
                f"my-energy/premises/{self._metering_point_id}/measurement",
                {
                    "resolution": resolution,
                    "measurementType": measurement_type,
                    "startDate": _epoch_utc_midnight(start_date),
                    "endDate": _epoch_utc_midnight(end_date),
                },
                label,
            )
        except _VattenfallBadRequestError as err:
            if start_date == end_date:
                raise VattenfallApiError(f"{err} (date {start_date})") from err
            middle = start_date + (end_date - start_date) // 2
            _LOGGER.debug(
                "Vattenfall rejected %s for %s..%s, splitting the range", label, start_date, end_date
            )
            return [
                *await self._async_fetch_raw_items(resolution, measurement_type, start_date, middle, label),
                *await self._async_fetch_raw_items(
                    resolution, measurement_type, middle + timedelta(days=1), end_date, label
                ),
            ]

        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise VattenfallApiError(f"Unexpected {label} payload shape")
        return payload["items"]

    async def _async_available_days(
        self, resolution: str, measurement_type: str
    ) -> tuple[date | None, date] | None:
        """Return the first and last day with published data, or None when there is none.

        The API pads unpublished periods with zeros, so measurements are only trusted inside
        a verified range: when the ranges cannot be fetched and none were fetched before,
        this raises instead of guessing.
        """
        now = self._now()
        if self._ranges_fetched_at is None or now - self._ranges_fetched_at > _RANGES_TTL:
            try:
                payload = await self._async_api_get(
                    f"my-energy/premises/{self._metering_point_id}/measurement-ranges",
                    {},
                    "measurement ranges",
                )
                if not isinstance(payload, list):
                    raise VattenfallApiError("Unexpected measurement ranges payload shape")
            except VattenfallAuthError:
                raise
            except VattenfallApiError as err:
                if self._ranges_fetched_at is None:
                    raise VattenfallApiError(
                        f"Could not fetch measurement ranges, so published data is unknown: {err}"
                    ) from err
                # Keep the last verified ranges; they never extend past what was published.
                _LOGGER.debug("Could not refresh measurement ranges, keeping previous ones: %s", err)
            else:
                self._available_days = _parse_measurement_ranges(payload)
                self._ranges_fetched_at = now
        return self._available_days.get((resolution, measurement_type))

    @staticmethod
    def _now() -> datetime:
        """Return the current time (separate for tests)."""
        return datetime.now(_API_TIMEZONE)

    async def _async_fetch_daily_consumption(
        self,
        start_date: date,
        end_date: date,
    ) -> list[ConsumptionPoint]:
        """Fetch daily consumption from the selfservice API."""
        items = await self._async_fetch_measurement_items(
            "Daily", "ElectricityConsumption", start_date, end_date, "daily consumption"
        )
        points = [
            ConsumptionPoint(
                date=_item_start(item).date().isoformat(),
                value_kwh=float(item["measurement"]["value"]),
            )
            for item in items
        ]
        points.sort(key=lambda p: p.date)
        return points

    async def _async_fetch_hourly_consumption(
        self,
        start_date: date,
        end_date: date,
        include_load: bool,
    ) -> list[HourlyConsumptionPoint]:
        """Fetch hourly consumption from the selfservice API (`include_load` is not applicable)."""
        items = await self._async_fetch_measurement_items(
            "Hourly", "ElectricityConsumption", start_date, end_date, "hourly consumption"
        )
        points = [
            HourlyConsumptionPoint(
                date_time=self._local_time_iso(item),
                value_kwh=float(item["measurement"]["value"]),
            )
            for item in items
        ]
        points.sort(key=lambda p: p.date_time)
        return points

    async def async_get_quarter_hour_consumption(
        self,
        start_date: date,
        end_date: date,
        allow_stub: bool = True,
    ) -> list[HourlyConsumptionPoint]:
        """Fetch 15-minute consumption from the selfservice API."""
        return await self._async_fetch_with_auth(
            "15-minute consumption",
            lambda: self._async_fetch_quarter_hour_consumption(start_date, end_date),
            list,
            allow_stub,
        )

    async def _async_fetch_quarter_hour_consumption(
        self,
        start_date: date,
        end_date: date,
    ) -> list[HourlyConsumptionPoint]:
        """Fetch 15-minute consumption points."""
        items = await self._async_fetch_measurement_items(
            "QuarterHour", "ElectricityConsumption", start_date, end_date, "15-minute consumption"
        )
        points = [
            HourlyConsumptionPoint(
                date_time=self._local_time_iso(item),
                value_kwh=float(item["measurement"]["value"]),
            )
            for item in items
        ]
        points.sort(key=lambda p: p.date_time)
        return points

    async def _async_fetch_hourly_temperature(
        self,
        start_date: date,
        end_date: date,
        use_cet: bool,
    ) -> list[HourlyTemperaturePoint]:
        """Fetch hourly temperature for the premise (`use_cet` is not applicable)."""
        items = await self._async_fetch_measurement_items(
            "Hourly", "Temperature", start_date, end_date, "hourly temperature"
        )
        points = [
            HourlyTemperaturePoint(
                date_time=self._local_time_iso(item),
                value_c=float(item["measurement"]["value"]),
            )
            for item in items
        ]
        points.sort(key=lambda p: p.date_time)
        return points

    @staticmethod
    def _local_time_iso(item: dict[str, Any]) -> str:
        """Format an item's local wall-clock start time like the Eldistribution API does."""
        return _item_start(item).isoformat()
