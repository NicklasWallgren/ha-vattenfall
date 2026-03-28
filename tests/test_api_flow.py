"""Tests for Vattenfall API auth and consumption flow."""

from __future__ import annotations

from dataclasses import dataclass, field
import dataclasses as dataclasses_mod
from datetime import date
from types import ModuleType
import importlib
from pathlib import Path
from urllib.parse import urlsplit
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_dependency_stubs() -> None:
    """Install minimal module stubs required to import integration code."""
    if "aiohttp" not in sys.modules:
        aiohttp_mod = ModuleType("aiohttp")

        class ClientError(Exception):
            pass

        aiohttp_mod.ClientError = ClientError
        sys.modules["aiohttp"] = aiohttp_mod

    if "yarl" not in sys.modules:
        yarl_mod = ModuleType("yarl")

        class URL(str):
            def __new__(cls, value: str):
                return str.__new__(cls, value)

        yarl_mod.URL = URL
        sys.modules["yarl"] = yarl_mod

    if "homeassistant" not in sys.modules:
        homeassistant_pkg = ModuleType("homeassistant")
        sys.modules["homeassistant"] = homeassistant_pkg

    if "homeassistant.core" not in sys.modules:
        core_mod = ModuleType("homeassistant.core")

        class HomeAssistant:
            pass

        core_mod.HomeAssistant = HomeAssistant
        sys.modules["homeassistant.core"] = core_mod

    if "homeassistant.const" not in sys.modules:
        const_mod = ModuleType("homeassistant.const")
        const_mod.CONF_PASSWORD = "password"
        const_mod.Platform = type("Platform", (), {"SENSOR": "sensor"})
        sys.modules["homeassistant.const"] = const_mod

    if "homeassistant.config_entries" not in sys.modules:
        config_entries_mod = ModuleType("homeassistant.config_entries")

        class ConfigEntry:  # pragma: no cover - minimal stub
            pass

        config_entries_mod.ConfigEntry = ConfigEntry
        sys.modules["homeassistant.config_entries"] = config_entries_mod

    if "homeassistant.helpers" not in sys.modules:
        helpers_pkg = ModuleType("homeassistant.helpers")
        sys.modules["homeassistant.helpers"] = helpers_pkg

    if "homeassistant.helpers.aiohttp_client" not in sys.modules:
        aiohttp_client_mod = ModuleType("homeassistant.helpers.aiohttp_client")

        def async_create_clientsession(_hass):
            raise RuntimeError("tests should patch async_create_clientsession")

        aiohttp_client_mod.async_create_clientsession = async_create_clientsession
        sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client_mod

    if "homeassistant.helpers.update_coordinator" not in sys.modules:
        update_coordinator_mod = ModuleType("homeassistant.helpers.update_coordinator")

        class UpdateFailed(Exception):
            pass

        class DataUpdateCoordinator:
            def __init__(self, *args, **kwargs):  # noqa: D401, ANN002, ANN003
                pass

            def __class_getitem__(cls, _item):  # noqa: ANN001
                return cls

        update_coordinator_mod.UpdateFailed = UpdateFailed
        update_coordinator_mod.DataUpdateCoordinator = DataUpdateCoordinator
        sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator_mod


