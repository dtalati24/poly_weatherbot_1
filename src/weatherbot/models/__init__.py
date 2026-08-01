"""Forecast models.

Every model produces a `TemperatureDistribution` -- a PMF over integer degrees
Celsius. Market buckets are applied only at the final step, because bucket
structure is not stable across market eras.
"""

from weatherbot.models.climatology import ClimatologyConfig, ClimatologyModel
from weatherbot.models.distribution import TemperatureDistribution

__all__ = ["ClimatologyModel", "ClimatologyConfig", "TemperatureDistribution"]
