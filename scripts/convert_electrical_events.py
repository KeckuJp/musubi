#!/usr/bin/env python3
"""Normalized electrical observations + declared limits -> observed condition rows.

Reads the `observations.csv` the adopted electrical converter already produces; never a
vendor format, never a device, never a command. Reports that a **declared** limit was
crossed under a **declared** window — not a cause, a fault or a safe-limit certification.

The four electrical selectors below are this module's own. The crossing, run, window,
hysteresis, gap and provenance implementation is also reused by another passive caller that
supplies its own closed `domain` table (`convert_link_level_events.py`), so that no second
copy of this algorithm exists; the default `domain` keeps every electrical declaration,
message and output exactly as it was.
"""
import argparse
import csv
import hashlib
import io
import json
import math
import re
from pathlib import Path

try:
    from scripts.convert_position_csv import unique_object, invalid_constant
except ModuleNotFoundError:
    from convert_position_csv import unique_object, invalid_constant

LIMIT = 16 * 1024 * 1024
MAX_ROWS = 200_000
MAX_ENTRIES = 64
MAX_EVALUATIONS = 4096
NUMERIC_TEXT = re.compile(r"\A-?(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,9})?\Z")
# Microseconds are whole numbers and are kept as exact Python integers: a float would round
# past 2**53 and would silently truncate a fractional timestamp into a different instant.
INTEGER_TEXT = re.compile(r"\A-?(?:0|[1-9][0-9]{0,18})\Z")
# The common reader accepts a time cell of `0 ..= i64::MAX` (parsed as i64, then u64).
MAX_SOURCE_TIME_US = 2**63 - 1
NAME_TEXT = re.compile(r"\A[A-Za-z][A-Za-z0-9_]{0,63}\Z")

# Quantity -> (source column, declared unit, optional availability column and its sentinel).
# The columns and units are the ones the adopted converter already emits; nothing is
# converted or rescaled here, because a unit mismatch is a refusal rather than a guess.
QUANTITY = {
    "voltage": ("battery_voltage_v", "V", None),
    "current": ("battery_current_a", "A", None),
    "temperature": ("battery_temperature_k", "K", ("battery_temperature_state",
                                                   "ZERO_OR_UNAVAILABLE")),
    # A declared remaining-charge fraction as the adopted converter reports it. It is not a
    # calibrated state of charge and not a voltage proxy.
    "remaining_charge_fraction": ("battery_remaining_fraction", "fraction", None),
}
CONDITION_QUANTITY = {
    "voltage_sag": "voltage",
    "overcurrent": "current",
    "thermal_over_limit": "temperature",
    "low_charge": "remaining_charge_fraction",
}
# A sag must be entered to be observed; a pack that is already low is low.
DOWNWARD_CONDITIONS = {"voltage_sag", "low_charge"}
# The boundary is declared, never assumed.
COMPARISON = {
    "below": lambda value, limit: value < limit,
    "at_or_below": lambda value, limit: value <= limit,
    "above": lambda value, limit: value > limit,
    "at_or_above": lambda value, limit: value >= limit,
}
CROSSES_DOWNWARD = {"below", "at_or_below"}
SIGN_HANDLING = ("signed_as_recorded", "magnitude")
CLOCK_BASES = ("unknown", "boot_relative", "host_received", "gps_locked")
SIGN_BASIS_COLUMN = "battery_current_sign_basis"
# A composite instance key joins its declared parts with this character, and a cell containing
# it is refused, so the joined form can never be read two ways.
INSTANCE_SEPARATOR = "|"
# A sag is a transition into the low band, so it needs a run and a baseline before it.
MINIMUM_SAG_SAMPLES = 2

MET = "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET"
NOT_SUSTAINED = "CROSSED_LIMIT_NOT_SUSTAINED"
NOT_CROSSED = "NO_SAMPLE_CROSSED_LIMIT"
NO_TRANSITION = "CROSSED_LIMIT_WITHOUT_OBSERVED_TRANSITION"
UNKNOWN = "UNKNOWN_INSUFFICIENT_EVIDENCE"

