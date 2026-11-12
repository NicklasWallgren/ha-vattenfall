"""Tests for the Vattenfall Min energi API client."""

from __future__ import annotations

from datetime import date, datetime
import importlib
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_api_flow import FakeHttpxClient, FakeResponse, api

min_energi_api = importlib.import_module("custom_components.vattenfall.min_energi_api")
const = importlib.import_module("custom_components.vattenfall.const")

AUTHORIZE_URL = "https://accounts.vattenfall.se/iamng/seb2c/sales/oauth2/authorize?client_id=test"
LOGIN_PAGE_URL = "https://accounts.vattenfall.se/iamng/seb2c/sales/web/#/?sessionDataKey=nonce-value"
COMMONAUTH_URL = "https://accounts.vattenfall.se/iamng/seb2c/sales/commonauth"
OAUTH2_URL = "https://accounts.vattenfall.se/iamng/seb2c/sales/oauth2/authorize?sessionDataKey=nonce-value"
CALLBACK_URL = "https://www.vattenfall.se/signin-oidc?code=abc&state=xyz"
MINA_SIDOR_URL = "https://www.vattenfall.se/mina-sidor"
API = "https://selfserviceapi.www.vattenfall.se/elements"
PREMISE_ID = "SKR123456789"
RANGES_URL = f"{API}/my-energy/premises/{PREMISE_ID}/measurement-ranges"

# 2026-10-22 00:00 UTC
DAY_EPOCH = 1792627200
LATER = datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Europe/Stockholm"))


def _login_routes(logins: int = 1) -> dict[tuple[str, str], list[FakeResponse]]:
    def times(response_factory):
        return [response_factory() for _ in range(logins)]

    return {
        ("GET", const.MIN_ENERGI_AUTH_START_URL): times(
            lambda: FakeResponse(status=302, headers={"Location": AUTHORIZE_URL})
        ),
        ("GET", AUTHORIZE_URL): times(
            lambda: FakeResponse(
                status=302,
                headers={"Location": LOGIN_PAGE_URL},
                set_cookies=[
                    ("https://accounts.vattenfall.se", "sessionNonceCookie-nonce-value", "nonce")
                ],
            )
        ),
        ("GET", LOGIN_PAGE_URL): times(lambda: FakeResponse(status=200)),
        ("POST", COMMONAUTH_URL): times(
            lambda: FakeResponse(status=302, headers={"Location": OAUTH2_URL})
        ),
        ("GET", OAUTH2_URL): times(
            lambda: FakeResponse(status=302, headers={"Location": CALLBACK_URL})
        ),
        ("GET", CALLBACK_URL): times(
            lambda: FakeResponse(
                status=302,
                headers={"Location": "/mina-sidor"},
                set_cookies=[
                    ("https://.www.vattenfall.se", ".VFIdToken", "id-token"),
                    ("https://.www.vattenfall.se", ".VFState", "state"),
                ],
            )
        ),
        ("GET", MINA_SIDOR_URL): times(lambda: FakeResponse(status=200)),
        ("GET", RANGES_URL): [_ranges(DAY_EPOCH + 3 * 86400)],
    }


def _ranges(last_available_epoch: int, first_available_epoch: int | None = None) -> FakeResponse:
    return FakeResponse(
        json_body=[
            {
                "resolution": resolution,
                "measurementType": measurement_type,
                "firstAvailableMeasurementDate": first_available_epoch,
                "lastAvailableMeasurementDate": last_available_epoch,
            }
            for resolution in ("QuarterHour", "Hourly", "Daily")
            for measurement_type in ("ElectricityConsumption", "Temperature")
        ]
    )


def _measurement_url(resolution: str, measurement_type: str, start: int, end: int) -> str:
    return (
        f"{API}/my-energy/premises/{PREMISE_ID}/measurement"
        f"?resolution={resolution}&measurementType={measurement_type}"
        f"&startDate={start}&endDate={end}"
    )


def _item(day: int, hour: int, value: float | None, minute: int | None = None) -> dict:
    item = {"year": 2026, "month": 10, "day": day, "hour": hour}
    if minute is not None:
        item["minute"] = minute
    item["measurement"] = None if value is None else {"value": value, "unit": "KWH"}
    return item


def _config() -> dict[str, object]:
    return {
        "account_type": "min_energi",
        "metering_point_id": PREMISE_ID,
        "customer_id": "123456789",
        "password": "secret",
        "temperature_area_code": PREMISE_ID,
    }


