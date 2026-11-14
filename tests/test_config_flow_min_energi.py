"""Tests for the Min energi parts of the config flow."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_api_flow import api  # noqa: F401  # installs Home Assistant stubs

PREMISE_ID = "SKR123456789"
OTHER_PREMISE_ID = "SKR987654321"


class AbortFlow(Exception):
    """Raised like Home Assistant's AbortFlow when a unique ID is already configured."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _install_config_flow_stubs() -> None:
    """Add the Home Assistant pieces config_flow.py needs on top of the shared stubs."""

    class ConfigFlow:
        def __init_subclass__(cls, domain=None, **kwargs):  # noqa: ANN001, ANN003
            super().__init_subclass__(**kwargs)

        def async_show_menu(self, **kwargs):  # noqa: ANN003
            return {"type": "menu", **kwargs}

        def async_show_form(self, **kwargs):  # noqa: ANN003
            return {"type": "form", **kwargs}

        def async_abort(self, reason):  # noqa: ANN001
            return {"type": "abort", "reason": reason}

        def async_create_entry(self, title, data):  # noqa: ANN001
            return {"type": "create_entry", "title": title, "data": data}

        def async_update_reload_and_abort(self, entry, data):  # noqa: ANN001
            return {"type": "abort", "reason": "updated", "data": data}

        async def async_set_unique_id(self, unique_id):  # noqa: ANN001
            self.unique_id = unique_id

        def _abort_if_unique_id_configured(self):
            if self.unique_id in self._async_current_ids():
                raise AbortFlow("already_configured")

        def _async_current_entries(self, include_ignore=True):  # noqa: ANN001
            return self.current_entries

        def _async_current_ids(self, include_ignore=True):  # noqa: ANN001
            return {entry.unique_id for entry in self.current_entries}

        def _get_reauth_entry(self):
            return self.entry

        def _get_reconfigure_entry(self):
            return self.entry

    config_entries = sys.modules["homeassistant.config_entries"]
    config_entries.ConfigFlow = ConfigFlow
    sys.modules["homeassistant.const"].CONF_NAME = "name"

    data_entry_flow = ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict
    sys.modules.setdefault("homeassistant.data_entry_flow", data_entry_flow)

    httpx_client = ModuleType("homeassistant.helpers.httpx_client")
    httpx_client.get_async_client = lambda hass: None
    sys.modules.setdefault("homeassistant.helpers.httpx_client", httpx_client)

    selector = ModuleType("homeassistant.helpers.selector")
    selector.TextSelector = lambda config: ("text", config)
    selector.TextSelectorConfig = lambda **kwargs: kwargs
    selector.TextSelectorType = SimpleNamespace(PASSWORD="password")
    sys.modules.setdefault("homeassistant.helpers.selector", selector)


_install_config_flow_stubs()
config_flow = importlib.import_module("custom_components.vattenfall.config_flow")
integration = importlib.import_module("custom_components.vattenfall")


# The shared voluptuous stubs do not take defaults; schemas only need to build here.
_VOL = SimpleNamespace(
    Schema=lambda schema: schema,
    Required=lambda key, default=None: ("required", key),
    Optional=lambda key, default=None: ("optional", key),
    In=lambda values: values,
)


class _FakeMinEnergiClient:
    """Stands in for the Min energi client; set the class attributes per test."""

    login_error: Exception | None = None
    premises: list[dict] = []

    def __init__(self, hass, config):  # noqa: ANN001
        self.config = config

    async def async_authenticate(self, force=False):  # noqa: ANN001
        if self.login_error is not None:
            raise self.login_error

    async def async_get_premises(self):
        return self.premises

    async def async_close(self):
        return None


def _entry(
    unique_id: str,
    account_type: str | None,
    metering_point_id: str,
    version: int = 1,
    minor_version: int = 2,
) -> SimpleNamespace:
    data = {"metering_point_id": metering_point_id}
    if account_type:
        data["account_type"] = account_type
    return SimpleNamespace(
        entry_id="entry-1",
        unique_id=unique_id,
        data=data,
        version=version,
        minor_version=minor_version,
    )


def _min_energi_entry() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="entry-1",
        unique_id=f"min_energi_{PREMISE_ID}",
        version=1,
        minor_version=2,
        data={
            "account_type": "min_energi",
            "name": "Vattenfall",
            "customer_id": "123456789",
            "password": "old",
            "metering_point_id": PREMISE_ID,
            "temperature_area_code": PREMISE_ID,
        }
    )


