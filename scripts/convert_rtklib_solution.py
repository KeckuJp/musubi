"""A saved RTKLIB solution (.pos, LLH) -> common position observations. Passive reading only.

This converter computes NO solution. It does not run RTK or PPK, does not touch raw measurements,
does not apply or recompute a geoid, and converts between no time systems, datums or height
references. It reads a file a producer already wrote and carries what that producer DECLARED.

What the source means is fixed by one official producer, pinned here and not re-authored:

  RTKLIB 2.4.3 b34, tomojitakasu/RTKLIB, commit 180043ee24b6d2b168f98b64be15f69d50046b1a
    src/solution.c  sha256 efffa957cb7b674ca5583f2f847ada083747d55a85fcb4b24b6f8559449e73d8
    src/rtklib.h    sha256 c00abb9eee5b89de376ab3a971555459b4efbf92410b8efb0141d168ae94160d
    src/rtkcmn.c    sha256 3f57e10a71e72bc44dc7453ed1b7a6706dd42d1ab8f2103686f1672c1a19195a
  read: outsolheads, outsols, outpos, sqvar, opt2sep, time2epoch, time2str;
        COMMENTH, SOLF_*, SOLQ_*, TIMES_*

Only `SOLF_LLH` with decimal degrees is read. The header is where the meaning lives, because
`outsolheads` writes the producer's options into it as literal tokens:

  % (lat/lon/height=<WGS84|Tokyo>/<ellipsoidal|geodetic>,Q=1:fix,...,6:ppp,ns=# of satellites)
  %  <GPST|UTC |JST >  latitude(deg)  longitude(deg)  height(m)  Q  ns  sdn(m) ... ratio

`s1[opt->datum]`, `s2[opt->height]` and `s3[opt->times]` are those tokens. So the datum, the height
reference and the time system are **read from the producer's own header, never inferred from a
column name and never defaulted**. The first line is optional (`opt->outhead`); when it is absent a
CALLER may declare what it would have said, and that is carried as a declaration, never as producer
provenance. A caller declaration that contradicts a present token is refused.

Three details of `outpos` that a careless reader gets wrong, each taken from the pinned source:

  * height is `pos[2]`, and `if (opt->height==1) pos[2] -= geoidh(pos)`. That subtraction is exactly
    what the header word `geodetic` means, so the word decides the reference and nothing is
    recomputed here.
  * `sdn` reads `Q[4]` and `sde` reads `Q[0]` -- north and east are NOT in the naive index order.
  * `sdne`, `sdeu`, `sdun` come from `sqvar`, which returns `-sqrt(-v)` for a negative covariance.
    They are SIGNED square roots of covariance and are never renamed standard deviations.

And two facts the header legend does not state: `SOLQ_DR = 7` exists in the enum while the legend
lists only 1..6, and `outsols` suppresses a row when `sol->stat <= SOLQ_NONE`, so Q = 0 is never
written by this producer.

The Q value is the solution producer's OWN report. It is not verified accuracy, and no covariance,
confidence or error estimate is fabricated anywhere in this path.
"""
import argparse
import csv
import datetime
import decimal
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sys

COMMENT = "%"
# `s1[opt->datum]`, `s2[opt->height]`, `s3[opt->times]` from outsolheads, verbatim.
DATUM_TOKENS = ("WGS84", "Tokyo")
HEIGHT_TOKENS = ("ellipsoidal", "geodetic")
TIME_SYSTEMS = ("GPST", "UTC", "JST")
# SOLQ_* from rtklib.h. 0 is never written because outsols suppresses stat <= SOLQ_NONE, and 7 is in
# the enum even though the header legend stops at 6.
SOLUTION_QUALITY = {1: "FIX", 2: "FLOAT", 3: "SBAS", 4: "DGPS", 5: "SINGLE", 6: "PPP",
                    7: "DEAD_RECKONING"}
QUALITY_NOT_IN_LEGEND = frozenset({7})