class TestMinEnergiApi(unittest.IsolatedAsyncioTestCase):
    """Validate Min energi auth and measurement parsing."""

    def setUp(self) -> None:
        now_patch = patch.object(min_energi_api.VattenfallMinEnergiClient, "_now", return_value=LATER)
        self.mock_now = now_patch.start()
        self.addCleanup(now_patch.stop)

    async def test_authenticate_follows_redirects_and_posts_credentials(self) -> None:
        session = FakeHttpxClient(_login_routes())

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            await client.async_authenticate(force=True)

        self.assertTrue(client._has_auth_cookies)
        post_call = next(c for c in session.calls if c[0] == "POST")
        self.assertEqual(
            post_call[2]["data"],
            {
                "customerId": "123456789",
                "password": "secret",
                "auth_method": "customerid_password",
                "tenantDomain": "se.b2c",
                "sessionDataKey": "nonce-value",
            },
        )

    async def test_authenticate_follows_html_form_handover(self) -> None:
        authorize_endpoint = "https://accounts.vattenfall.se/iamng/seb2c/sales/oauth2/authorize"
        routes = _login_routes()
        routes[("GET", const.MIN_ENERGI_AUTH_START_URL)] = [
            FakeResponse(
                status=200,
                text_body=(
                    '<html><body onload="document.forms[0].submit()">'
                    f'<form method="post" action="{authorize_endpoint}">'
                    '<input type="hidden" name="client_id" value="web" />'
                    '<input type="hidden" name="state" value="a&amp;b" />'
                    "</form></body></html>"
                ),
            )
        ]
        routes[("POST", authorize_endpoint)] = [
            FakeResponse(
                status=302,
                headers={"Location": LOGIN_PAGE_URL},
                set_cookies=[
                    ("https://accounts.vattenfall.se", "sessionNonceCookie-nonce-value", "nonce")
                ],
            )
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            await client.async_authenticate(force=True)

        self.assertTrue(client._has_auth_cookies)
        form_post = next(c for c in session.calls if c[1] == authorize_endpoint)
        self.assertEqual(form_post[2]["data"], {"client_id": "web", "state": "a&b"})

    def test_host_only_cookies_do_not_reach_api_host(self) -> None:
        session = FakeHttpxClient({})
        session.cookies.set(".VFState", "fresh-state", domain=".www.vattenfall.se")
        session.cookies.set(".VFIdToken", "id-token", domain=".www.vattenfall.se")
        session.cookies.set(".VFState", "stale-state", domain="www.vattenfall.se")
        client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
        client._client = session

        self.assertEqual(
            client._cookies_for_url(f"{API}/my-premises-select"),
            {".VFState": "fresh-state", ".VFIdToken": "id-token"},
        )
        self.assertEqual(
            client._cookies_for_url("https://www.vattenfall.se/mina-sidor")[".VFState"], "stale-state"
        )

    def test_client_side_redirect_detects_meta_refresh_and_js(self) -> None:
        base = "https://www.vattenfall.se/iam/se/login"
        meta = '<meta http-equiv="refresh" content="0;url=/next?a=1&amp;b=2">'
        script = '<script>window.location.replace("https:\\/\\/accounts.vattenfall.se\\/x");</script>'

        self.assertEqual(
            min_energi_api._client_side_redirect(meta, base),
            ("GET", "https://www.vattenfall.se/next?a=1&b=2", None),
        )
        self.assertEqual(
            min_energi_api._client_side_redirect(script, base),
            ("GET", "https://accounts.vattenfall.se/x", None),
        )
        self.assertIsNone(min_energi_api._client_side_redirect("<p>hello</p>", base))

    async def test_authenticate_reports_invalid_credentials(self) -> None:
        failure_url = "https://accounts.vattenfall.se/iamng/seb2c/sales/web/?authFailure=true"
        routes = _login_routes()
        routes[("POST", COMMONAUTH_URL)] = [
            FakeResponse(status=302, headers={"Location": failure_url})
        ]
        routes[("GET", failure_url)] = [FakeResponse(status=200)]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            with self.assertRaisesRegex(api.VattenfallAuthError, "Invalid Vattenfall credentials"):
                await client.async_authenticate(force=True)

    async def test_hourly_consumption_parses_items_and_skips_missing_values(self) -> None:
        routes = _login_routes()
        routes[("GET", _measurement_url("Hourly", "ElectricityConsumption", DAY_EPOCH, DAY_EPOCH))] = [
            FakeResponse(
                json_body={
                    "resolution": "Hourly",
                    "items": [_item(22, 1, 0.5), _item(22, 0, 1.25), _item(22, 2, None)],
                }
            )
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            points = await client.async_get_hourly_consumption(date(2026, 10, 22), date(2026, 10, 22))

        self.assertEqual(
            [(p.date_time, p.value_kwh) for p in points],
            [("2026-10-22T00:00:00", 1.25), ("2026-10-22T01:00:00", 0.5)],
        )
        api_call = session.calls[-1]
        self.assertEqual(api_call[2]["cookies"], {".VFIdToken": "id-token", ".VFState": "state"})

    async def test_daily_consumption_and_temperature(self) -> None:
        next_day = DAY_EPOCH + 86400
        routes = _login_routes()
        routes[("GET", _measurement_url("Daily", "ElectricityConsumption", DAY_EPOCH, next_day))] = [
            FakeResponse(json_body={"items": [_item(23, 0, 9.5), _item(22, 0, 12.0)]})
        ]
        routes[("GET", _measurement_url("Hourly", "Temperature", DAY_EPOCH, DAY_EPOCH))] = [
            FakeResponse(json_body={"items": [_item(22, 5, -1.5)]})
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            daily = await client.async_get_daily_consumption(date(2026, 10, 22), date(2026, 10, 23))
            temperature = await client.async_get_hourly_temperature(
                date(2026, 10, 22), date(2026, 10, 22)
            )

        self.assertEqual(
            [(p.date, p.value_kwh) for p in daily], [("2026-10-22", 12.0), ("2026-10-23", 9.5)]
        )
        self.assertEqual(
            [(p.date_time, p.value_c) for p in temperature], [("2026-10-22T05:00:00", -1.5)]
        )

    async def test_quarter_hour_consumption(self) -> None:
        routes = _login_routes()
        routes[("GET", _measurement_url("QuarterHour", "ElectricityConsumption", DAY_EPOCH, DAY_EPOCH))] = [
            FakeResponse(
                json_body={
                    "items": [_item(22, 13, 0.25, minute=15), _item(22, 13, 0.5, minute=0)]
                }
            )
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            points = await client.async_get_quarter_hour_consumption(
                date(2026, 10, 22), date(2026, 10, 22)
            )

        self.assertTrue(client.supports_quarter_hour)
        self.assertEqual(
            [(p.date_time, p.value_kwh) for p in points],
            [("2026-10-22T13:00:00", 0.5), ("2026-10-22T13:15:00", 0.25)],
        )

    async def test_requested_range_is_clamped_to_available_days(self) -> None:
        routes = _login_routes()
        routes[("GET", RANGES_URL)] = [_ranges(DAY_EPOCH, first_available_epoch=DAY_EPOCH - 86400)]
        routes[("GET", _measurement_url("Hourly", "ElectricityConsumption", DAY_EPOCH - 86400, DAY_EPOCH))] = [
            FakeResponse(json_body={"items": [_item(21, 23, 0.4), _item(22, 23, 0.9)]})
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            points = await client.async_get_hourly_consumption(date(2026, 10, 1), date(2026, 10, 23))

        self.assertEqual([p.value_kwh for p in points], [0.4, 0.9])

    async def test_long_daily_range_is_requested_per_calendar_month(self) -> None:
        jan_15 = 1768435200  # 2026-01-15 00:00 UTC
        day = 86400
        routes = _login_routes()
        routes[("GET", _measurement_url("Daily", "ElectricityConsumption", jan_15, jan_15 + 16 * day))] = [
            FakeResponse(json_body={"items": [_item(15, 0, 1.0) | {"month": 1}]})
        ]
        routes[("GET", _measurement_url("Daily", "ElectricityConsumption", jan_15 + 17 * day, jan_15 + 26 * day))] = [
            FakeResponse(json_body={"items": [_item(1, 0, 2.0) | {"month": 2}]})
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            points = await client.async_get_daily_consumption(date(2026, 1, 15), date(2026, 2, 10))

        self.assertEqual([(p.date, p.value_kwh) for p in points], [("2026-01-15", 1.0), ("2026-02-01", 2.0)])

    async def test_rejected_range_is_split_in_half(self) -> None:
        start = DAY_EPOCH - 3 * 86400  # 2026-10-19
        routes = _login_routes()
        routes[("GET", _measurement_url("Hourly", "ElectricityConsumption", start, DAY_EPOCH))] = [
            FakeResponse(status=400, text_body='{"title":"Unsuccessful call towards VIP!","status":400}')
        ]
        routes[("GET", _measurement_url("Hourly", "ElectricityConsumption", start, start + 86400))] = [
            FakeResponse(json_body={"items": [_item(19, 0, 0.1)]})
        ]
        routes[("GET", _measurement_url("Hourly", "ElectricityConsumption", start + 2 * 86400, DAY_EPOCH))] = [
            FakeResponse(json_body={"items": [_item(22, 0, 0.2)]})
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            points = await client.async_get_hourly_consumption(date(2026, 10, 19), date(2026, 10, 22))

        self.assertEqual([p.value_kwh for p in points], [0.1, 0.2])

    async def test_rejected_single_day_raises(self) -> None:
        routes = _login_routes()
        routes[("GET", _measurement_url("Hourly", "ElectricityConsumption", DAY_EPOCH, DAY_EPOCH))] = [
            FakeResponse(status=400, text_body="bad request")
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            with self.assertRaisesRegex(api.VattenfallApiError, "HTTP 400"):
                await client.async_get_hourly_consumption(date(2026, 10, 22), date(2026, 10, 22))

    async def test_periods_that_have_not_ended_are_dropped(self) -> None:
        self.mock_now.return_value = datetime(2026, 10, 22, 1, 30, tzinfo=ZoneInfo("Europe/Stockholm"))
        routes = _login_routes()
        routes[("GET", _measurement_url("Hourly", "ElectricityConsumption", DAY_EPOCH, DAY_EPOCH))] = [
            FakeResponse(json_body={"items": [_item(22, 0, 0.4), _item(22, 1, 0.0), _item(22, 2, 0.0)]})
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            points = await client.async_get_hourly_consumption(date(2026, 10, 22), date(2026, 10, 22))

        self.assertEqual([(p.date_time, p.value_kwh) for p in points], [("2026-10-22T00:00:00", 0.4)])

    async def test_unauthorized_response_triggers_relogin(self) -> None:
        routes = _login_routes(logins=2)
        routes[("GET", _measurement_url("Hourly", "ElectricityConsumption", DAY_EPOCH, DAY_EPOCH))] = [
            FakeResponse(status=401, text_body="expired"),
            FakeResponse(json_body={"items": [_item(22, 0, 0.75)]}),
        ]
        session = FakeHttpxClient(routes)

        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=_config())
            points = await client.async_get_hourly_consumption(date(2026, 10, 22), date(2026, 10, 22))

        self.assertEqual([p.value_kwh for p in points], [0.75])
        self.assertEqual(sum(1 for c in session.calls if c[0] == "POST"), 2)

    async def test_get_premises(self) -> None:
        premises_url = (
            f"{API}/my-premises-select?includeFutureContracts=true&contractTypeFilter=Heat,Battery"
            "&excludeContractTypes=true&combineMicroProductionPremises=true&combineBatteryPremises=false"
        )
        routes = _login_routes()
        routes[("GET", premises_url)] = [
            FakeResponse(
                json_body=[
                    {"id": PREMISE_ID, "name": "Home", "city": "Uppsala", "gridOwner": "Ellevio AB"},
                    {"name": "missing id"},
                ]
            )
        ]
        session = FakeHttpxClient(routes)

        # The config flow lists premises before one is chosen, so no metering point ID yet.
        config = {key: value for key, value in _config().items() if key != "metering_point_id"}
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = min_energi_api.VattenfallMinEnergiClient(hass=object(), config=config)
            premises = await client.async_get_premises()

        self.assertEqual([p["id"] for p in premises], [PREMISE_ID])

    def test_create_api_client_selects_by_account_type(self) -> None:
        self.assertIsInstance(
            min_energi_api.create_api_client(hass=object(), config=_config()),
            min_energi_api.VattenfallMinEnergiClient,
        )
        eldistribution = min_energi_api.create_api_client(
            hass=object(),
            config={
                "metering_point_id": "HDG1",
                "customer_id": "1",
                "password": "x",
                "subscription_key": "k",
            },
        )
        self.assertIs(type(eldistribution), api.VattenfallApiClient)


if __name__ == "__main__":
    unittest.main()