CLOCK_ORDER_OK = "STRICTLY_INCREASING"
CLOCK_ORDER_DUPLICATE = "AMBIGUOUS_DUPLICATE_TIMESTAMPS"
CLOCK_ORDER_BACKWARDS = "BACKWARDS_OR_RESTARTED"

COLUMNS = [
    "record_time_us", "evaluation_anchor_basis", "source_observations_sha256",
    "declared_limits_sha256", "instance_column", "instance_value", "condition",
    "condition_status", "unknown_reason",
    "quantity", "quantity_column", "declared_unit", "declared_limit", "declared_comparison",
    "declared_release_limit", "declared_window_kind", "declared_window_value",
    "declared_max_gap_us", "declared_sign_handling", "declared_sign_basis",
    "observed_sign_basis",
    "evaluated_sample_count", "usable_sample_count", "unusable_sample_count",
    "crossing_sample_count", "run_sample_count", "run_start_time_us", "run_end_time_us",
    "run_span_us", "run_extreme_value", "transition_evidence",
    "source_clock_basis", "source_clock_order", "duration_basis",
    "threshold_declared_by", "threshold_declared_basis", "threshold_authority",
    "interpretation_basis",
]

INTERPRETATION = ("observed_against_declared_limit_not_a_cause_fault_or_safe_limit")
ELECTRICAL_REUSE = ("consumes the normalized observations of the adopted electrical converter; "
                    "no vendor format is parsed, no device is contacted, no command is emitted")
# The quantities this evaluation is willing to compare, and what a declared condition names.
# A caller cannot widen these: an unlisted condition is refused. A second passive caller may
# supply its OWN closed table of the same shape instead of copying the crossing, run, window
# and hysteresis implementation below -- which is why this is a parameter with a default and
# not a new engine. Everything outside these three tables is quantity-independent already.
ELECTRICAL_DOMAIN = {
    "quantities": QUANTITY,
    "conditions": CONDITION_QUANTITY,
    "downward": DOWNWARD_CONDITIONS,
    "interpretation": INTERPRETATION,
    "reuse": ELECTRICAL_REUSE,
    "failure": "electrical condition evaluation failed; no condition is claimed",
    "input_help": "normalized electrical observations.csv",
}
THRESHOLD_AUTHORITY = "OPERATOR_DECLARED_NOT_CERTIFIED"


def numeric(text, what):
    """A finite measured value; blank is not zero and is handled by the caller."""
    if isinstance(text, bool) or not isinstance(text, (int, float, str)):
        raise ValueError("invalid " + what)
    if isinstance(text, str):
        if not NUMERIC_TEXT.match(text):
            raise ValueError("invalid " + what)
        return float(text)
    if not math.isfinite(text):
        raise ValueError("invalid " + what)
    return float(text)


def microseconds(text, what):
    """An exact integer microsecond instant the common reader would also accept.

    `musubi_decoded_csv::parse` reads the time cell as `i64` and then `u64::try_from`, so its
    range is `0 ..= i64::MAX` and anything else fails with "time must be a nonnegative
    integer". A timestamp outside that is refused here rather than carried into output the
    reader would reject. No float, so nothing rounds or truncates either.
    """
    if type(text) is not bool and isinstance(text, int):
        value = text
    elif isinstance(text, str) and INTEGER_TEXT.match(text):
        value = int(text)
    else:
        raise ValueError("non-integer " + what)
    if not 0 <= value <= MAX_SOURCE_TIME_US:
        raise ValueError(what + " outside the common reader's nonnegative 64-bit range")
    return value


def whole(value, what, maximum=2**63 - 1):
    if type(value) is bool or type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid " + what)
    return value


