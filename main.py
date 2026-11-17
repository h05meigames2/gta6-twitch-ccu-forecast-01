"""Forecast Grand Theft Auto VI's launch-day Twitch viewers, hour by hour.

Method (original): learn the shape of a big launch day from the case's own
comparables -- past launches' hourly Twitch category averages. For every
comparable launch, take its release-day curve as log-ratios to its hour-1
print, ln(v_h / v_1). Two facts the data shows drive the fit:

1. Launch days share a shape: a dip in the small hours, then a rise into a
   broad evening peak (hours 18-22 UTC), then a slow fall.
2. The size of hour 1 predicts how much room the day has left to rise:
   across the comparables, corr(ln v_1, ln peak ratio) is about -0.94. A
   launch whose first hour is already enormous stays flat; one whose first
   hour is small can still grow many-fold as the big markets unlock.

So for each hour h = 2..24 we fit, over the comparable launches, a robust
(Theil-Sen) line of ln(v_h / v_1) against ln(v_1), and read it off at the
case's own hour-1 print. The five quantiles are the fitted value plus the
empirical quantiles of that hour's residuals -- winsorised at +-2 log units
and shrunk halfway toward the fit, because the raw cross-launch spread is
dominated by a few explosive outliers and overstates the typical miss.
A 3-hour moving average smooths the fitted curve and the offsets.

Only launches the kit marks as clean analogues are used (the tier the kit
records in each comparable's `unlock` field): early-access-contaminated
launches peaked days before their release day, free-to-play / slow-burn
launches are not paid-launch analogues, and the target game's own
pre-release category history is not a launch day at all.

The program reads one case file per run from $MAKEFUN_INPUT_DIR (else
/input) and writes the forecast to $MAKEFUN_OUTPUT_DIR (else /output).
Python 3.13, standard library only, no network, deterministic: every
collection is sorted before it is used, and no randomness anywhere.
"""

import json
import math
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path

# The grader's -P leaves this file's directory OFF sys.path: put it back
# first, so a module shipped beside this file imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]

# Fit controls, chosen on the four practice launches (see README):
#   residual winsor (log units), residual shrink toward the fit,
#   fit clamp (log units), hours of smoothing either side.
WINSOR = 2.0
SHRINK = 0.5
FIT_LO, FIT_HI = -1.5, 3.5


def round2(x: float):
    """Round to 2 decimals, half up, as JavaScript's Math.round(x*100)/100.

    A whole number comes back as an int, so json.dumps writes 1240 where
    JavaScript writes 1240 (not 1240.0).
    """
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value.is_integer() else value


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def tier_of(comp: dict) -> str:
    """The analogue tier the kit records in a comparable's `unlock` field."""
    note = comp.get("unlock", "") or ""
    if "Tier: target-history" in note:
        return "target-history"
    if "Tier: early-access-contaminated" in note:
        return "early-access-contaminated"
    if "Tier: weak" in note:
        return "weak"
    return "comparable"


