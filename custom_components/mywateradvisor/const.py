"""Constants for the My Water Advisor integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "mywateradvisor"

# Public values shipped in the My Water Advisor 2.0 web app (globals.js / api.js).
BASE_URL = "https://customerportal-api.harmonyencoremdm.com"
APP_ID = "3a869241-d476-40f6-a923-d789d63db11d"

CONF_DEVICE_ID = "device_id"
CONF_UNIT = "unit"
CONF_BACKFILL_DAYS = "backfill_days"
# Per-statistic backfill progress kept in the config entry:
# {statistic_id: "YYYY-MM-DD" (next local day to import) | "done"}
CONF_BACKFILL_STATE = "backfill_state"
BACKFILL_DONE = "done"

# Import history in chunks of this many days, saving progress after each,
# so a long backfill shows up month by month and resumes after an error.
CHUNK_DAYS = 30

DEFAULT_UNIT = "gal"
DEFAULT_BACKFILL_DAYS = 30
MAX_BACKFILL_DAYS = 365

# Units the portal may report in. The integration does not convert values;
# pick the unit your utility's portal shows.
UNIT_OPTIONS = {
    "gal": "Gallons",
    "ft³": "Cubic feet",
    "CCF": "Hundred cubic feet (CCF)",
    "m³": "Cubic meters",
    "L": "Liters",
}

# Re-fetch this many days before the last imported hour on every update, so
# late or corrected readings are picked up.
LOOKBACK_DAYS = 3

# Meter data reaches the portal in batches and often a day or more late, so
# polling more often gains nothing.
UPDATE_INTERVAL = timedelta(hours=4)

# Pause between portal requests during a backfill.
REQUEST_DELAY_SECONDS = 0.3

# Login protection. The portal temporarily locks accounts (seen as HTTP 400
# with correct credentials), so background logins back off 15 min, 30 min,
# 1 h ... up to 6 h after a failure, and never exceed a daily budget. The
# budget sits above one login per poll (6/day) in case tokens are short-lived.
LOGIN_BACKOFF_FIRST = timedelta(minutes=15)
LOGIN_BACKOFF_MAX = timedelta(hours=6)
LOGIN_BUDGET_PER_DAY = 8
# Consecutive 400/403 refusals (spread over ~4 h by the backoff) before the
# user is asked to re-enter the password.
REFUSALS_BEFORE_REAUTH = 4