# The decimal-degree column labels `outsolheads` writes for SOLF_LLH, in order.
LLH_COLUMNS = ("latitude(deg)", "longitude(deg)", "height(m)", "Q", "ns", "sdn(m)", "sde(m)",
               "sdu(m)", "sdne(m)", "sdeu(m)", "sdun(m)", "age(s)", "ratio")
# Labels that mean this file is a format this path does not read.
FOREIGN_COLUMNS = {"x-ecef(m)": "SOLF_XYZ", "e-baseline(m)": "SOLF_ENU",
                   "latitude(d'\")": "SOLF_LLH with opt->degf=1 (degree/minute/second)"}

DECIMAL_TEXT = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
CALENDAR_TIME = re.compile(r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?$")
NEEDS_QUOTING = re.compile(r'[,"\r\n]')

EPOCH_ORDINAL = datetime.date(1970, 1, 1).toordinal()
# GPS week 0 began 1980-01-06T00:00:00 UTC; used only to place a week/tow pair on the row axis.
GPS_EPOCH_SECONDS = (datetime.date(1980, 1, 6).toordinal() - EPOCH_ORDINAL) * 86400

MAX_INPUT = 32 * 1024 * 1024
MAX_ROWS = 200000
MAX_OUTPUT = 64 * 1024 * 1024
MAX_HEADER_LINES = 256
# 2**40 seconds is about 1.1e12, whose adjusted decimal exponent is 12; checked before any expansion.
MAX_TIME_ADJUSTED_EXPONENT = 12
MAX_TIME_SIGNIFICANT_DIGITS = 64

QUALITY_BASIS = (
    "THE_SOLUTION_PRODUCERS_OWN_REPORTED_STATUS_AND_NEVER_VERIFIED_ACCURACY_the_pinned_enumeration_"
    "names_1_fix_2_float_3_sbas_4_dgps_5_single_6_ppp_and_7_dead_reckoning_a_value_of_FIX_is_what_the_"
    "solver_said_about_its_own_ambiguity_resolution_it_is_NOT_a_measured_error_NOT_a_confidence_and_"
    "NOT_evidence_that_the_position_is_correct_no_covariance_confidence_or_error_estimate_is_"
    "fabricated_anywhere_in_this_path")

DEVIATION_BASIS = (
    "THE_PRODUCERS_OWN_REPORTED_SPREAD_carried_as_written_sdn_sde_sdu_are_metres_from_SQRT_of_the_ENU_"
    "covariance_diagonal_where_the_writer_reads_north_from_index_4_and_east_from_index_0_while_sdne_"
    "sdeu_and_sdun_come_from_a_helper_that_returns_MINUS_the_root_of_a_NEGATIVE_covariance_so_they_"
    "are_SIGNED_SQUARE_ROOTS_OF_COVARIANCE_AND_NOT_STANDARD_DEVIATIONS_and_none_of_them_is_an_"
    "independently_verified_accuracy")

FIELDS = (
    "record_time_us",
    "solution_time_system",
    "solution_time_basis",
    "solution_time_text",
    "latitude_deg",
    "longitude_deg",
    "height_m",
    "height_reference",
    "height_reference_basis",
    "horizontal_datum",
    "datum_basis",
    "solution_quality_code",
    "solution_quality_name",
    "solution_quality_in_header_legend",
    "quality_basis",
    "satellites_used_reported",
    "sdn_m",
    "sde_m",
    "sdu_m",
    "sdne_signed_m",
    "sdeu_signed_m",
    "sdun_signed_m",
    "deviation_basis",
    "age_of_differential_s",
    "ambiguity_ratio_reported",
    "source_line",
    "uncarried_column_count",
    "uncarried_column_names_hex",
    "header_line_count",
    "producer_header_hex",
    "source_sha256",
)

NOT_A_SOLVER = "NONE_THIS_TOOL_COMPUTES_NO_SOLUTION_AND_RESOLVES_NO_AMBIGUITY"


def hexed(value):
    if value is None:
        return ""
    return "hex:" + value.encode("utf-8").hex()


def hexed_list(values):
    if not values:
        return ""
    return hexed(json.dumps(values, ensure_ascii=False, separators=(",", ":")))


def exact_number(text, what, low=None, high=None):
    """The producer's own digits, range-checked on their exact decimal value and emitted unchanged.

    What this guarantees, precisely: the TEXT reaches the cell byte for byte, the range comparison is
    made on the exact decimal value rather than on a float, and a magnitude that would arrive at the
    reader as an infinity, or as a zero it is not, is refused.

    What it does NOT guarantee, and what this path does not demand: that the reader's binary float
    equals the decimal exactly. `35.681236000` has no exact binary representation, and neither do most
    real coordinates. Requiring that would reject ordinary positions. Preserving the text is a
    different and weaker promise than exact numeric representation downstream, and it is the one made
    here.
    """
    text = text.strip()
    if not DECIMAL_TEXT.fullmatch(text):
        raise ValueError("%s is %r, which is not a decimal number" % (what, text))
    try:
        quantity = decimal.Decimal(text)
    except decimal.InvalidOperation:
        raise ValueError("%s is %r, which is not a decimal number" % (what, text))
    if not quantity.is_finite():
        raise ValueError("%s is %r, which is not finite" % (what, text))
    if low is not None and quantity < decimal.Decimal(low):
        raise ValueError("%s is %r, below the range this path accepts" % (what, text))
    if high is not None and quantity > decimal.Decimal(high):
        raise ValueError("%s is %r, above the range this path accepts" % (what, text))
    reachable = float(text)
    if not math.isfinite(reachable):
        raise ValueError("%s is %r, which overflows the reader's float and is refused rather than "
                         "carried as an infinity" % (what, text))
    if reachable == 0 and quantity != 0:
        raise ValueError("%s is %r, which underflows to zero at the reader and is refused rather "
                         "than carried as an exact zero it is not" % (what, text))
    return text


def integer_field(text, what, low, high):
    text = text.strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        raise ValueError("%s is %r, which is not an integer" % (what, text))
    value = int(text)
    if not low <= value <= high:
        raise ValueError("%s is %d, outside the range this path accepts" % (what, value))
    return value


def calendar_time_us(text):
    """`time2str` output, placed on the row axis with no system conversion whatsoever."""
    match = CALENDAR_TIME.fullmatch(text)
    if not match:
        raise ValueError("the solution time %r is not the producer's calendar form" % text)
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    fraction = match.group(7)
    # `time2epoch` computes `ep[5] = sec%60 + t.sec` with the fraction in [0,1), and `time2str`
    # rounds a fraction up into the integer second before formatting. So this writer's seconds field
    # is [0,60) and it NEVER prints 60: the format carries no leap-second representation at all.
    # Accepting 60 and rolling it into the next minute would invent one, so it is refused.
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError(
            "the solution time %r is outside the pinned writer's range; its seconds field is 0 to 59 "
            "and it never prints 60, so no leap second is represented here and none is invented"
            % text)
    if fraction and len(fraction) > 7:
        digits = fraction[1:]
        if digits[6:].strip("0"):
            raise ValueError(
                "the solution time %r carries nonzero precision finer than one microsecond, which "
                "the common output cannot hold; it is refused rather than rounded" % text)
    try:
        ordinal = datetime.date(year, month, day).toordinal()
    except ValueError as error:
        raise ValueError("the solution time %r is not a real date: %s" % (text, error))
    microseconds = int((fraction or ".")[1:7].ljust(6, "0") or "0") if fraction else 0
    seconds = (ordinal - EPOCH_ORDINAL) * 86400 + hour * 3600 + minute * 60 + second
    if seconds < 0:
        raise ValueError("the solution time %r is before the epoch the common reader counts from"
                         % text)
    return seconds * 1000000 + microseconds


def exact_microseconds(text, what):
    """Decimal text -> integer microseconds by INTEGER arithmetic on the coefficient and exponent.

    `Decimal` addition and `Decimal.scaleb` both obey the current decimal context and round at its
    precision, which defaults to 28 significant digits. An earlier version of this function added the
    GPS epoch to the seconds-of-week and then scaled, so a seconds-of-week carrying a nonzero tail
    beyond that precision -- `356645.00000000000000000000000000001` has 35 digits -- was rounded into
    a whole microsecond and silently accepted. The fraction is therefore decided HERE, before any
    epoch is added, using Python integers, which are exact and context-free.
    """
    quantity = decimal.Decimal(text)
    if quantity.is_zero():
        return 0
    if quantity.adjusted() > MAX_TIME_ADJUSTED_EXPONENT:
        raise ValueError("%s has magnitude 1e%d, far outside the range this path accepts"
                         % (what, quantity.adjusted()))
    if quantity.adjusted() <= -7:
        raise ValueError("%s is %s, a nonzero magnitude finer than the one microsecond the common "
                         "output holds; it is refused rather than rounded" % (what, quantity))
    _sign, digits, exponent = quantity.as_tuple()
    if len(digits) > MAX_TIME_SIGNIFICANT_DIGITS:
        raise ValueError("%s carries %d significant digits, past the %d this path reads"
                         % (what, len(digits), MAX_TIME_SIGNIFICANT_DIGITS))
    coefficient = int("".join(map(str, digits)))
    scaled_exponent = exponent + 6
    if scaled_exponent >= 0:
        return coefficient * 10 ** scaled_exponent
    divisor = 10 ** -scaled_exponent
    if coefficient % divisor:
        raise ValueError("%s is %s, which carries nonzero precision finer than one microsecond; it "
                         "is refused rather than rounded" % (what, quantity))
    return coefficient // divisor


def week_tow_time_us(week_text, tow_text):
    """A GPS week and seconds-of-week, placed on the row axis by exact integer arithmetic.

    The seconds-of-week range is the writer's own: `outsols` contains
    `if (86400*7-gpst<0.5/pow(10.0,timeu)) { week++; gpst=0.0; }`, so this producer rolls a
    full week into the next week number and NEVER emits 604800. The bound is therefore exclusive.

    This is a position on the row axis only. No leap second is applied and no system is converted:
    whichever system the header names is the system these numbers are already in.
    """
    week = integer_field(week_text, "the solution GPS week", 0, 32767)
    text = exact_number(tow_text, "the solution seconds of week", 0, None)
    if decimal.Decimal(text) >= 604800:
        raise ValueError(
            "the solution seconds of week is %r; the pinned writer rolls a full week into the next "
            "week number and never emits 604800 or more, so this is outside its range" % text)
    # The fraction is resolved before the epoch is added, and the rest is integer arithmetic.
    return (GPS_EPOCH_SECONDS + week * 604800) * 1000000 + exact_microseconds(
        text, "the solution seconds of week")


def parse_header(lines, declared_height, declared_datum, declared_time_system):
    """Read the producer's options out of the header it wrote, and only from there.

    `outsolheads` writes `s1[opt->datum]`, `s2[opt->height]` and `s3[opt->times]` as literal tokens.
    That is the only place this path takes them from. When the optional first line is absent the
    caller may declare its content, which is then labelled a caller declaration; a declaration that
    contradicts a token the producer did write is refused outright.
    """
    datum = height = time_system = None
    columns = None
    option_line = column_line = None
    for line in lines:
        body = line[len(COMMENT):].strip()
        found = re.search(r"lat/lon/height=(\w+)/(\w+)", body)
        if found:
            if found.group(1) not in DATUM_TOKENS:
                raise ValueError("the header declares datum %r, which the pinned writer does not "
                                 "emit" % found.group(1))
            if found.group(2) not in HEIGHT_TOKENS:
                raise ValueError("the header declares height reference %r, which the pinned writer "
                                 "does not emit" % found.group(2))
            # A second option line is only tolerable if it says exactly the same thing. Taking the
            # last one would let a later comment silently redefine the meaning of earlier rows.
            if option_line is not None and (datum, height) != (found.group(1), found.group(2)):
                raise ValueError(
                    "the header declares lat/lon/height twice and disagrees -- %s/%s then %s/%s; a "
                    "contradictory basis is refused rather than resolved by taking the last"
                    % (datum, height, found.group(1), found.group(2)))
            option_line = line
            datum, height = found.group(1), found.group(2)
        if "latitude" in body or "x-ecef" in body or "e-baseline" in body:
            if column_line is not None:
                raise ValueError(
                    "the file carries a second column header; two concatenated layouts are refused "
                    "rather than read as one, because a later layout must not redefine the meaning "
                    "of rows that came before it")
            column_line = line
            fields = [part for part in re.split(r"\s{1,}", body) if part]
            for label, which in FOREIGN_COLUMNS.items():
                if label in body:
                    raise ValueError(
                        "this file's columns are %s; this path reads only SOLF_LLH in decimal "
                        "degrees and refuses another format rather than guessing at it" % which)
            if fields and fields[0] in TIME_SYSTEMS:
                time_system = fields[0]
                columns = fields[1:]
            elif fields and fields[0] in LLH_COLUMNS:
                # A column line with no leading time token at all: the time system is unstated and
                # a caller may declare it. The data columns start immediately.
                columns = fields
            else:
                # Something occupies the time position that this writer never emits there. Treating
                # it as a data column would quietly file it among the uncarried names, so it is
                # refused instead of being absorbed.
                raise ValueError(
                    "the column header begins with %r, which the pinned writer emits neither as a "
                    "time system nor as a column label; it is refused rather than absorbed as data"
                    % (fields[0] if fields else ""))

    if columns is None:
        raise ValueError("the saved file carries no solution column header, so its format, time "
                         "system and height reference are all unstated")
    repeated = sorted({name for name in columns if columns.count(name) > 1})
    if repeated:
        raise ValueError(
            "the column header repeats %s; a duplicate label would make the row mapping ambiguous "
            "and the last one would silently win, so it is refused"
            % ", ".join(repr(name) for name in repeated))
    missing = [name for name in LLH_COLUMNS if name not in columns]
    if missing:
        raise ValueError("the column header lacks %s, so this is not the SOLF_LLH decimal-degree "
                         "layout this path reads" % ", ".join(missing))

    def settle(producer, caller, what, allowed):
        if caller is not None and caller not in allowed:
            raise ValueError("the caller declared %s %r, which the pinned writer does not emit"
                             % (what, caller))
        if producer is not None:
            if caller is not None and caller != producer:
                raise ValueError(
                    "the caller declared %s %r while the producer's own header says %r; a "
                    "contradiction is refused rather than resolved" % (what, caller, producer))
            return producer, "PRODUCER_DECLARED_IN_THE_SAVED_HEADER"
        if caller is None:
            raise ValueError(
                "the saved header does not state the %s and no caller declaration was supplied; "
                "this path refuses a missing basis rather than assuming a default" % what)
        return caller, ("CALLER_DECLARED_BECAUSE_THE_SAVED_HEADER_OMITS_IT_this_is_the_CALLERS_"
                        "STATEMENT_about_how_the_producer_was_configured_and_is_NOT_producer_"
                        "provenance_and_is_never_confirmed_by_the_file")

    height_value, height_basis = settle(height, declared_height, "height reference", HEIGHT_TOKENS)
    datum_value, datum_basis = settle(datum, declared_datum, "horizontal datum", DATUM_TOKENS)
    time_value, time_basis = settle(time_system, declared_time_system, "time system", TIME_SYSTEMS)
    uncarried = [name for name in columns if name not in LLH_COLUMNS]
    return {
        "height_reference": height_value, "height_reference_basis": height_basis,
        "horizontal_datum": datum_value, "datum_basis": datum_basis,
        "solution_time_system": time_value, "solution_time_basis": time_basis,
        "columns": columns, "uncarried": uncarried,
    }


def convert(data, height_reference=None, horizontal_datum=None, time_system=None,
            output_budget=None):
    """Saved .pos bytes -> (CSV text for the common reader, a report of what was read).

    One row per solution epoch the producer wrote. Nothing is solved, nothing is converted.
    """
    budget = MAX_OUTPUT if output_budget is None else output_budget
    if not isinstance(data, bytes):
        raise ValueError("a saved solution file is read as bytes")
    if not data.strip():
        raise ValueError("the saved solution file is empty")
    if len(data) > MAX_INPUT:
        raise ValueError("the saved solution file is %d bytes, past the %d byte input bound"
                         % (len(data), MAX_INPUT))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("the saved solution file is not UTF-8: %s" % error)

    header_lines, body_lines = [], []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(COMMENT):
            # One header block, before all data. A comment section appearing after solution rows
            # would otherwise be read as declaring the basis for rows that preceded it.
            if body_lines:
                raise ValueError(
                    "line %d is a comment after the first solution epoch; this path reads ONE header "
                    "block before all data, so a later comment can never retroactively change what "
                    "earlier rows meant" % number)
            if len(header_lines) >= MAX_HEADER_LINES:
                raise ValueError("the saved file carries more header lines than the finite bound")
            header_lines.append(stripped)
        else:
            body_lines.append((number, line))
    if not header_lines:
        raise ValueError("the saved file carries no header, so its format and options are unstated")
    if not body_lines:
        raise ValueError("the saved file carries a header and no solution epoch")
    if len(body_lines) > MAX_ROWS:
        raise ValueError("the saved file carries more solution epochs than the finite bound")

    settled = parse_header(header_lines, height_reference, horizontal_datum, time_system)
    digest = hashlib.sha256(data).hexdigest()
    # The producer's own provenance lines, carried verbatim. `% program` and `% inp file` are what
    # outsolopts writes; they are the producer's statement and are never resolved or verified here.
    provenance = [line for line in header_lines
                  if re.match(r"%\s*(program|inp file|obs start|obs end|ref pos)\b", line)]

    shared = {
        "solution_time_system": settled["solution_time_system"],
        "solution_time_basis": settled["solution_time_basis"],
        "height_reference": settled["height_reference"],
        "height_reference_basis": settled["height_reference_basis"],
        "horizontal_datum": settled["horizontal_datum"],
        "datum_basis": settled["datum_basis"],
        "quality_basis": QUALITY_BASIS,
        "deviation_basis": DEVIATION_BASIS,
        "uncarried_column_count": len(settled["uncarried"]),
        "uncarried_column_names_hex": hexed_list(settled["uncarried"]),
        "header_line_count": len(header_lines),
        "producer_header_hex": hexed_list(provenance),
        "source_sha256": digest,
    }

    rows = []
    for number, line in body_lines:
        parts = [part for part in re.split(r"\s+", line.strip()) if part]
        # The time occupies two whitespace-separated tokens in both of the producer's spellings:
        # `YYYY/MM/DD hh:mm:ss.sss` from time2str, or `week seconds-of-week`.
        if len(parts) < 2 + len(LLH_COLUMNS):
            raise ValueError("line %d carries %d fields; the declared layout needs %d"
                             % (number, len(parts), 2 + len(LLH_COLUMNS)))
        first, second = parts[0], parts[1]
        if "/" in first:
            time_us = calendar_time_us(first + " " + second)
            time_text = first + " " + second
        else:
            time_us = week_tow_time_us(first, second)
            time_text = first + " " + second
        values = parts[2:]
        if len(values) != len(settled["columns"]):
            raise ValueError("line %d carries %d value fields while the header declares %d"
                             % (number, len(values), len(settled["columns"])))
        named = dict(zip(settled["columns"], values))
        quality = integer_field(named["Q"], "the solution quality on line %d" % number, 1, 7)
        if quality not in SOLUTION_QUALITY:
            raise ValueError("line %d reports quality %d, which the pinned enumeration does not "
                             "define" % (number, quality))
        rows.append(dict(
            shared,
            record_time_us=time_us,
            solution_time_text=time_text,
            latitude_deg=exact_number(named["latitude(deg)"], "latitude", -90, 90),
            longitude_deg=exact_number(named["longitude(deg)"], "longitude", -180, 180),
            height_m=exact_number(named["height(m)"], "height"),
            solution_quality_code=quality,
            solution_quality_name=SOLUTION_QUALITY[quality],
            solution_quality_in_header_legend=(
                "NO_THE_PINNED_ENUMERATION_DEFINES_IT_BUT_THE_HEADER_LEGEND_STOPS_AT_SIX"
                if quality in QUALITY_NOT_IN_LEGEND else "YES"),
            satellites_used_reported=integer_field(
                named["ns"], "the satellite count on line %d" % number, 0, 255),
            sdn_m=exact_number(named["sdn(m)"], "sdn", 0, None),
            sde_m=exact_number(named["sde(m)"], "sde", 0, None),
            sdu_m=exact_number(named["sdu(m)"], "sdu", 0, None),
            sdne_signed_m=exact_number(named["sdne(m)"], "sdne"),
            sdeu_signed_m=exact_number(named["sdeu(m)"], "sdeu"),
            sdun_signed_m=exact_number(named["sdun(m)"], "sdun"),
            age_of_differential_s=exact_number(named["age(s)"], "age", 0, None),
            ambiguity_ratio_reported=exact_number(named["ratio"], "ratio", 0, None),
            source_line=number))

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(FIELDS)
    written = len(buffer.getvalue().encode("utf-8"))
    row_buffer = io.StringIO()
    row_writer = csv.writer(row_buffer, lineterminator="\n")
    for index, row in enumerate(rows):
        values = [row[name] for name in FIELDS]
        for name, value in zip(FIELDS, values):
            if isinstance(value, str) and NEEDS_QUOTING.search(value):
                raise ValueError("%s would need CSV quoting, which the common reader refuses"
                                 % name)
        row_buffer.seek(0)
        row_buffer.truncate(0)
        row_writer.writerow(values)
        chunk = row_buffer.getvalue()
        written += len(chunk.encode("utf-8"))
        if written > budget:
            raise ValueError("the expanded output passed the %d byte budget at row %d of %d (%d "
                             "UTF-8 bytes written); nothing is truncated to fit"
                             % (budget, index + 1, len(rows), written))
        buffer.write(chunk)

    report = {
        "epochs": len(rows),
        "header_lines": len(header_lines),
        "time_system": settled["solution_time_system"],
        "time_system_basis": settled["solution_time_basis"],
        "height_reference": settled["height_reference"],
        "height_reference_basis": settled["height_reference_basis"],
        "horizontal_datum": settled["horizontal_datum"],
        "uncarried_columns": settled["uncarried"],
        "quality_counts": {SOLUTION_QUALITY[q]: sum(1 for r in rows
                                                    if r["solution_quality_code"] == q)
                           for q in sorted({r["solution_quality_code"] for r in rows})},
        "source_sha256": digest,
        "solutions_computed": NOT_A_SOLVER,
        "verified_accuracy": "NONE_EVERY_QUALITY_AND_SPREAD_REMAINS_THE_PRODUCERS_OWN_REPORT",
    }
    return buffer.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="a saved RTKLIB .pos solution in LLH form")
    parser.add_argument("output", type=Path, help="directory for observations.csv and report.json")
    parser.add_argument("--declare-height-reference", choices=HEIGHT_TOKENS, default=None,
                        help="what the producer's height reference was, for a file whose optional "
                             "header line is absent; carried as a caller declaration only")
    parser.add_argument("--declare-datum", choices=DATUM_TOKENS, default=None,
                        help="likewise for the horizontal datum")
    parser.add_argument("--declare-time-system", choices=TIME_SYSTEMS, default=None,
                        help="likewise for the time system")
    arguments = parser.parse_args(argv)
    size = arguments.source.stat().st_size
    if size > MAX_INPUT:
        raise ValueError("the saved solution file is %d bytes, past the %d byte input bound"
                         % (size, MAX_INPUT))
    with arguments.source.open("rb") as stream:
        data = stream.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise ValueError("the saved solution file grew past the %d byte input bound while reading"
                         % MAX_INPUT)
    output, report = convert(data, arguments.declare_height_reference, arguments.declare_datum,
                            arguments.declare_time_system)
    if arguments.output.exists():
        raise ValueError("the output directory %s already exists; this converter never overwrites "
                         "an existing artifact" % arguments.output)
    arguments.output.mkdir(parents=True, exist_ok=False)
    (arguments.output / "observations.csv").write_text(output, encoding="utf-8")
    (arguments.output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
