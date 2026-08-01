"""The shared observation record.

Both the IEM METAR archive and Weather Underground normalise into this type so
`weatherbot.resolve` can run over either without knowing the difference. Which
source you use matters: see the module docstring in sources/wunderground.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Observation:
    """A single weather observation, timestamped in UTC.

    Temperatures are degrees Celsius. EGLC METARs encode temperature as a whole
    number of degrees natively, so `tmpc` is integer-valued in practice even
    though it is typed as a float.
    """

    station: str
    valid_utc: datetime
    tmpc: float | None
    dwpc: float | None = None
    drct: float | None = None
    # Knots. Left as None for Weather Underground, whose metric feed reports
    # wind speed in km/h -- filling this from that feed would be a unit bug.
    sknt: float | None = None
    gust: float | None = None
    skyc1: str | None = None
    vsby: float | None = None
    metar: str | None = None
    source: str = "unknown"

    @property
    def is_special(self) -> bool:
        """True if the raw report is a SPECI rather than a routine METAR.

        Only meaningful for sources that carry raw METAR text. EGLC reports are
        automated (AUTO) and routine, so this is False throughout in practice.
        """
        return bool(self.metar) and self.metar.lstrip().startswith("SPECI")