def checked_entry(entry, domain=ELECTRICAL_DOMAIN):
    """One declared limit. Every meaning is stated by the operator; nothing is defaulted."""
    if not isinstance(entry, dict):
        raise ValueError("each declared limit is an object")
    allowed = {"condition", "unit", "column", "comparison", "limit", "release_limit",
               "min_samples", "min_duration_us", "max_gap_us", "sign_handling", "sign_basis",
               "instance_value"}
    if not set(entry) <= allowed or not {"condition", "unit", "comparison", "limit"} <= set(entry):
        raise ValueError("declared limit has unknown or missing keys")
    condition = entry["condition"]
    if condition not in domain["conditions"]:
        raise ValueError("unknown declared condition")
    quantity = domain["conditions"][condition]
    column, unit, _ = domain["quantities"][quantity]
    if entry["unit"] != unit:
        # Units are matched, never converted: a millivolt limit is not a volt limit.
        raise ValueError("declared unit does not match the normalized column unit")
    declared_column = entry.get("column")
    if "column" in entry:
        # Present and unusable is not the same as absent: an explicit null declares something
        # this bridge cannot honour, so it is refused rather than quietly ignored.
        # Another adopted producer may carry the same quantity under its own column name. The
        # caller states which column, never an alias table and never a guess; the unit above
        # still has to be this quantity's unit, so naming a column authorises no conversion.
        if not isinstance(declared_column, str) or not NAME_TEXT.fullmatch(declared_column):
            raise ValueError("invalid declared quantity column")
        column = declared_column
    comparison = entry["comparison"]
    if comparison not in COMPARISON:
        raise ValueError("unknown declared comparison")
    downward = comparison in CROSSES_DOWNWARD
    if downward != (condition in domain["downward"]):
        # A sag and a low charge cross downward; overcurrent and thermal cross upward.
        raise ValueError("declared comparison direction contradicts the condition")
    limit = numeric(entry["limit"], "declared limit")
    # The bound belongs to the DECLARED UNIT, not to one quantity's name: a fraction limit
    # outside 0..1 is not a fraction, whichever quantity carries it, and would otherwise make
    # every reading cross it. `remaining_charge_fraction` is the only electrical quantity whose
    # unit is `fraction`, so every existing declaration is checked exactly as before.
    if unit == "fraction" and not 0 <= limit <= 1:
        raise ValueError("declared fraction limit outside 0..1")
    release = entry.get("release_limit")
    if release is not None:
        release = numeric(release, "declared release limit")
        if unit == "fraction" and not 0 <= release <= 1:
            raise ValueError("declared fraction release limit outside 0..1")
        # Hysteresis releases on the safe side of the limit, never past it.
        if (release < limit) if downward else (release > limit):
            raise ValueError("declared release limit is on the wrong side of the limit")
    if ("min_samples" in entry) == ("min_duration_us" in entry):
        raise ValueError("declare exactly one of min_samples or min_duration_us")
    max_gap = entry.get("max_gap_us")
    if max_gap is not None:
        max_gap = whole(max_gap, "max_gap_us")
    if "min_samples" in entry:
        window_kind, window_value = "min_samples", whole(entry["min_samples"], "min_samples")
        if window_value < 1:
            raise ValueError("invalid min_samples")
    else:
        window_kind = "min_duration_us"
        window_value = whole(entry["min_duration_us"], "min_duration_us")
        if window_value < 1:
            raise ValueError("invalid min_duration_us")
        if max_gap is None:
            # A span between two samples says nothing about the time between them unless the
            # caller declares how far apart samples may be. Physical continuity is not
            # invented here, so a duration claim must bound its own gaps.
            raise ValueError("a min_duration_us claim must also declare max_gap_us")
    if condition == "voltage_sag" and window_kind == "min_samples" \
            and window_value < MINIMUM_SAG_SAMPLES:
        # One sample below a limit is a low reading, not an observed sag.
        raise ValueError("a voltage_sag window needs at least two samples")
    sign_handling = entry.get("sign_handling")
    sign_basis = entry.get("sign_basis")
    if quantity == "current":
        if sign_handling not in SIGN_HANDLING:
            raise ValueError("a current limit must declare its sign handling")
    elif sign_handling is not None:
        raise ValueError("sign handling belongs to a current limit")
    if "sign_basis" in entry:
        # An operator statement of what the recorded sign means, never a measurement. It is
        # only usable where the sign is actually compared, so it is refused anywhere else
        # rather than carried as configuration that does nothing.
        if quantity != "current" or sign_handling != "signed_as_recorded":
            raise ValueError("a declared sign basis belongs to a signed_as_recorded current limit")
        if not isinstance(sign_basis, str) or not NAME_TEXT.fullmatch(sign_basis):
            raise ValueError("invalid declared sign basis")
    instance, arity = entry.get("instance_value"), 0
    if "instance_value" in entry:
        # One declared instance column takes a string; a composite key takes one string per
        # declared column, in the declared order. A mismatch is refused, never guessed.
        values = [instance] if isinstance(instance, str) else instance
        if not isinstance(values, list) or not values \
                or any(not isinstance(item, str) or not 1 <= len(item) <= 128
                       or INSTANCE_SEPARATOR in item for item in values):
            raise ValueError("invalid declared instance value")
        arity = len(values)
        instance = INSTANCE_SEPARATOR.join(values)
    return {"condition": condition, "quantity": quantity, "column": column, "unit": unit,
            "comparison": comparison, "limit": limit, "release_limit": release,
            "window_kind": window_kind, "window_value": window_value, "max_gap_us": max_gap,
            "sign_handling": sign_handling, "sign_basis": sign_basis, "instance_value": instance,
            "instance_arity": arity, "declared_column": declared_column}


