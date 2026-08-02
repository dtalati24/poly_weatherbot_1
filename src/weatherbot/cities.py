"""Per-city settlement definitions.

The project began as a London-only model and the London settlement rules were
baked into `config`. They are not universal, and the differences are not
cosmetic -- each one changes the settlement operator:

    EGLC (London)   whole degrees C in the METAR body, reported half-hourly
                    at :20/:50, market priced in C
    KLAX (LA)       tenths of a degree C via the METAR remark T-group, reported
                    hourly at :53, market priced in whole F

So LA has *half* the temporal sampling of London and *ten times* the thermometer
resolution, and the settled value passes through a Celsius-to-Fahrenheit
rounding step London never has. A model that assumed London's operator would be
wrong about LA in two directions at once.

Note that `max` commutes with the Celsius-to-Fahrenheit conversion, because the
conversion is monotonic: `max_i round_F(c_i) == round_F(max_i c_i)`. So taking
the maximum in Celsius and converting once at the end is exact, not an
approximation. What is *not* settled is whether Weather Underground converts
from the T-group tenths or from the whole-degree body value -- those disagree on
some days, and which one is right has to be validated against settled markets
the same way Phase 0 did for London.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class City:
    """Everything city-specific about a daily-maximum temperature market."""

    key: str
    station: str
    name: str
    tz: ZoneInfo
    slug_prefix: str
    unit: str  # "C" or "F" -- the unit the market's buckets are quoted in
    latitude: float
    longitude: float
    # Kalshi series ticker for the same city, where one exists.
    kalshi_series: str | None = None
    # What Kalshi settles on, when it differs from Polymarket's source.
    kalshi_station: str | None = None

    @property
    def settles_in_fahrenheit(self) -> bool:
        return self.unit == "F"

    @property
    def same_station_as_kalshi(self) -> bool:
        """Whether both venues measure the same physical station.

        When true the venues differ only in reporting pipeline, which is a far
        tighter relationship than when they differ in location as well.
        """
        return self.kalshi_station is not None and self.kalshi_station == self.station


LONDON = City(
    key="london",
    station="EGLC",
    name="London City Airport",
    tz=ZoneInfo("Europe/London"),
    slug_prefix="highest-temperature-in-london-on",
    unit="C",
    latitude=51.505,
    longitude=0.055,
)

# Both venues settle on KLAX. Kalshi reads the NWS Climatological Report;
# Polymarket reads Weather Underground. Same thermometer, different pipeline --
# which is what makes LA the clean test of the cross-venue idea.
LOS_ANGELES = City(
    key="los-angeles",
    station="KLAX",
    name="Los Angeles International Airport",
    tz=ZoneInfo("America/Los_Angeles"),
    slug_prefix="highest-temperature-in-los-angeles-on",
    unit="F",
    latitude=33.938,
    longitude=-118.389,
    kalshi_series="KXHIGHLAX",
    kalshi_station="KLAX",
)

# Kept for completeness and explicitly NOT recommended for the cross-venue work:
# Kalshi settles on Central Park, Polymarket on LaGuardia. Those are different
# thermometers several miles apart, and their difference varies with wind
# direction and sea breeze -- a second unknown stacked on top of the first.
NEW_YORK = City(
    key="nyc",
    station="KLGA",
    name="LaGuardia Airport",
    tz=ZoneInfo("America/New_York"),
    slug_prefix="highest-temperature-in-nyc-on",
    unit="F",
    latitude=40.779,
    longitude=-73.880,
    kalshi_series="KXHIGHNY",
    kalshi_station="KNYC",  # Central Park -- deliberately different
)

CITIES: dict[str, City] = {c.key: c for c in (LONDON, LOS_ANGELES, NEW_YORK)}


def get(key: str) -> City:
    if key not in CITIES:
        raise KeyError(f"unknown city {key!r}; have {sorted(CITIES)}")
    return CITIES[key]
