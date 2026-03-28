"""Config flow for Vattenfall integration."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .api import VattenfallApiClient, VattenfallApiError, VattenfallAuthError
from .const import (
    CONF_ALLOW_STUB_DATA,
    CONF_CUSTOMER_ID,
    CONF_METERING_POINT_ID,
    CONF_PASSWORD,
    CONF_SUBSCRIPTION_KEY,
    CONF_TEMPERATURE_AREA_CODE,
    DEFAULT_NAME,
    DEFAULT_TEMPERATURE_AREA_CODE,
    DOMAIN,
)

class VattenfallConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Vattenfall."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_METERING_POINT_ID])
            self._abort_if_unique_id_configured()

            client = VattenfallApiClient(hass=self.hass, config=user_input)
            try:
                await client.async_authenticate(force=True)

                today = date.today()
                yesterday = today - timedelta(days=1)
                await client.async_get_daily_consumption(yesterday, today)
            except VattenfallAuthError:
                if not user_input.get(CONF_ALLOW_STUB_DATA, False):
                    errors["base"] = "invalid_auth"
            except VattenfallApiError:
                if not user_input.get(CONF_ALLOW_STUB_DATA, False):
                    errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                errors["base"] = "unknown"
            finally:
                await client.async_close()

            if not errors:
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME, DEFAULT_NAME),
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_CUSTOMER_ID): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_METERING_POINT_ID): str,
                vol.Required(CONF_SUBSCRIPTION_KEY): str,
                vol.Optional(
                    CONF_TEMPERATURE_AREA_CODE,
                    default=DEFAULT_TEMPERATURE_AREA_CODE,
                ): str,
                vol.Optional(CONF_ALLOW_STUB_DATA, default=False): bool,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
