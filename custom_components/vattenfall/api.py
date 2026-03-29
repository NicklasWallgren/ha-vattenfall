"""API client for Vattenfall consumption data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ALLOW_STUB_DATA,
    CONF_CUSTOMER_ID,
    CONF_METERING_POINT_ID,
    CONF_PASSWORD,
    CONF_SUBSCRIPTION_KEY,
    CONF_TEMPERATURE_AREA_CODE,
    DEFAULT_AUTH_START_URL,
    DEFAULT_BASE_URL,
    DEFAULT_TEMPERATURE_AREA_CODE,
)

_LOGGER = logging.getLogger(__name__)

_ACCOUNTS_BASE_URL = "https://accounts.vattenfall.com"
_COMMONAUTH_URL = "https://accounts.vattenfall.com/iamng/seb2c/dso/commonauth"
_SESSION_NONCE_PREFIX = "sessionNonceCookie-"
_WEB_ORIGIN = "https://www.vattenfalleldistribution.se"
_ACCOUNTS_ORIGIN = "https://accounts.vattenfall.com"
_MAX_SERVER_ERROR_RETRIES = 3
_RETRY_DELAY_S = 2.0

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


class VattenfallApiError(Exception):
    """Generic API error."""


class VattenfallAuthError(VattenfallApiError):
    """Authentication error."""


@dataclass(slots=True)
class ConsumptionPoint:
    """A single consumption datapoint."""

    date: str
    value_kwh: float


@dataclass(slots=True)
class HourlyConsumptionPoint:
    """A single hourly consumption datapoint."""

    date_time: str
    value_kwh: float
    status: str | None = None


@dataclass(slots=True)
class HourlyTemperaturePoint:
    """A single hourly temperature datapoint."""

    date_time: str
    value_c: float


class VattenfallApiClient:
    """Vattenfall API client."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self._hass = hass
        self._base_url = DEFAULT_BASE_URL
        self._metering_point_id: str = config[CONF_METERING_POINT_ID]
        self._customer_id: str = config[CONF_CUSTOMER_ID]
        self._password: str = config[CONF_PASSWORD]
        self._subscription_key: str = config[CONF_SUBSCRIPTION_KEY]
        self._temperature_area_code: str = str(
            config.get(CONF_TEMPERATURE_AREA_CODE, DEFAULT_TEMPERATURE_AREA_CODE)
        )
        self._allow_stub_data: bool = config.get(CONF_ALLOW_STUB_DATA, False)
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _create_httpx_client() -> httpx.AsyncClient:
        """Create a configured async HTTP client."""
        return httpx.AsyncClient(http2=True, timeout=30.0)

    async def _async_get_client(self) -> httpx.AsyncClient:
        """Create and cache HTTP client lazily, off the event loop when possible."""
        if self._client is not None:
            return self._client

        if hasattr(self._hass, "async_add_executor_job"):
            self._client = await self._hass.async_add_executor_job(self._create_httpx_client)
        else:
            # Fallback for standalone test scripts where hass is a stub object.
            self._client = self._create_httpx_client()

        return self._client

    async def async_close(self) -> None:
        """Close HTTP client resources."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def async_authenticate(self, force: bool = False) -> None:
        """Authenticate against Vattenfall and ensure API cookies are available."""
        if not force and self._has_auth_cookies:
            return

        try:
            await self._async_login_flow()
        except httpx.HTTPError as err:
            raise VattenfallApiError(f"Network error during authentication: {err}") from err

    async def async_get_daily_consumption(
        self,
        start_date: date,
        end_date: date,
    ) -> list[ConsumptionPoint]:
        """Fetch daily measured consumption from the API."""
        try:
            await self.async_authenticate()
            return await self._async_fetch_daily_consumption(start_date, end_date)
        except VattenfallAuthError:
            await self.async_authenticate(force=True)
            return await self._async_fetch_daily_consumption(start_date, end_date)
        except (VattenfallApiError, httpx.HTTPError) as err:
            if self._allow_stub_data:
                _LOGGER.warning("Consumption fetch failed, falling back to stub data: %s", err)
                return self._build_stub_points(start_date, end_date)
            if isinstance(err, VattenfallApiError):
                raise
            raise VattenfallApiError(f"Network error while calling Vattenfall API: {err}") from err

    async def async_get_hourly_consumption(
        self,
        start_date: date,
        end_date: date,
        include_load: bool = True,
    ) -> list[HourlyConsumptionPoint]:
        """Fetch hourly measured consumption from the API."""
        try:
            await self.async_authenticate()
            return await self._async_fetch_hourly_consumption(
                start_date, end_date, include_load=include_load
            )
        except VattenfallAuthError:
            await self.async_authenticate(force=True)
            return await self._async_fetch_hourly_consumption(
                start_date, end_date, include_load=include_load
            )
        except (VattenfallApiError, httpx.HTTPError) as err:
            if self._allow_stub_data:
                _LOGGER.warning("Hourly consumption fetch failed, falling back to stub data: %s", err)
                return self._build_stub_hourly_points(start_date, end_date)
            if isinstance(err, VattenfallApiError):
                raise
            raise VattenfallApiError(f"Network error while calling Vattenfall API: {err}") from err

    async def async_get_hourly_temperature(
        self,
        start_date: date,
        end_date: date,
        use_cet: bool = True,
    ) -> list[HourlyTemperaturePoint]:
        """Fetch hourly temperature from the API."""
        try:
            await self.async_authenticate()
            return await self._async_fetch_hourly_temperature(start_date, end_date, use_cet=use_cet)
        except VattenfallAuthError:
            await self.async_authenticate(force=True)
            return await self._async_fetch_hourly_temperature(start_date, end_date, use_cet=use_cet)
        except (VattenfallApiError, httpx.HTTPError) as err:
            if self._allow_stub_data:
                _LOGGER.warning("Hourly temperature fetch failed, falling back to stub data: %s", err)
                return self._build_stub_hourly_temperature_points(start_date, end_date)
            if isinstance(err, VattenfallApiError):
                raise
            raise VattenfallApiError(f"Network error while calling Vattenfall API: {err}") from err

    @property
    def _has_auth_cookies(self) -> bool:
        """Check if both API auth cookies are present."""
        security_cookie = self._cookie_value("VF_SecurityCookie", domain_hint="vattenfalleldistribution.se")
        access_cookie = self._cookie_value("VF_AccessCookie", domain_hint="vattenfalleldistribution.se")
        return bool(security_cookie and access_cookie)

    async def _async_login_flow(self) -> None:
        """Run Vattenfall login flow and populate auth cookies in jar."""
        client = await self._async_get_client()
        client.cookies.clear()
        _LOGGER.debug("Starting Vattenfall auth flow")

        headers = self._request_headers()
        cookies = self._cookies_for_url(DEFAULT_AUTH_START_URL)
        self._debug_log_request("GET", DEFAULT_AUTH_START_URL, headers, cookies=cookies)
        response = await client.get(
            DEFAULT_AUTH_START_URL,
            headers=headers,
            cookies=cookies,
            follow_redirects=False,
        )
        self._debug_log_response("auth init", response)
        location_1 = self._redirect_location(response, "auth init")

        authorize_url = self._resolve_url(DEFAULT_AUTH_START_URL, location_1)
        headers = self._request_headers()
        cookies = self._cookies_for_url(authorize_url)
        self._debug_log_request("GET", authorize_url, headers, cookies=cookies)
        response = await client.get(
            authorize_url,
            headers=headers,
            cookies=cookies,
            follow_redirects=False,
        )
        self._debug_log_response("authorize", response)
        if response.status_code >= 400:
            raise VattenfallAuthError(
                f"Authorize step failed with HTTP {response.status_code}: {response.text[:200]}"
            )

        session_data_key = self._extract_session_data_key()
        session_nonce_cookie_name = f"{_SESSION_NONCE_PREFIX}{session_data_key}"
        session_nonce_cookie_value = self._cookie_value(
            session_nonce_cookie_name, domain_hint="vattenfall.com"
        )

        form_data = {
            "customerId": self._customer_id,
            "password": self._password,
            "auth_method": "customerid_password",
            "tenantDomain": "se.b2c",
            "sessionDataKey": session_data_key,
        }
        commonauth_cookies = self._cookies_for_url(_COMMONAUTH_URL)
        if session_nonce_cookie_value:
            commonauth_cookies[session_nonce_cookie_name] = session_nonce_cookie_value
        dt_cookie = self._cookie_value("dtCookie", domain_hint="vattenfall.com")
        if dt_cookie:
            commonauth_cookies["dtCookie"] = dt_cookie
        opbs_cookie = self._cookie_value("opbs", domain_hint="vattenfall.com")
        if opbs_cookie:
            commonauth_cookies["opbs"] = opbs_cookie

        headers = self._request_headers(
            content_type_form=True,
            origin=_ACCOUNTS_ORIGIN,
            referer=f"{_ACCOUNTS_ORIGIN}/iamng/seb2c/dso/web/",
        )
        self._debug_log_request(
            "POST", _COMMONAUTH_URL, headers, cookies=commonauth_cookies, data=form_data
        )
        response = await client.post(
            _COMMONAUTH_URL,
            data=form_data,
            headers=headers,
            cookies=commonauth_cookies,
            follow_redirects=False,
        )
        self._debug_log_response("commonauth", response)
        if response.status_code in (401, 403):
            raise VattenfallAuthError("Invalid Vattenfall credentials")
        location_2 = self._redirect_location(response, "commonauth")

        oauth2_url = self._resolve_url(_COMMONAUTH_URL, location_2)
        oauth2_cookies = self._cookies_for_url(oauth2_url)
        if session_nonce_cookie_value:
            oauth2_cookies[session_nonce_cookie_name] = session_nonce_cookie_value
        if opbs_cookie:
            oauth2_cookies["opbs"] = opbs_cookie
        headers = self._request_headers()
        self._debug_log_request("GET", oauth2_url, headers, cookies=oauth2_cookies)
        response = await client.get(
            oauth2_url,
            headers=headers,
            cookies=oauth2_cookies,
            follow_redirects=False,
        )
        self._debug_log_response("oauth authorize", response)
        location_3 = self._redirect_location(response, "oauth authorize")

        callback_url = self._resolve_url(oauth2_url, location_3)
        self._set_auth_scope_cookie_from_callback(callback_url)
        headers = self._request_headers()
        cookies = self._cookies_for_url(callback_url)
        self._debug_log_request("GET", callback_url, headers, cookies=cookies)
        response = await client.get(
            callback_url,
            headers=headers,
            cookies=cookies,
            follow_redirects=False,
        )
        self._debug_log_response("auth callback", response)
        if response.status_code >= 400:
            raise VattenfallAuthError(
                f"Callback step failed with HTTP {response.status_code}: {response.text[:200]}"
            )

        if not self._has_auth_cookies:
            raise VattenfallAuthError("Authentication completed without API cookies")

    async def _async_get_with_retry(
        self,
        endpoint: str,
        headers: dict[str, str],
        cookies: dict[str, str],
        label: str,
    ) -> httpx.Response:
        """Make a GET request, retrying on 5xx server errors."""
        client = await self._async_get_client()
        response: httpx.Response | None = None
        for attempt in range(1, _MAX_SERVER_ERROR_RETRIES + 1):
            self._debug_log_request("GET", endpoint, headers=headers, cookies=cookies)
            response = await client.get(
                endpoint, headers=headers, cookies=cookies, follow_redirects=False
            )
            self._debug_log_response(label, response)
            if response.status_code < 500 or attempt == _MAX_SERVER_ERROR_RETRIES:
                break
            _LOGGER.warning(
                "Vattenfall API returned HTTP %d for %s (attempt %d/%d), retrying in %.1fs",
                response.status_code, label, attempt, _MAX_SERVER_ERROR_RETRIES, _RETRY_DELAY_S,
            )
            await asyncio.sleep(_RETRY_DELAY_S)
        return response  # type: ignore[return-value]

    async def _async_fetch_daily_consumption(
        self,
        start_date: date,
        end_date: date,
    ) -> list[ConsumptionPoint]:
        """Fetch daily measured consumption from Vattenfall API."""
        endpoint = (
            f"{self._base_url}/consumption/consumption/"
            f"{self._metering_point_id}/{start_date}/{end_date}/Daily/Measured"
        )

        headers = self._request_headers(
            sec_fetch_dest="empty",
            sec_fetch_mode="cors",
            sec_fetch_site="same-site",
            priority="u=1, i",
        )
        headers["accept"] = "application/json, text/plain, */*"
        headers["ocp-apim-subscription-key"] = self._subscription_key

        cookies: dict[str, str] = {}
        for key in ("csrf-token", "VF_SecurityCookie", "VF_AccessCookie"):
            value = self._cookie_value(key, domain_hint="vattenfalleldistribution.se")
            if value:
                cookies[key] = value

        if not cookies.get("VF_SecurityCookie") or not cookies.get("VF_AccessCookie"):
            raise VattenfallAuthError("Missing API auth cookies before consumption request")

        response = await self._async_get_with_retry(endpoint, headers, cookies, "consumption")

        if response.status_code in (401, 403):
            raise VattenfallAuthError(
                f"Unauthorized response from Vattenfall API (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise VattenfallApiError(
                f"Vattenfall API returned HTTP {response.status_code} when fetching daily consumption: {response.text[:200] or '<empty response body>'}"
            )

        payload = response.json()

        points = self._extract_points(payload)
        if points:
            return points

        if self._allow_stub_data:
            _LOGGER.warning("Unexpected API payload shape, falling back to stub data")
            return self._build_stub_points(start_date, end_date)

        raise VattenfallApiError("Unexpected API payload shape; no points extracted")

    async def _async_fetch_hourly_consumption(
        self,
        start_date: date,
        end_date: date,
        include_load: bool,
    ) -> list[HourlyConsumptionPoint]:
        """Fetch hourly measured consumption from Vattenfall API."""
        endpoint = (
            f"{self._base_url}/consumption/consumption/"
            f"{self._metering_point_id}/{start_date}/{end_date}/Hourly/Measured"
            f"?includeLoad={'true' if include_load else 'false'}"
        )

        headers = self._request_headers(
            sec_fetch_dest="empty",
            sec_fetch_mode="cors",
            sec_fetch_site="same-site",
            priority="u=1, i",
        )
        headers["accept"] = "application/json, text/plain, */*"
        headers["ocp-apim-subscription-key"] = self._subscription_key

        cookies: dict[str, str] = {}
        for key in ("csrf-token", "VF_SecurityCookie", "VF_AccessCookie"):
            value = self._cookie_value(key, domain_hint="vattenfalleldistribution.se")
            if value:
                cookies[key] = value

        if not cookies.get("VF_SecurityCookie") or not cookies.get("VF_AccessCookie"):
            raise VattenfallAuthError("Missing API auth cookies before hourly consumption request")

        response = await self._async_get_with_retry(endpoint, headers, cookies, "hourly_consumption")

        if response.status_code in (401, 403):
            raise VattenfallAuthError(
                f"Unauthorized response from Vattenfall API (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise VattenfallApiError(
                f"Vattenfall API returned HTTP {response.status_code} when fetching hourly consumption: {response.text[:200] or '<empty response body>'}"
            )

        payload = response.json()
        points = self._extract_hourly_points(payload)
        if points:
            return points

        if self._allow_stub_data:
            _LOGGER.warning("Unexpected hourly API payload shape, falling back to stub data")
            return self._build_stub_hourly_points(start_date, end_date)

        raise VattenfallApiError("Unexpected hourly API payload shape; no points extracted")

    async def _async_fetch_hourly_temperature(
        self,
        start_date: date,
        end_date: date,
        use_cet: bool,
    ) -> list[HourlyTemperaturePoint]:
        """Fetch hourly temperature from Vattenfall API."""
        endpoint = (
            f"{self._base_url}/climate/temperature/{self._temperature_area_code}/"
            f"Hourly/{start_date}/{end_date}?useCet={'true' if use_cet else 'false'}"
        )

        headers = self._request_headers(
            sec_fetch_dest="empty",
            sec_fetch_mode="cors",
            sec_fetch_site="same-site",
            priority="u=1, i",
        )
        headers["accept"] = "application/json, text/plain, */*"
        headers["ocp-apim-subscription-key"] = self._subscription_key

        cookies: dict[str, str] = {}
        for key in ("csrf-token", "VF_SecurityCookie", "VF_AccessCookie"):
            value = self._cookie_value(key, domain_hint="vattenfalleldistribution.se")
            if value:
                cookies[key] = value

        if not cookies.get("VF_SecurityCookie") or not cookies.get("VF_AccessCookie"):
            raise VattenfallAuthError("Missing API auth cookies before hourly temperature request")

        response = await self._async_get_with_retry(endpoint, headers, cookies, "hourly_temperature")

        if response.status_code in (401, 403):
            raise VattenfallAuthError(
                f"Unauthorized response from Vattenfall API (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise VattenfallApiError(
                f"Vattenfall API returned HTTP {response.status_code} when fetching hourly temperature: {response.text[:200] or '<empty response body>'}"
            )

        payload = response.json()
        points = self._extract_hourly_temperature_points(payload)
        if points:
            return points

        if self._allow_stub_data:
            _LOGGER.warning("Unexpected hourly temperature API payload shape, falling back to stub data")
            return self._build_stub_hourly_temperature_points(start_date, end_date)

        raise VattenfallApiError("Unexpected hourly temperature API payload shape; no points extracted")

    def _extract_session_data_key(self) -> str:
        """Extract `sessionDataKey` from `sessionNonceCookie-<key>` cookie name."""
        if self._client is None:
            raise VattenfallAuthError("Could not find session nonce cookie")
        for cookie in self._client.cookies.jar:
            if cookie.name.startswith(_SESSION_NONCE_PREFIX):
                return cookie.name.removeprefix(_SESSION_NONCE_PREFIX)
        raise VattenfallAuthError("Could not find session nonce cookie")

    def _redirect_location(self, response: httpx.Response, step_name: str) -> str:
        """Read location header from a redirect response."""
        location = response.headers.get("Location")
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            raise VattenfallAuthError(
                f"Expected redirect during {step_name}, got HTTP {response.status_code}"
            )
        return location

    def _resolve_url(self, base: str, location: str) -> str:
        """Resolve absolute URL from location header."""
        return urljoin(base, location)

    def _cookie_value(self, name: str, domain_hint: str | None = None) -> str | None:
        """Read cookie value by name, optionally constrained by domain."""
        if self._client is None:
            return None
        for cookie in self._client.cookies.jar:
            if cookie.name != name:
                continue
            if domain_hint and domain_hint not in (cookie.domain or ""):
                continue
            return cookie.value
        return None

    def _cookies_for_url(self, url: str) -> dict[str, str]:
        """Return cookies that look applicable for the given URL."""
        if self._client is None:
            return {}
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path or "/"
        secure = parsed.scheme == "https"

        cookies: dict[str, str] = {}
        for cookie in self._client.cookies.jar:
            if cookie.is_expired():
                continue

            domain = (cookie.domain or "").lstrip(".")
            if domain and not (host == domain or host.endswith(f".{domain}")):
                continue

            cookie_path = cookie.path or "/"
            if not path.startswith(cookie_path):
                continue

            if cookie.secure and not secure:
                continue

            cookies[cookie.name] = cookie.value

        return cookies

    def _set_auth_scope_cookie_from_callback(self, callback_url: str) -> None:
        """Set VF_AuthRequestScope cookie from callback state query param."""
        if self._client is None:
            return
        parsed = urlparse(callback_url)
        state = parse_qs(parsed.query).get("state", [None])[0]
        if not state:
            return

        self._client.cookies.set(
            "VF_AuthRequestScope",
            state,
            domain="services.vattenfalleldistribution.se",
            path="/",
        )

    def _request_headers(
        self,
        *,
        content_type_form: bool = False,
        origin: str = _WEB_ORIGIN,
        referer: str | None = None,
        sec_fetch_dest: str | None = None,
        sec_fetch_mode: str | None = None,
        sec_fetch_site: str | None = None,
        priority: str | None = None,
    ) -> dict[str, str]:
        """Build a consistent browser-like header set for all requests."""
        headers = {
            "accept-language": "en-US,en;q=0.9",
            "dnt": "1",
            "origin": origin,
            "referer": referer or f"{origin}/",
            "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-gpc": "1",
            "user-agent": _DEFAULT_USER_AGENT,
        }
        if sec_fetch_dest:
            headers["sec-fetch-dest"] = sec_fetch_dest
        if sec_fetch_mode:
            headers["sec-fetch-mode"] = sec_fetch_mode
        if sec_fetch_site:
            headers["sec-fetch-site"] = sec_fetch_site
        if priority:
            headers["priority"] = priority
        if content_type_form:
            headers["content-type"] = "application/x-www-form-urlencoded"
        return headers

    def _debug_log_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        cookies: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> None:
        """Log outbound HTTP request details for debugging."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        effective_cookies = cookies if cookies is not None else self._cookies_for_url(url)
        header_lines = "\n".join(
            f"    {key}: {value}" for key, value in sorted(headers.items())
        )
        cookie_lines = "\n".join(
            f"    {key}: {value}" for key, value in sorted(effective_cookies.items())
        )
        data_lines = "\n".join(
            f"    {key}: {value}" for key, value in sorted((data or {}).items())
        )
        _LOGGER.debug(
            "\n[vattenfall] HTTP request\n"
            "  method: %s\n"
            "  url: %s\n"
            "  headers:\n%s\n"
            "  cookies:\n%s\n"
            "  data:\n%s",
            method,
            url,
            header_lines or "    (none)",
            cookie_lines or "    (none)",
            data_lines or "    (none)",
        )

    def _debug_log_response(self, step: str, response: httpx.Response) -> None:
        """Log inbound HTTP response details for debugging."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        response_headers: dict[str, str] = dict(response.headers)
        set_cookie_headers = response.headers.get_list("set-cookie")
        header_lines = "\n".join(
            f"    {key}: {value}" for key, value in sorted(response_headers.items())
        )
        set_cookie_lines = "\n".join(f"    {value}" for value in set_cookie_headers)
        _LOGGER.debug(
            "\n[vattenfall] HTTP response\n"
            "  step: %s\n"
            "  status: %s\n"
            "  headers:\n%s\n"
            "  set-cookie:\n%s",
            step,
            response.status_code,
            header_lines or "    (none)",
            set_cookie_lines or "    (none)",
        )

    def _extract_points(self, payload: Any) -> list[ConsumptionPoint]:
        """Extract points from API response payload."""
        raw_points = self._flatten_points(payload)

        points: list[ConsumptionPoint] = []
        for item in raw_points:
            day = (
                item.get("date")
                or item.get("Date")
                or item.get("period")
                or item.get("Period")
                or item.get("from")
                or item.get("From")
            )
            value = (
                item.get("value")
                or item.get("Value")
                or item.get("consumption")
                or item.get("Consumption")
                or item.get("quantity")
                or item.get("Quantity")
            )

            try:
                if day is not None and value is not None:
                    points.append(ConsumptionPoint(date=str(day), value_kwh=float(value)))
            except (TypeError, ValueError):
                continue

        points.sort(key=lambda p: p.date)
        return points

    def _extract_hourly_points(self, payload: Any) -> list[HourlyConsumptionPoint]:
        """Extract hourly points from API response payload."""
        raw_points: list[dict[str, Any]] = []
        if isinstance(payload, dict) and isinstance(payload.get("consumption"), list):
            raw_points = [
                item for item in payload["consumption"] if isinstance(item, dict)
            ]
        else:
            raw_points = self._flatten_points(payload)

        points: list[HourlyConsumptionPoint] = []
        for item in raw_points:
            date_time = (
                item.get("date")
                or item.get("Date")
                or item.get("period")
                or item.get("Period")
                or item.get("from")
                or item.get("From")
            )
            value = (
                item.get("consumption")
                or item.get("Consumption")
                or item.get("value")
                or item.get("Value")
                or item.get("quantity")
                or item.get("Quantity")
            )
            status = item.get("status") or item.get("Status")

            try:
                if date_time is not None and value is not None:
                    points.append(
                        HourlyConsumptionPoint(
                            date_time=str(date_time),
                            value_kwh=float(value),
                            status=str(status) if status is not None else None,
                        )
                    )
            except (TypeError, ValueError):
                continue

        points.sort(key=lambda p: p.date_time)
        return points

    def _extract_hourly_temperature_points(self, payload: Any) -> list[HourlyTemperaturePoint]:
        """Extract hourly temperature points from API response payload."""
        raw_points: list[dict[str, Any]] = []
        if isinstance(payload, dict) and isinstance(payload.get("temperatures"), list):
            raw_points = [
                item for item in payload["temperatures"] if isinstance(item, dict)
            ]
        else:
            raw_points = self._flatten_points(payload)

        points: list[HourlyTemperaturePoint] = []
        for item in raw_points:
            date_time = item.get("date")
            if date_time is None:
                date_time = item.get("Date")

            value = item.get("value")
            if value is None:
                value = item.get("Value")
            try:
                if date_time is not None and value is not None:
                    points.append(
                        HourlyTemperaturePoint(
                            date_time=str(date_time),
                            value_c=float(value),
                        )
                    )
            except (TypeError, ValueError):
                continue

        points.sort(key=lambda p: p.date_time)
        return points

    def _flatten_points(self, payload: Any) -> list[dict[str, Any]]:
        """Flatten known/unknown payload shapes into a list of dict points."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if not isinstance(payload, dict):
            return []

        direct_keys = (
            "consumption",
            "data",
            "items",
            "values",
            "result",
            "results",
            "timeSeries",
            "timeSeriesValues",
        )
        for key in direct_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

        nested_points: list[dict[str, Any]] = []
        for value in payload.values():
            if isinstance(value, dict):
                nested_points.extend(self._flatten_points(value))
            elif isinstance(value, list):
                nested_points.extend(item for item in value if isinstance(item, dict))

        return nested_points

    def _build_stub_points(self, start_date: date, end_date: date) -> list[ConsumptionPoint]:
        """Build deterministic stub data for development/testing."""
        points: list[ConsumptionPoint] = []
        day = start_date

        while day <= end_date:
            value = 8.0 + ((day.day % 7) * 0.7)
            points.append(ConsumptionPoint(date=day.isoformat(), value_kwh=round(value, 3)))
            day += timedelta(days=1)

        return points

    def _build_stub_hourly_points(
        self, start_date: date, end_date: date
    ) -> list[HourlyConsumptionPoint]:
        """Build deterministic hourly stub data for development/testing."""
        points: list[HourlyConsumptionPoint] = []
        dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time()).replace(
            minute=0, second=0, microsecond=0
        )

        while dt <= end_dt:
            hour_weight = 1.2 if 6 <= dt.hour <= 9 else 0.7 if 0 <= dt.hour <= 5 else 0.9
            value = 0.35 + ((dt.day % 5) * 0.11) + hour_weight
            points.append(
                HourlyConsumptionPoint(
                    date_time=dt.isoformat(),
                    value_kwh=round(value, 3),
                    status="012",
                )
            )
            dt += timedelta(hours=1)

        return points

    def _build_stub_hourly_temperature_points(
        self, start_date: date, end_date: date
    ) -> list[HourlyTemperaturePoint]:
        """Build deterministic hourly temperature stub data for development/testing."""
        points: list[HourlyTemperaturePoint] = []
        dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time()).replace(
            minute=0, second=0, microsecond=0
        )

        while dt <= end_dt:
            # Simple diurnal curve with day-by-day variation.
            base = 3.0 + ((dt.day % 6) * 0.8)
            if 0 <= dt.hour <= 5:
                diurnal = -2.2
            elif 6 <= dt.hour <= 8:
                diurnal = 0.0
            elif 12 <= dt.hour <= 16:
                diurnal = 4.1
            else:
                diurnal = 1.4
            points.append(
                HourlyTemperaturePoint(
                    date_time=dt.isoformat(),
                    value_c=round(base + diurnal, 1),
                )
            )
            dt += timedelta(hours=1)

        return points