def checked_limits(document, domain=ELECTRICAL_DOMAIN):
    """The declared limit document: data from an operator, with its provenance."""
    if not isinstance(document, dict):
        raise ValueError("declared limits are an object")
    required = {"declared_by", "declared_basis", "not_a_certified_safe_limit", "limits"}
    if not required <= set(document) <= required | {"source_identity"}:
        raise ValueError("declared limits have unknown or missing keys")
    if document["not_a_certified_safe_limit"] is not True:
        # The document must say what it is before anything is read from it.
        raise ValueError("declared limits must acknowledge they are not certified")
    for key in ("declared_by", "declared_basis"):
        if not isinstance(document[key], str) or not 1 <= len(document[key]) <= 256:
            raise ValueError("invalid " + key)
    identity = document.get("source_identity")
    if "source_identity" in document:
        # The columns that together identify one source in this input. Declaring it pins the
        # binding: an instance key that leaves any of them out would combine distinct sources,
        # so it is refused instead of pooling them.
        if not isinstance(identity, list) or not 1 <= len(identity) <= 8 \
                or any(not isinstance(name, str) or not NAME_TEXT.fullmatch(name)
                       for name in identity) or len(set(identity)) != len(identity):
            raise ValueError("invalid declared source identity")
    entries = document["limits"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
        raise ValueError("declared limits list is empty or too large")
    return document, [checked_entry(entry, domain) for entry in entries]


def clock_order(rows, time_column):
    """How this instance's own timestamps run, so a duration is never bridged across a break."""
    order = CLOCK_ORDER_OK
    previous = None
    for row in rows:
        now = microseconds(row[time_column], time_column)
        if previous is not None:
            if now < previous:
                return CLOCK_ORDER_BACKWARDS
            if now == previous:
                order = CLOCK_ORDER_DUPLICATE
        previous = now
    return order


def sample_series(rows, entry, time_column, domain=ELECTRICAL_DOMAIN):
    """Every row of this instance in order, with `None` where it is not evidence.

    A blank cell and an unavailable sentinel are not readings, so they stay in the series as
    explicit holes rather than being removed — a run must not close over them and call the
    result consecutive evidence.
    """
    column = entry["column"]
    _, _, availability = domain["quantities"][entry["quantity"]]
    series, bases, unstated = [], set(), 0
    for row in rows:
        time_us = microseconds(row[time_column], time_column)
        text = row.get(column, "")
        unavailable = (availability is not None
                       and row.get(availability[0], "") == availability[1])
        if unavailable or text == "" or text is None:
            series.append((time_us, None))
            continue
        value = numeric(text, column)
        if entry["unit"] == "fraction" and not 0 <= value <= 1:
            # Outside 0..1 it is not a fraction. An unknown sentinel such as -1 is a hole,
            # never an empty pack, so it can never become a low-charge reading. The rule
            # belongs to the declared unit, as the declared limit's bound does, so a fraction
            # from any producer is held to it; `remaining_charge_fraction` is the only
            # electrical quantity carrying that unit, so nothing electrical changes.
            series.append((time_us, None))
            continue
        if entry["quantity"] == "current":
            basis = row.get(SIGN_BASIS_COLUMN, "")
            # A cell of spaces or tabs states nothing, so it counts as unstated exactly as an
            # empty one does. What it does contain is still kept verbatim when it says
            # something: padding is not evidence that two different bases are the same.
            if basis.strip():
                bases.add(basis)
            else:
                unstated += 1
            if entry["sign_handling"] == "magnitude":
                value = abs(value)
        series.append((time_us, value))
    return series, bases, unstated


def best_run(series, entry, requires_transition):
    """The best crossing run **for the declared criterion**, plus counts and transition.

    A count criterion wants the longest run by samples; a duration criterion wants the
    longest by span, which can be a sparser run entirely — choosing by count would miss it.
    A run is broken by a hole in the evidence, by a gap wider than the declared maximum, and
    by a sample that releases; hysteresis never carries a previous run's state across such a
    break.

    When the condition requires an observed transition, a run that has one and meets the
    window is preferred over a bigger run that has neither: a recording that begins low and
    later shows a genuine sag must report the sag. If no run has a transition, the best run
    is still reported, as insufficient transition evidence rather than as the event.
    """
    crosses = COMPARISON[entry["comparison"]]
    limit, release = entry["limit"], entry["release_limit"]
    downward = entry["comparison"] in CROSSES_DOWNWARD
    duration = entry["window_kind"] == "min_duration_us"
    window = entry["window_value"]
    best_any = best_transition = best_transition_met = None
    current, crossing = None, 0
    previous_time = None
    previous_usable_crossed = None  # the sample immediately before, when it was evidence

    def keep(candidate):
        nonlocal best_any, best_transition, best_transition_met
        if best_any is None or candidate["measure"] > best_any["measure"]:
            best_any = candidate
        if not candidate["preceded_by_non_crossing"]:
            return
        if best_transition is None or candidate["measure"] > best_transition["measure"]:
            best_transition = candidate
        if candidate["measure"] >= window and (
                best_transition_met is None
                or candidate["measure"] > best_transition_met["measure"]):
            best_transition_met = candidate

    for time_us, value in series:
        if value is None:
            # A hole is a boundary: nothing is asserted across missing evidence, and the
            # sample before it is no longer an adjacent baseline.
            current = None
            previous_time = None
            previous_usable_crossed = None
            continue
        if entry["max_gap_us"] is not None and previous_time is not None \
                and time_us - previous_time > entry["max_gap_us"]:
            # An excessive gap ends a run **and** ends baseline adjacency, whether or not a
            # run was open: a distant normal sample is not the baseline of this crossing.
            current = None
            previous_usable_crossed = None
        inside = crosses(value, limit)
        if inside:
            crossing += 1
        elif current is not None and release is not None:
            # Hysteresis holds an *existing* run open until the value passes the release
            # limit. A broken run is gone, so this can never resurrect one.
            inside = not (value >= release if downward else value <= release)
        if inside:
            if current is None:
                current = {"count": 0, "start": time_us, "end": time_us, "extreme": value,
                           "preceded_by_non_crossing": previous_usable_crossed is False}
            current["count"] += 1
            current["end"] = time_us
            if (value < current["extreme"]) if downward else (value > current["extreme"]):
                current["extreme"] = value
            measure = (current["end"] - current["start"]) if duration else current["count"]
            keep(dict(current, measure=measure))
        else:
            current = None
        previous_time = time_us
        previous_usable_crossed = crosses(value, limit)

    if requires_transition:
        best = best_transition_met or best_transition or best_any
    else:
        best = best_any
    return best, crossing


def evaluate(rows, entry, instance_column, instance_value, context):
    row = dict.fromkeys(COLUMNS, "")
    row.update({
        "record_time_us": context["anchor"],
        "evaluation_anchor_basis": context["anchor_basis"],
        "source_observations_sha256": context["digests"][0],
        "declared_limits_sha256": context["digests"][1],
        "instance_column": instance_column or "", "instance_value": instance_value or "",
        "condition": entry["condition"], "quantity": entry["quantity"],
        "quantity_column": entry["column"], "declared_unit": entry["unit"],
        "declared_limit": entry["limit"], "declared_comparison": entry["comparison"],
        "declared_release_limit": "" if entry["release_limit"] is None else entry["release_limit"],
        "declared_window_kind": entry["window_kind"], "declared_window_value": entry["window_value"],
        "declared_max_gap_us": "" if entry["max_gap_us"] is None else entry["max_gap_us"],
        "declared_sign_handling": entry["sign_handling"] or "",
        "declared_sign_basis": entry["sign_basis"] or "",
        "source_clock_basis": context["clock_basis"],
        "threshold_declared_by": context["declared_by"],
        "threshold_declared_basis": context["declared_basis"],
        "threshold_authority": THRESHOLD_AUTHORITY,
        "interpretation_basis": context["domain"]["interpretation"],
    })
    duration = entry["window_kind"] == "min_duration_us"
    row["duration_basis"] = ("SOURCE_AXIS_MICROSECONDS_ON_" + context["clock_basis"].upper()
                             if duration else "SAMPLE_COUNT_NO_TIMEBASE_NEEDED")
    if rows is None:
        # The caller asked about an instance this input does not contain. That is a named
        # Unknown for the requested target, not a silent absence of rows.
        row["condition_status"] = UNKNOWN
        row["unknown_reason"] = "REQUESTED_INSTANCE_NOT_PRESENT_IN_INPUT"
        return row

    series, bases, unstated = sample_series(rows, entry, context["time_column"], context["domain"])
    usable = [item for item in series if item[1] is not None]
    row["evaluated_sample_count"] = len(series)
    row["usable_sample_count"] = len(usable)
    row["unusable_sample_count"] = len(series) - len(usable)
    row["observed_sign_basis"] = "+".join(sorted(bases))
    order = clock_order(rows, context["time_column"])
    row["source_clock_order"] = order

    if not usable:
        row["condition_status"] = UNKNOWN
        row["unknown_reason"] = "NO_USABLE_SAMPLE_FOR_THE_DECLARED_QUANTITY"
        return row
    if len(bases) > 1:
        # Two sign conventions in one evaluation are not one comparable series.
        row["condition_status"] = UNKNOWN
        row["unknown_reason"] = "INCONSISTENT_CURRENT_SIGN_BASIS"
        return row
    if entry["sign_handling"] == "signed_as_recorded":
        # Comparing the recorded sign against a limit only means something once somebody has
        # said what the sign means. Magnitude needs no such statement; this does.
        declared = entry["sign_basis"]
        if declared is not None and bases and declared not in bases:
            row["condition_status"] = UNKNOWN
            row["unknown_reason"] = "DECLARED_CURRENT_SIGN_BASIS_CONTRADICTS_INPUT"
            return row
        if unstated and declared is None:
            row["condition_status"] = UNKNOWN
            row["unknown_reason"] = "UNSTATED_CURRENT_SIGN_BASIS_FOR_SIGNED_COMPARISON"
            return row
    if duration:
        if context["clock_basis"] == "unknown":
            # Ordering survives an unknown axis; a duration does not.
            row["condition_status"] = UNKNOWN
            row["unknown_reason"] = "DURATION_REQUIRES_A_TIMEBASE_SOURCE_CLOCK_UNKNOWN"
            return row
        if order != CLOCK_ORDER_OK:
            # A restart or a repeated instant makes the elapsed time between samples
            # unreadable; a span across it would be invented.
            row["condition_status"] = UNKNOWN
            row["unknown_reason"] = "DURATION_REQUIRES_AN_ORDERED_CLOCK_" + order
            return row

    requires_transition = entry["condition"] == "voltage_sag"
    best, crossing = best_run(series, entry, requires_transition)
    row["crossing_sample_count"] = crossing
    if best is None:
        row["condition_status"] = NOT_CROSSED
        return row
    span = best["end"] - best["start"]
    row.update({"run_sample_count": best["count"], "run_start_time_us": best["start"],
                "run_end_time_us": best["end"], "run_span_us": span,
                "run_extreme_value": best["extreme"]})
    met = span >= entry["window_value"] if duration else best["count"] >= entry["window_value"]
    if requires_transition:
        # A sag is a transition into the low band. Without an observed non-crossing sample
        # immediately before the run, a recording that simply begins low is evidence of a low
        # voltage, not of a sag, and it is not promoted to one.
        row["transition_evidence"] = ("OBSERVED_NON_CROSSING_SAMPLE_BEFORE_RUN"
                                      if best["preceded_by_non_crossing"]
                                      else "NO_OBSERVED_BASELINE_BEFORE_RUN")
        if not best["preceded_by_non_crossing"]:
            row["condition_status"] = NO_TRANSITION
            return row
    # A crossing that misses the declared window is exactly the thing that must not be
    # reported as the event.
    row["condition_status"] = MET if met else NOT_SUSTAINED
    return row


def convert(observations, limits_text, *, clock_basis, instance_column,
            time_column="record_time_us", analysis_time_us=None, domain=ELECTRICAL_DOMAIN):
    if clock_basis not in CLOCK_BASES:
        raise ValueError("declare the source clock basis")
    # One declared instance column, or several that together form one composite identity.
    instance_columns = ([] if instance_column is None else
                        [instance_column] if isinstance(instance_column, str) else
                        list(instance_column))
    if len(set(instance_columns)) != len(instance_columns):
        raise ValueError("duplicate declared instance column")
    for name in instance_columns + [time_column]:
        if name is not None and not NAME_TEXT.match(name):
            raise ValueError("invalid column name")
    if analysis_time_us is not None:
        whole(analysis_time_us, "analysis time")
    if len(observations) > LIMIT or len(limits_text) > LIMIT:
        raise ValueError("input exceeds bound")
    try:
        text = observations.decode("utf-8")
        document = json.loads(limits_text.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed input") from exc
    if "\x00" in text:
        raise ValueError("NUL byte in observations")
    limits, entries = checked_limits(document, domain)
    source_identity = limits.get("source_identity")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError("observations have no header") from exc
    if len(header) != len(set(header)):
        # A repeated header would let one column silently overwrite another.
        raise ValueError("duplicate column in the observations header")
    if time_column not in header:
        raise ValueError("the declared time column is not in the observations")
    for name in instance_columns:
        # Identity is declared, never invented: a named column must exist.
        if name not in header:
            raise ValueError("declared instance column is not in the observations")
    if source_identity is not None:
        for name in source_identity:
            if name not in header:
                raise ValueError("declared source identity column is not in the observations")
        missing = [name for name in source_identity if name not in instance_columns]
        if missing:
            # The document pinned what identifies one source here. An instance key that leaves
            # any of it out would group distinct sources into one series, so it is refused
            # rather than pooled. A finer key that also carries every declared part is allowed.
            raise ValueError("the declared source identity is not fully used as the instance key")
    for entry in entries:
        if entry["instance_arity"] and entry["instance_arity"] != len(instance_columns):
            # A target must name exactly as many parts as the identity has.
            raise ValueError("declared instance value does not match the declared identity")
        if entry["declared_column"] is not None and entry["declared_column"] not in header:
            # A caller who names a column has asserted it is there; a missing one is refused
            # rather than quietly becoming "no usable sample".
            raise ValueError("declared quantity column is not in the observations")

    # Group once. Every evaluation then reads only its own instance's rows.
    grouped, order_seen, total = {}, [], 0
    for values in reader:
        if len(values) != len(header):
            raise ValueError("ragged observations row")
        row = dict(zip(header, values))
        microseconds(row[time_column], time_column)
        parts = [row[name] for name in instance_columns]
        if any(not part.strip() for part in parts):
            # A blank identity is not an identity, and would otherwise pool unrelated rows.
            raise ValueError("blank declared instance value in the observations")
        if any(INSTANCE_SEPARATOR in part for part in parts):
            raise ValueError("declared instance value contains the composite separator")
        key = INSTANCE_SEPARATOR.join(parts)
        if key not in grouped:
            grouped[key] = []
            order_seen.append(key)
        grouped[key].append(row)
        total += 1
        if total > MAX_ROWS:
            raise ValueError("observations exceed row bound")
    if not total:
        raise ValueError("no observations to evaluate")

    requested = {entry["instance_value"] for entry in entries if entry["instance_value"]}
    targets = order_seen + sorted(name for name in requested if name not in grouped)
    evaluations = sum(1 if entry["instance_value"] else len(targets) for entry in entries)
    if evaluations > MAX_EVALUATIONS:
        # Refused out loud rather than truncated: the caller asked for more than this bridge
        # will do in one pass.
        raise ValueError("requested evaluations exceed bound")

    anchor = analysis_time_us
    anchor_basis = "CALLER_ANALYSIS_TIME_NOT_EVENT_ONSET"
    if anchor is None:
        anchor = min(microseconds(rows[0][time_column], time_column)
                     for rows in grouped.values())
        anchor_basis = "FIRST_SOURCE_ROW_TIME_NOT_EVENT_ONSET"
    context = {
        "anchor": anchor, "anchor_basis": anchor_basis, "clock_basis": clock_basis,
        "time_column": time_column, "domain": domain,
        "digests": (hashlib.sha256(observations).hexdigest(),
                    hashlib.sha256(limits_text).hexdigest()),
        "declared_by": "hex:" + limits["declared_by"].encode().hex(),
        "declared_basis": "hex:" + limits["declared_basis"].encode().hex(),
    }

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    counts = {status: 0 for status in (MET, NOT_SUSTAINED, NOT_CROSSED, NO_TRANSITION, UNKNOWN)}
    written = 0
    for entry in entries:
        for instance in targets:
            if entry["instance_value"] is not None and entry["instance_value"] != instance:
                continue
            result = evaluate(grouped.get(instance), entry,
                              INSTANCE_SEPARATOR.join(instance_columns), instance, context)
            writer.writerow(result)
            counts[result["condition_status"]] += 1
            written += 1
            if output.tell() > LIMIT:  # enforced as rows are written, not after the fact
                raise ValueError("observation CSV exceeds bound")
    report = {
        "source_observations_sha256": context["digests"][0],
        "declared_limits_sha256": context["digests"][1],
        "source_rows": total, "evaluations": written,
        "instance_column": INSTANCE_SEPARATOR.join(instance_columns), "time_column": time_column,
        "instances": list(targets),
        "instances_absent_from_input": [name for name in targets if name not in grouped],
        "source_clock_basis": clock_basis,
        "evaluation_anchor_us": anchor, "evaluation_anchor_basis": anchor_basis,
        "threshold_authority": THRESHOLD_AUTHORITY,
        "interpretation_basis": domain["interpretation"],
        "status_counts": counts,
        "reuse": domain["reuse"],
    }
    if len(json.dumps(report).encode()) > LIMIT:
        raise ValueError("report exceeds bound")
    return output.getvalue(), report


def main(domain=ELECTRICAL_DOMAIN, description=__doc__):
    """The one CLI. A second passive caller supplies its own closed domain and reuses it."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("observations", type=Path, help=domain["input_help"])
    parser.add_argument("declared_limits", type=Path, help="operator-declared limit document")
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--source-clock-basis", required=True, choices=CLOCK_BASES,
                        help="clock basis of the source observations; unknown forbids duration")
    parser.add_argument("--instance-column", action="append", default=None,
                        help="column holding the device/instance identity; repeat for a composite identity, omit for one unnamed instance")
    parser.add_argument("--time-column", default="record_time_us",
                        help="time column of the source observations (the adopted BAT export uses TimeUS)")
    parser.add_argument("--analysis-time-us", type=int, default=None,
                        help="caller analysis clock used as the row anchor; never an event onset")
    args = parser.parse_args()
    try:
        with args.observations.open("rb") as stream:
            observations = stream.read(LIMIT + 1)  # one bounded read; no size/read race
        with args.declared_limits.open("rb") as stream:
            limits_text = stream.read(LIMIT + 1)
        output, report = convert(observations, limits_text,
                                 clock_basis=args.source_clock_basis,
                                 instance_column=args.instance_column,
                                 time_column=args.time_column,
                                 analysis_time_us=args.analysis_time_us, domain=domain)
        args.output_directory.mkdir()  # Refuse an existing output/evidence directory.
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, RecursionError):
        parser.exit(2, domain["failure"] + "\n")


if __name__ == "__main__":
    main()
