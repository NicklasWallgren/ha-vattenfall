"""Constants for the Vattenfall integration."""

from datetime import timedelta

from homeassistant.const import CONF_PASSWORD as HA_CONF_PASSWORD

DOMAIN = "vattenfall"
PLATFORMS = ["sensor"]

CONF_METERING_POINT_ID = "metering_point_id"
CONF_CUSTOMER_ID = "customer_id"
CONF_PASSWORD = HA_CONF_PASSWORD
CONF_SUBSCRIPTION_KEY = "subscription_key"
CONF_ALLOW_STUB_DATA = "allow_stub_data"

DEFAULT_NAME = "Vattenfall"
DEFAULT_SCAN_INTERVAL = timedelta(hours=1)
DEFAULT_BASE_URL = "https://services.vattenfalleldistribution.se"
DEFAULT_AUTH_START_URL = (
    "https://services.vattenfalleldistribution.se/auth/login"
    "?returnUrl=https%3a%2f%2fwww.vattenfalleldistribution.se%2flogga-in%2f"
)

ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_POINTS = "points"
