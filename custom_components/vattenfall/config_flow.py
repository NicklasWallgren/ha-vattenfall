"""Config flow for Vattenfall integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    VattenfallAccessDeniedError,
    VattenfallApiClient,
    VattenfallApiError,
    VattenfallAuthError,
    async_fetch_subscription_key,
)
from .const import (
    ACCOUNT_TYPE_ELDISTRIBUTION,
    ACCOUNT_TYPE_MIN_ENERGI,
    CONF_ACCOUNT_TYPE,
    CONF_CUSTOMER_ID,
    CONF_METERING_POINT_ID,
    CONF_PASSWORD,
    CONF_SUBSCRIPTION_KEY,
    CONF_TEMPERATURE_AREA_CODE,
    DEFAULT_NAME,
    DEFAULT_TEMPERATURE_AREA_CODE,
    DOMAIN,
    min_energi_unique_id,
)
from .min_energi_api import VattenfallMinEnergiClient

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _without_spaces(user_input: dict[str, Any]) -> dict[str, Any]:
    """Remove spaces from the fields where the API rejects them."""
    return {
        key: "".join(value.split())
        if key in (CONF_METERING_POINT_ID, CONF_TEMPERATURE_AREA_CODE)
        else value
        for key, value in user_input.items()
    }


def _is_min_energi(config: Mapping[str, Any]) -> bool:
    """Return whether settings belong to a Min energi entry."""
    return config.get(CONF_ACCOUNT_TYPE) == ACCOUNT_TYPE_MIN_ENERGI


class VattenfallConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Vattenfall."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow state shared between Min energi steps."""
        self._min_energi_input: dict[str, Any] = {}
        self._premises: dict[str, str] = {}
        self._premise_count = 0

    async def _async_validate(
        self, config: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Try the settings against Vattenfall, returning them completed plus any form errors."""
        if _is_min_energi(config):
            premises, errors = await self._async_validate_min_energi(config)
            premise_id = config.get(CONF_METERING_POINT_ID)
            if not errors and premise_id not in {premise["id"] for premise in premises}:
                # Valid login, but to an account that cannot read this entry's premise.
                errors = {"base": "premise_not_found"}
            return config, errors

        if not config.get(CONF_SUBSCRIPTION_KEY):
            try:
                key = await async_fetch_subscription_key(get_async_client(self.hass))
            except VattenfallApiError:
                return config, {CONF_SUBSCRIPTION_KEY: "subscription_key_not_found"}
            config = {**config, CONF_SUBSCRIPTION_KEY: key}

        errors: dict[str, str] = {}
        client = VattenfallApiClient(hass=self.hass, config=config)
        try:
            await client.async_authenticate(force=True)

            today = date.today()
            yesterday = today - timedelta(days=1)
            await client.async_get_daily_consumption(yesterday, today, allow_stub=False)
        except VattenfallAuthError:
            errors["base"] = "invalid_auth"
        except VattenfallApiError:
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            errors["base"] = "unknown"
        finally:
            await client.async_close()
        return config, errors

    async def _async_validate_min_energi(
        self, config: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Log in to Min energi, returning the account's premises plus any form errors."""
        premises: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        client = VattenfallMinEnergiClient(hass=self.hass, config=config)
        try:
            await client.async_authenticate(force=True)
            premises = await client.async_get_premises()
        except VattenfallAccessDeniedError:
            errors["base"] = "invalid_auth"
        except VattenfallApiError:
            # Includes other login failures, such as outages, rate limits or the geo-block.
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            errors["base"] = "unknown"
        finally:
            await client.async_close()
        return premises, errors

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Let the user pick which Vattenfall service to connect."""
        return self.async_show_menu(
            step_id="user",
            menu_options=[ACCOUNT_TYPE_ELDISTRIBUTION, ACCOUNT_TYPE_MIN_ENERGI],
        )

    async def async_step_eldistribution(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle setup against Vattenfall Eldistribution (grid operator)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = _without_spaces(user_input)
            await self.async_set_unique_id(user_input[CONF_METERING_POINT_ID])
            self._abort_if_unique_id_configured()

            config, errors = await self._async_validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME, DEFAULT_NAME),
                    data={**config, CONF_ACCOUNT_TYPE: ACCOUNT_TYPE_ELDISTRIBUTION},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_CUSTOMER_ID): str,
                vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                vol.Required(CONF_METERING_POINT_ID): str,
                vol.Optional(CONF_SUBSCRIPTION_KEY): str,
                vol.Optional(
                    CONF_TEMPERATURE_AREA_CODE,
                    default=DEFAULT_TEMPERATURE_AREA_CODE,
                ): str,
            }
        )

        return self.async_show_form(
            step_id="eldistribution",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_min_energi(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle login against Vattenfall Min energi (electricity retailer)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            config = {**user_input, CONF_ACCOUNT_TYPE: ACCOUNT_TYPE_MIN_ENERGI}
            premises, errors = await self._async_validate_min_energi(config)
            if not errors:
                if not premises:
                    return self.async_abort(reason="no_premises")
                # Compare with Min energi entries only: an Eldistribution metering
                # point can have the same ID as the premise.
                configured = {
                    entry.data.get(CONF_METERING_POINT_ID)
                    for entry in self._async_current_entries(include_ignore=False)
                    if _is_min_energi(entry.data)
                }
                self._premises = {
                    premise["id"]: self._premise_label(premise)
                    for premise in premises
                    if premise["id"] not in configured
                }
                if not self._premises:
                    return self.async_abort(reason="already_configured")

                self._min_energi_input = config
                self._premise_count = len(premises)
                return await self.async_step_premise()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_CUSTOMER_ID): str,
                vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
            }
        )

        return self.async_show_form(
            step_id="min_energi",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_premise(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Let the user pick which Min energi premise to add."""
        if user_input is not None:
            premise_id = user_input[CONF_METERING_POINT_ID]
            await self.async_set_unique_id(min_energi_unique_id(premise_id))
            self._abort_if_unique_id_configured()

            name = self._min_energi_input.get(CONF_NAME, DEFAULT_NAME)
            # Count every premise on the account, so the title does not depend on setup order.
            title = f"{name} ({self._premises[premise_id]})" if self._premise_count > 1 else name
            return self.async_create_entry(
                title=title,
                data={
                    **self._min_energi_input,
                    CONF_METERING_POINT_ID: premise_id,
                    # Temperature comes per premise, so key its statistics by premise too.
                    CONF_TEMPERATURE_AREA_CODE: premise_id,
                },
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_METERING_POINT_ID, default=next(iter(self._premises))
                ): vol.In(self._premises),
            }
        )
        return self.async_show_form(step_id="premise", data_schema=schema)

    @staticmethod
    def _premise_label(premise: dict[str, Any]) -> str:
        """Build a human-readable label for a premise."""
        parts = [premise.get("name"), premise.get("city")]
        label = ", ".join(str(part) for part in parts if part) or str(premise["id"])
        if premise.get("gridOwner"):
            label += f" – {premise['gridOwner']}"
        return label

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Start re-authentication after Vattenfall rejected the saved login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for new login details."""
        return await self._async_update_entry_step(
            "reauth_confirm", self._get_reauth_entry(), user_input
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Change the settings of an existing entry."""
        entry = self._get_reconfigure_entry()
        if _is_min_energi(entry.data):
            # Min energi reports temperature per premise, so there is no area to change.
            return await self._async_update_entry_step("reconfigure", entry, user_input)

        area_code = entry.data.get(CONF_TEMPERATURE_AREA_CODE, DEFAULT_TEMPERATURE_AREA_CODE)
        return await self._async_update_entry_step(
            "reconfigure",
            entry,
            user_input,
            {vol.Required(CONF_TEMPERATURE_AREA_CODE, default=area_code): str},
        )

    async def _async_update_entry_step(
        self,
        step_id: str,
        entry: config_entries.ConfigEntry,
        user_input: dict[str, Any] | None,
        extra_fields: dict[vol.Marker, Any] | None = None,
    ) -> FlowResult:
        """Show or apply a form that changes an existing entry's settings."""
        errors: dict[str, str] = {}
        min_energi = _is_min_energi(entry.data)

        if user_input is not None:
            if min_energi:
                merged = {**entry.data, **user_input}
            else:
                # A blank key must trigger detection, not fall back to the saved one.
                merged = {
                    **entry.data,
                    CONF_SUBSCRIPTION_KEY: None,
                    **_without_spaces(user_input),
                }
            config, errors = await self._async_validate(merged)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=config)

        fields: dict[vol.Marker, Any] = {
            vol.Required(CONF_CUSTOMER_ID, default=entry.data[CONF_CUSTOMER_ID]): str,
            vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
        }
        if not min_energi:
            fields[vol.Optional(CONF_SUBSCRIPTION_KEY)] = str
        schema = vol.Schema({**fields, **(extra_fields or {})})

        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)
