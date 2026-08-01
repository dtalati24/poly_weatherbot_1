"""Days where settlement cannot be reproduced from either observation feed.

Polymarket settles against Weather Underground's record *as it stood at
resolution time*. That snapshot is not recoverable after the fact: WU's archive
can lose observations later, and its history endpoint has at least one date
where it returns a broken block. So neither the raw METAR record (IEM) nor
today's WU snapshot reproduces settlement perfectly.

Measured over 2025-01-01 to 2026-08-01, on 504 markets with a pinned outcome:

    IEM raw METAR      503/504   99.80%
    WU current archive 502/504   99.60%

The three disagreeing days are enumerated below with what actually happened.
This list is a *gate*, not an excuse: `scripts/validate_resolve.py` passes only
if every mismatch it finds is on this list. A new unexplained mismatch fails the
gate and must be investigated before anything is trained or traded.

Trading implication: the residual is not noise to be ignored. On days where the
two feeds disagree, the settled value is genuinely uncertain between them, and
that disagreement is visible in real time. Those days warrant a widened
distribution and reduced size -- and anyone pricing off a single feed is
carrying an unhedged one-bucket error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class SettlementAnomaly:
    """A day where at least one observation feed disagrees with settlement."""

    day: date
    settled: str
    iem_max_c: int
    wu_max_c: int
    matched: str  # which feed agreed with settlement
    note: str


KNOWN_SETTLEMENT_ANOMALIES: dict[date, SettlementAnomaly] = {
    anomaly.day: anomaly
    for anomaly in (
        SettlementAnomaly(
            day=date(2025, 3, 30),
            settled="63-64F",
            iem_max_c=17,
            wu_max_c=11,
            matched="iem",
            note=(
                "WU's history endpoint returns a broken 2-observation block for "
                "this date (00:20Z and 00:50Z only). IEM has the full 46 "
                "observations peaking at 17C. "
                "CORRECTED (Phase 5): this IS a DST bug. The original note "
                "called it isolated on the strength of two spring-forward days; "
                "widening to five shows 2022-03-27, 2024-03-31 and 2025-03-30 "
                "all return exactly 2 observations, truncated at the 01:00Z "
                "transition, and only 2026-03-29 is clean. 2023-03-26 is also "
                "truncated but escapes the mismatch list because its maximum "
                "happened to fall in the first two observations. Reproducible "
                "on re-fetch, so it is a permanent archive defect. Fall-back "
                "Sundays are also affected more mildly: WU returns 46 "
                "observations against IEM's 50, silently dropping the repeated "
                "local hour. Consequence: WU-derived labels are wrong by 3-6C "
                "on spring-forward Sundays and must not be trained on -- screen "
                "on block size (< 40 observations) and fall back to IEM."
            ),
        ),
        SettlementAnomaly(
            day=date(2026, 5, 12),
            settled="17C",
            iem_max_c=17,
            wu_max_c=16,
            matched="iem",
            note=(
                "WU is missing four METARs including the 13:50Z daily peak, so "
                "its archived maximum is 16C. Settlement took 17C, implying WU "
                "still had the peak observation at resolution time and lost it "
                "afterwards."
            ),
        ),
        SettlementAnomaly(
            day=date(2026, 5, 27),
            settled="24C",
            iem_max_c=25,
            wu_max_c=24,
            matched="wunderground",
            note=(
                "WU is missing the 23:20Z observation (25C, which fell at 00:20 "
                "local on the 27th under BST). Settlement took 24C, WU's value. "
                "The mirror image of 2026-05-12: here the gap was already "
                "present at resolution time."
            ),
        ),
    )
}


def is_known_anomaly(day: date) -> bool:
    return day in KNOWN_SETTLEMENT_ANOMALIES


def describe(day: date) -> str:
    anomaly = KNOWN_SETTLEMENT_ANOMALIES.get(day)
    return anomaly.note if anomaly else "unexplained"
