"""Project-wide constants.

Single source of truth for the station, the resolution rules, and endpoints.
Nothing here should be duplicated elsewhere in the codebase.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

# --- Resolution station -------------------------------------------------
# Polymarket London temperature markets resolve on Wunderground's reading for
# the London City Airport station. NOT Heathrow (EGLL).
STATION_ICAO = "EGLC"
STATION_NAME = "London City Airport"
STATION_LAT = 51.505
STATION_LON = 0.055
STATION_ELEV_M = 6

# Neighbouring stations, used for spatial-context features and as a sanity
# check. EGLL is the station most generic "London" forecasts implicitly mean,
# so the EGLC-EGLL spread is itself a modelling feature.
NEIGHBOUR_STATIONS = ("EGLL", "EGKK", "EGSS", "EGWU")

# --- Timezones ----------------------------------------------------------
# The market rules say "all times on this day" without naming a timezone.
# Which day boundary Polymarket actually uses is an open Phase 0 question,
# so both are tested. See weatherbot.resolve.
LOCAL_TZ = ZoneInfo("Europe/London")
UTC = ZoneInfo("UTC")

# --- Endpoints ----------------------------------------------------------
IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
GAMMA_API = "https://gamma-api.polymarket.com"

# Weather Underground is the actual settlement source. Its ingest occasionally
# drops individual METARs, so its daily maximum is not always equal to the raw
# METAR maximum -- see sources/wunderground.py. Train and validate on this.
WU_HISTORY_URL = (
    "https://api.weather.com/v1/location/{station_key}/observations/historical.json"
)
WU_STATION_KEY = "EGLC:9:GB"
# Public key embedded in the wunderground.com site, not a private credential.
# It may rotate without notice; IEM is the durable fallback archive.
WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"

# IEM report_type codes. 3 = routine METAR, 4 = SPECI (special/off-cycle).
# Whether Wunderground's daily max includes SPECIs is a Phase 0 question, so
# both are pulled and the distinction is preserved in the raw METAR text.
REPORT_TYPE_ROUTINE = 3
REPORT_TYPE_SPECIAL = 4

# Fields requested from IEM. `metar` (raw text) is kept so report type and
# encoding conventions can be audited after the fact.
IEM_FIELDS = ("tmpc", "dwpc", "drct", "sknt", "gust", "skyc1", "vsby", "metar")

# --- Market structure ---------------------------------------------------
# London daily-high markets present 11 mutually exclusive ordered buckets.
# Edges shift seasonally, so they are read per-market, never hardcoded.
N_MARKET_BUCKETS = 11

MARKET_SLUG_PREFIX = "highest-temperature-in-london-on"

# --- Paths --------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
OBS_DIR = RAW_DIR / "observations"
MARKETS_DIR = RAW_DIR / "markets"
PROCESSED_DIR = DATA_DIR / "processed"


def ensure_dirs() -> None:
    """Create the data directory tree if it does not already exist."""
    for d in (DATA_DIR, RAW_DIR, OBS_DIR, MARKETS_DIR, PROCESSED_DIR):
        d.mkdir(parents=True, exist_ok=True)
