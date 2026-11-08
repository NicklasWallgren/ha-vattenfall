"""Config flow for Vattenfall integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import VattenfallApiClient, VattenfallApiError, VattenfallAuthError
from .const import (
    CONF_CUSTOMER_ID,
    CONF_METERING_POINT_ID,
    CONF_PASSWORD,
    CONF_SUBSCRIPTION_KEY,
    CONF_TEMPERATURE_AREA_CODE,
    DEFAULT_NAME,
    DEFAULT_TEMPERATURE_AREA_CODE,
    DOMAIN,
)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class VattenfallConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Vattenfall."""

    VERSION = 1

    async def _async_validate(self, config: dict[str, Any]) -> dict[str, str]:
        """Try the given settings against Vattenfall and return any form errors."""
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
        return errors

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_METERING_POINT_ID])
            self._abort_if_unique_id_configured()

            errors = await self._async_validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME, DEFAULT_NAME),
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_CUSTOMER_ID): str,
                vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                vol.Required(CONF_METERING_POINT_ID): str,
                vol.Required(CONF_SUBSCRIPTION_KEY): str,
                vol.Optional(
                    CONF_TEMPERATURE_AREA_CODE,
                    default=DEFAULT_TEMPERATURE_AREA_CODE,
                ): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

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

        if user_input is not None:
            errors = await self._async_validate({**entry.data, **user_input})
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_CUSTOMER_ID, default=entry.data[CONF_CUSTOMER_ID]): str,
                vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                vol.Required(
                    CONF_SUBSCRIPTION_KEY, default=entry.data[CONF_SUBSCRIPTION_KEY]
                ): str,
                **(extra_fields or {}),
            }
        )

        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)