class TestMinEnergiConfigFlow(unittest.IsolatedAsyncioTestCase):
    """Validate setup, reauth and reconfigure for Min energi entries."""

    def setUp(self) -> None:
        _FakeMinEnergiClient.login_error = None
        _FakeMinEnergiClient.premises = [{"id": PREMISE_ID, "name": "Home", "city": "Uppsala"}]
        client_patch = patch.object(config_flow, "VattenfallMinEnergiClient", _FakeMinEnergiClient)
        vol_patch = patch.object(config_flow, "vol", _VOL)
        for active in (client_patch, vol_patch):
            active.start()
            self.addCleanup(active.stop)

    def _flow(self, current_entries=()) -> config_flow.VattenfallConfigFlow:  # noqa: ANN001
        flow = config_flow.VattenfallConfigFlow()
        flow.hass = object()
        flow.current_entries = list(current_entries)
        flow.entry = _min_energi_entry()
        return flow

    async def test_reauth_rejects_account_without_the_configured_premise(self) -> None:
        _FakeMinEnergiClient.premises = [{"id": OTHER_PREMISE_ID}]

        result = await self._flow().async_step_reauth_confirm(
            {"customer_id": "555", "password": "new"}
        )

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "premise_not_found"})

    async def test_reauth_saves_login_for_account_with_the_configured_premise(self) -> None:
        result = await self._flow().async_step_reauth_confirm(
            {"customer_id": "123456789", "password": "new"}
        )

        self.assertEqual(result["reason"], "updated")
        self.assertEqual(result["data"]["password"], "new")
        self.assertEqual(result["data"]["metering_point_id"], PREMISE_ID)

    async def test_reconfigure_checks_the_configured_premise_too(self) -> None:
        _FakeMinEnergiClient.premises = []

        result = await self._flow().async_step_reconfigure(
            {"customer_id": "555", "password": "new"}
        )

        self.assertEqual(result["errors"], {"base": "premise_not_found"})

    async def test_rejected_credentials_map_to_invalid_auth(self) -> None:
        _FakeMinEnergiClient.login_error = api.VattenfallAccessDeniedError("bad password")

        result = await self._flow().async_step_min_energi(
            {"name": "Vattenfall", "customer_id": "1", "password": "x"}
        )

        self.assertEqual(result["errors"], {"base": "invalid_auth"})

    async def test_other_login_failures_map_to_cannot_connect(self) -> None:
        _FakeMinEnergiClient.login_error = api.VattenfallAuthError("auth init failed with HTTP 503")

        result = await self._flow().async_step_min_energi(
            {"name": "Vattenfall", "customer_id": "1", "password": "x"}
        )

        self.assertEqual(result["errors"], {"base": "cannot_connect"})

    async def test_title_names_the_premise_whenever_the_account_has_several(self) -> None:
        _FakeMinEnergiClient.premises = [
            {"id": PREMISE_ID, "name": "Home", "city": "Uppsala"},
            {"id": OTHER_PREMISE_ID, "name": "Cabin", "city": "Mora"},
        ]
        # The first premise is already set up, so only the second one is offered.
        flow = self._flow(current_entries=[_min_energi_entry()])

        form = await flow.async_step_min_energi(
            {"name": "Vattenfall", "customer_id": "1", "password": "x"}
        )
        result = await flow.async_step_premise({"metering_point_id": OTHER_PREMISE_ID})

        self.assertEqual(form["step_id"], "premise")
        self.assertEqual(result["title"], "Vattenfall (Cabin, Mora)")

    async def test_eldistribution_entry_with_the_same_id_does_not_block_min_energi(self) -> None:
        # Eldistribution can use the very same ID for the metering point as Min energi
        # uses for the premise.
        flow = self._flow(current_entries=[_entry(PREMISE_ID, None, PREMISE_ID)])

        form = await flow.async_step_min_energi(
            {"name": "Vattenfall", "customer_id": "1", "password": "x"}
        )
        result = await flow.async_step_premise({"metering_point_id": PREMISE_ID})

        self.assertEqual(form["step_id"], "premise")
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(flow.unique_id, f"min_energi_{PREMISE_ID}")

    async def test_configured_min_energi_premise_is_not_offered_again(self) -> None:
        # Entries from 0.14.0 still have the bare premise ID until they are set up again.
        legacy = _entry(PREMISE_ID, "min_energi", PREMISE_ID)

        result = await self._flow(current_entries=[legacy]).async_step_min_energi(
            {"name": "Vattenfall", "customer_id": "1", "password": "x"}
        )

        self.assertEqual(result, {"type": "abort", "reason": "already_configured"})

    async def test_min_energi_entry_does_not_block_eldistribution(self) -> None:
        flow = self._flow(current_entries=[_min_energi_entry()])
        flow._async_validate = AsyncMock(side_effect=lambda config: (config, {}))

        result = await flow.async_step_eldistribution(
            {"customer_id": "1", "password": "x", "metering_point_id": PREMISE_ID}
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(flow.unique_id, PREMISE_ID)

    async def test_title_is_the_name_for_a_single_premise_account(self) -> None:
        flow = self._flow()

        await flow.async_step_min_energi({"name": "Vattenfall", "customer_id": "1", "password": "x"})
        result = await flow.async_step_premise({"metering_point_id": PREMISE_ID})

        self.assertEqual(result["title"], "Vattenfall")


class TestMinEnergiUniqueIdMigration(unittest.IsolatedAsyncioTestCase):
    """Entries created before 1.2 move Min energi entries to the prefixed unique ID."""

    async def _migrate(self, entry: SimpleNamespace) -> tuple[bool, list[dict]]:
        updates: list[dict] = []
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_update_entry=lambda entry, **changes: updates.append(changes)
            )
        )
        return await integration.async_migrate_entry(hass, entry), updates

    async def test_legacy_min_energi_entry_gets_its_own_unique_id(self) -> None:
        entry = _entry(PREMISE_ID, "min_energi", PREMISE_ID, minor_version=1)

        self.assertEqual(
            await self._migrate(entry),
            (True, [{"unique_id": f"min_energi_{PREMISE_ID}", "minor_version": 2}]),
        )

    async def test_eldistribution_entry_keeps_its_unique_id(self) -> None:
        entry = _entry(PREMISE_ID, None, PREMISE_ID, minor_version=1)

        self.assertEqual(
            await self._migrate(entry), (True, [{"unique_id": PREMISE_ID, "minor_version": 2}])
        )

    async def test_current_entry_is_left_alone(self) -> None:
        self.assertEqual(await self._migrate(_min_energi_entry()), (True, []))

    async def test_entry_from_a_newer_version_is_refused(self) -> None:
        entry = _entry(PREMISE_ID, "min_energi", PREMISE_ID, version=2)

        self.assertEqual(await self._migrate(entry), (False, []))


if __name__ == "__main__":
    unittest.main()