def _patch_dataclass_slots_for_py39() -> None:
    """Allow importing modules that use dataclass(slots=True) on Python 3.9."""
    original_dataclass = dataclasses_mod.dataclass

    def compatible_dataclass(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs.pop("slots", None)
        return original_dataclass(*args, **kwargs)

    dataclasses_mod.dataclass = compatible_dataclass


_install_dependency_stubs()
_patch_dataclass_slots_for_py39()
api = importlib.import_module("custom_components.vattenfall.api")


class FakeCookie:
    """Simple cookie object compatible with what api.py uses from httpx jar."""

    def __init__(
        self,
        name: str,
        value: str,
        domain: str,
        path: str = "/",
        secure: bool = True,
    ) -> None:
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path
        self.secure = secure

    def is_expired(self) -> bool:
        return False


class FakeCookies:
    """Cookie container with a `.jar` iterable and set/clear methods."""

    def __init__(self) -> None:
        self.jar: list[FakeCookie] = []

    def clear(self) -> None:
        self.jar.clear()

    def set(
        self,
        name: str,
        value: str,
        domain: str,
        path: str = "/",
    ) -> None:
        self.jar = [
            c
            for c in self.jar
            if not (c.name == name and c.domain == domain and c.path == path)
        ]
        self.jar.append(FakeCookie(name=name, value=value, domain=domain, path=path))

    def set_cookie(self, url: str, name: str, value: str) -> None:
        host = urlsplit(url).hostname
        if host is None:
            raise AssertionError(f"Invalid URL for cookie: {url}")
        self.set(name=name, value=value, domain=host, path="/")


@dataclass
class FakeResponse:
    """Fake httpx response."""

    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    json_body: object | None = None
    text_body: str = ""
    set_cookies: list[tuple[str, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        class _Headers(dict):
            def __init__(self, source: dict[str, str], set_cookies: list[tuple[str, str, str]]):
                super().__init__(source)
                self._set_cookies = set_cookies

            def get_list(self, key: str) -> list[str]:
                if key.lower() != "set-cookie":
                    return []
                return [f"{name}={value}" for _url, name, value in self._set_cookies]

        self.headers = _Headers(self.headers, self.set_cookies)

    @property
    def status_code(self) -> int:
        return self.status

    @property
    def text(self) -> str:
        return self.text_body

    def json(self):
        return self.json_body

class FakeHttpxClient:
    """Fake HTTP client with deterministic route responses."""

    def __init__(self, routes: dict[tuple[str, str], list[FakeResponse]]) -> None:
        self._routes = {key: list(responses) for key, responses in routes.items()}
        self.cookies = FakeCookies()
        self.calls: list[tuple[str, str, dict]] = []

    async def aclose(self) -> None:
        return None

    async def _request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        key = (method, url)
        if key not in self._routes or not self._routes[key]:
            raise AssertionError(f"Unexpected {method} call to {url}")

        response = self._routes[key].pop(0)
        for cookie_url, cookie_name, cookie_value in response.set_cookies:
            self.cookies.set_cookie(cookie_url, cookie_name, cookie_value)

        # Simulate cookies set directly in request kwargs.
        for cookie_name, cookie_value in (kwargs.get("cookies") or {}).items():
            host = urlsplit(url).hostname
            if host:
                self.cookies.set(cookie_name, cookie_value, domain=host, path="/")

        return response

    async def get(self, url: str, **kwargs) -> FakeResponse:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> FakeResponse:
        return await self._request("POST", url, **kwargs)


def _auth_routes() -> dict[tuple[str, str], list[FakeResponse]]:
    auth_start_url = api.DEFAULT_AUTH_START_URL
    authorize_url = (
        "https://accounts.vattenfall.com/iamng/seb2c//dso/oauth2/authorize?client_id=test"
    )
    oauth2_url = (
        "https://accounts.vattenfall.com/iamng/seb2c/dso/oauth2/authorize"
        "?sessionDataKey=nonce-value"
    )
    callback_url = (
        "https://services.vattenfalleldistribution.se/auth/login/authcallback"
        "?code=abc&state=xyz"
    )

    return {
        ("GET", auth_start_url): [
            FakeResponse(status=302, headers={"Location": authorize_url})
        ],
        ("GET", authorize_url): [
            FakeResponse(
                status=200,
                set_cookies=[
                    (
                        "https://accounts.vattenfall.com",
                        "sessionNonceCookie-nonce-value",
                        "cookie-data",
                    )
                ],
            )
        ],
        ("POST", "https://accounts.vattenfall.com/iamng/seb2c/dso/commonauth"): [
            FakeResponse(status=302, headers={"Location": oauth2_url})
        ],
        ("GET", oauth2_url): [
            FakeResponse(status=302, headers={"Location": callback_url})
        ],
        ("GET", callback_url): [
            FakeResponse(
                status=200,
                set_cookies=[
                    (
                        "https://services.vattenfalleldistribution.se",
                        "VF_SecurityCookie",
                        "security-cookie",
                    ),
                    (
                        "https://services.vattenfalleldistribution.se",
                        "VF_AccessCookie",
                        "access-cookie",
                    ),
                ],
            )
        ],
    }


def _base_config() -> dict[str, object]:
    return {
        "metering_point_id": "HDG123456789",
        "customer_id": "123456789",
        "password": "secret",
        "subscription_key": "sub-key",
        "allow_stub_data": False,
    }


class TestVattenfallApiFlow(unittest.IsolatedAsyncioTestCase):
    """Validate API auth + consumption control flow."""

    async def test_authenticate_follows_redirect_chain_and_sets_cookies(self) -> None:
        session = FakeHttpxClient(_auth_routes())
        config = _base_config()

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = api.VattenfallApiClient(hass=object(), config=config)
            await client.async_authenticate(force=True)

        self.assertTrue(client._has_auth_cookies)

        post_call = next(c for c in session.calls if c[0] == "POST")
        post_data = post_call[2]["data"]
        self.assertEqual(post_data["customerId"], "123456789")
        self.assertEqual(post_data["password"], "secret")
        self.assertEqual(post_data["sessionDataKey"], "nonce-value")

    async def test_get_daily_consumption_returns_points(self) -> None:
        routes = _auth_routes()
        consumption_url = (
            "https://services.vattenfalleldistribution.se/consumption/consumption/"
            "HDG123456789/2026-03-01/2026-03-02/Daily/Measured"
        )
        routes[("GET", consumption_url)] = [
            FakeResponse(
                status=200,
                json_body=[
                    {"date": "2026-03-02", "value": 12.0},
                    {"date": "2026-03-01", "value": 10.5},
                ],
            )
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = api.VattenfallApiClient(hass=object(), config=_base_config())
            points = await client.async_get_daily_consumption(
                date(2026, 3, 1),
                date(2026, 3, 2),
            )

        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].date, "2026-03-01")
        self.assertEqual(points[0].value_kwh, 10.5)
        self.assertEqual(points[1].date, "2026-03-02")
        self.assertEqual(points[1].value_kwh, 12.0)

        last_call = session.calls[-1]
        self.assertEqual(last_call[0], "GET")
        self.assertEqual(last_call[1], consumption_url)
        self.assertEqual(last_call[2]["headers"]["ocp-apim-subscription-key"], "sub-key")

    async def test_get_hourly_consumption_returns_points(self) -> None:
        routes = _auth_routes()
        consumption_url = (
            "https://services.vattenfalleldistribution.se/consumption/consumption/"
            "HDG123456789/2026-03-01/2026-03-02/Hourly/Measured?includeLoad=true"
        )
        routes[("GET", consumption_url)] = [
            FakeResponse(
                status=200,
                json_body={
                    "startDate": "2026-03-01T00:00:00",
                    "endDate": "2026-03-02T00:00:00",
                    "aggregationInterval": "Hourly",
                    "consumption": [
                        {
                            "date": "2026-03-01T01:00:00",
                            "consumption": 1.2,
                            "status": "012",
                        },
                        {
                            "date": "2026-03-01T00:00:00",
                            "consumption": 0.8,
                            "status": "012",
                        },
                    ],
                },
            )
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = api.VattenfallApiClient(hass=object(), config=_base_config())
            points = await client.async_get_hourly_consumption(
                date(2026, 3, 1),
                date(2026, 3, 2),
                include_load=True,
            )

        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].date_time, "2026-03-01T00:00:00")
        self.assertEqual(points[0].value_kwh, 0.8)
        self.assertEqual(points[1].date_time, "2026-03-01T01:00:00")
        self.assertEqual(points[1].value_kwh, 1.2)
        self.assertEqual(points[1].status, "012")

        last_call = session.calls[-1]
        self.assertEqual(last_call[0], "GET")
        self.assertEqual(last_call[1], consumption_url)
        self.assertEqual(last_call[2]["headers"]["ocp-apim-subscription-key"], "sub-key")

    async def test_get_daily_consumption_reauths_after_unauthorized(self) -> None:
        routes = _auth_routes()
        for key, values in _auth_routes().items():
            routes.setdefault(key, []).extend(values)

        consumption_url = (
            "https://services.vattenfalleldistribution.se/consumption/consumption/"
            "HDG123456789/2026-03-01/2026-03-01/Daily/Measured"
        )
        routes[("GET", consumption_url)] = [
            FakeResponse(status=401, text_body="unauthorized"),
            FakeResponse(status=200, json_body=[{"date": "2026-03-01", "value": 8.2}]),
        ]

        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = api.VattenfallApiClient(hass=object(), config=_base_config())
            points = await client.async_get_daily_consumption(
                date(2026, 3, 1),
                date(2026, 3, 1),
            )

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].value_kwh, 8.2)

        post_calls = [call for call in session.calls if call[0] == "POST"]
        self.assertEqual(len(post_calls), 2, "Expected two auth attempts (initial + retry)")

    async def test_authenticate_raises_when_session_nonce_cookie_missing(self) -> None:
        auth_start_url = api.DEFAULT_AUTH_START_URL
        authorize_url = "https://accounts.vattenfall.com/iamng/seb2c//dso/oauth2/authorize?client_id=test"
        routes = {
            ("GET", auth_start_url): [
                FakeResponse(status=302, headers={"Location": authorize_url})
            ],
            ("GET", authorize_url): [FakeResponse(status=200)],
        }
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = api.VattenfallApiClient(hass=object(), config=_base_config())
            with self.assertRaises(api.VattenfallAuthError):
                await client.async_authenticate(force=True)


if __name__ == "__main__":
    unittest.main()