def launch_day_curve(comp: dict) -> dict:
    """{hour offset: viewers} for a comparable's release day, hours -24..24.

    `hourly` points are whole-hour means; an hour the source did not sample
    has no point and is simply absent. Hour 1 is [day_start, day_start+1h).
    """
    start = parse_iso(comp["day_start"])
    curve = {}
    for point in comp.get("hourly") or []:
        viewers = point.get("viewers")
        if viewers is None:
            continue
        when = parse_iso(point["from"])
        hour = int((when - start).total_seconds() // 3600) + 1
        if -24 <= hour <= 24:
            curve[hour] = viewers
    return curve


def theil_sen(points):
    """Robust line fit: slope = median pairwise slope, intercept = median
    residual. Resistant to the odd launch whose hour 1 was a fluke."""
    slopes = []
    n = len(points)
    for i in range(n):
        xi, yi = points[i]
        for j in range(i + 1, n):
            dx = points[j][0] - xi
            if abs(dx) > 1e-9:
                slopes.append((points[j][1] - yi) / dx)
    slope = statistics.median(slopes) if slopes else 0.0
    intercept = statistics.median([y - slope * x for x, y in points])
    return intercept, slope


def quantile_sorted(values, q: float) -> float:
    """Linear-interpolation quantile of an already-sorted list."""
    if not values:
        return 0.0
    pos = q * (len(values) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


def forecast_case(case: dict) -> dict:
    if case["levels"] != LEVELS:
        raise ValueError(f"levels {case['levels']} are not {LEVELS}")
    first = next((k for k in case["known"] if k["hour"] == 1), None)
    if first is None:
        raise ValueError("case has no hour-1 print")
    p1 = max(1.0, float(first["viewers"]))
    log_p1 = math.log(p1)

    # Per hour: (log hour-1 print, log ratio to hour 1) over clean analogues.
    # An analogue only counts if its release day was actually sampled: a
    # curve with only a handful of the day's 24 hours is mostly gaps, and
    # its ratios would be noise. (Every clean analogue in the practice
    # cases clears this bar, so it changes nothing there; it guards the
    # final, whose set adds four more launches.)
    points = {h: [] for h in range(2, 25)}
    log_firsts = []
    for comp in sorted(case.get("comparables") or [],
                       key=lambda c: (c.get("name", ""), c.get("day_start", ""))):
        if tier_of(comp) != "comparable":
            continue
        curve = launch_day_curve(comp)
        if 1 not in curve or curve[1] <= 0:
            continue
        if sum(1 for h in range(1, 25) if h in curve) < 20:
            continue
        log_v1 = math.log(curve[1])
        log_firsts.append(log_v1)
        for h in range(2, 25):
            if h in curve and curve[h] > 0:
                points[h].append((log_v1, math.log(curve[h] / curve[1])))
    if not log_firsts:
        # No usable analogue at all: fall back to the flat persistence
        # forecast with a widening spread (the house baseline's shape).
        return {"hours": [{"hour": h, "q": [round2(p1 * math.exp(z * 0.15 * math.sqrt(h - 1)))
                                             for z in (-1.645, -0.674, 0.0, 0.674, 1.645)]}
                          for h in range(2, 25)]}
    # Never extrapolate the size effect past the biggest / smallest analogue.
    log_at = min(max(log_p1, min(log_firsts)), max(log_firsts))

    fits, offsets = {}, {}
    for h in range(2, 25):
        data = points[h]
        if len(data) < 5:
            fits[h], offsets[h] = 0.0, [0.0] * 5
            continue
        intercept, slope = theil_sen(data)
        fit = min(max(intercept + slope * log_at, FIT_LO), FIT_HI)
        residuals = sorted(min(max(y - (intercept + slope * x), -WINSOR), WINSOR)
                           for x, y in data)
        offsets[h] = [quantile_sorted(residuals, tau) * (1.0 - SHRINK)
                      for tau in LEVELS]
        fits[h] = fit

    # Smooth the fitted curve and the offsets over neighbouring hours: a
    # launch day's shape moves slowly, and per-hour fits are noisy.
    hours = list(range(2, 25))
    smooth_fit = {}
    smooth_off = {}
    for i, h in enumerate(hours):
        near = hours[max(0, i - 1):i + 2]
        smooth_fit[h] = sum(fits[g] for g in near) / len(near)
        smooth_off[h] = [sum(offsets[g][k] for g in near) / len(near)
                         for k in range(5)]

    rows = []
    for h in hours:
        qs = [p1 * math.exp(smooth_fit[h] + off) for off in smooth_off[h]]
        # Enforce the contract: finite, > 0, <= 1e9, non-decreasing.
        qs = [min(max(q, 0.01), 1e9) for q in qs]
        for k in range(1, 5):
            qs[k] = max(qs[k], qs[k - 1])
        rows.append({"hour": h, "q": [round2(q) for q in qs]})
    return {"hours": rows}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Every case file, in name order, so two runs write the same bytes.
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
