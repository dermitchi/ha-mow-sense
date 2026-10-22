"""Constants for Mow Sense."""

import logging

DOMAIN = "mow_sense"
LOGGER = logging.getLogger(__package__)

SERVICE_UUID = "98bd0001-0b0e-421a-84e5-ddbf75dc6de4"

CONF_CHANNEL_ID = "channel_id"
CONF_SCHEDULE_ENTITY = "schedule_entity"
CONF_ACTIVE_INTERVAL = "active_interval"
CONF_IDLE_INTERVAL = "idle_interval"
CONF_GRACE_MINUTES = "grace_minutes"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_RAIN_LEAD = "rain_lead_minutes"
CONF_RAIN_DRY = "rain_dry_minutes"
CONF_MIN_RUN = "min_run_minutes"

DEFAULT_ACTIVE_INTERVAL = 60  # s, while the mower is outside
DEFAULT_IDLE_INTERVAL = 300  # s, while it sits in the station
DEFAULT_GRACE_MINUTES = 30  # keep last values this long without contact
RETRY_INTERVAL = 120  # s, after a failed connection / out of range
DEFAULT_MOW_HOURS = 3.0
DEFAULT_BLADE_HOURS = 100  # cutting hours between blade changes (no official Gardena value)

SLOW_EVERY = 1800  # s, statistics / clock / last message
TASKS_EVERY = 600  # s, re-check the mower's schedule
MAX_SCHEDULE_ATTEMPTS = 3
CLOCK_TOLERANCE = 120  # s

EVENT_CORRECTED = f"{DOMAIN}_corrected"

DEFAULT_RAIN_LEAD = 30  # min: be home this long before forecast rain
DEFAULT_RAIN_DRY = 60  # min: let the grass dry after rain
DEFAULT_MIN_RUN = 45  # min: shorter runs are not worth starting
FORECAST_EVERY = 900  # s
