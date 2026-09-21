"""Unit-safe probability conversion; scheduling is not enabled yet."""

import math


def activity_probability(rate_per_hour: float, elapsed_seconds: float) -> float:
    if not math.isfinite(rate_per_hour) or rate_per_hour < 0:
        raise ValueError("rate_per_hour must be finite and non-negative")
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("elapsed_seconds must be finite and non-negative")
    return -math.expm1(-rate_per_hour * (elapsed_seconds / 3600))
