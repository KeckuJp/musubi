#!/usr/bin/env python3
"""Saved MOOS-IvP `.moos` mission / `.bhv` behavior declarations -> typed CSV for TelemetryCsvReader.

Declaration import only. No MOOS community is started, no socket is opened, no device is contacted, no
behavior is run, no condition expression is evaluated and no mission is written. A declared value is
what the saved file says, never what a vehicle did: nothing here reports status, navigation or a
behavior outcome, and no wall clock, UTC or coordinate projection is invented.

Grammar is fixed against moos-ivp f56c6907aed8b1c4b35b4b7b94593876dfa95646 (the tree this repo already
pins for its MOOS logger work), reader side only:
`MOOS_Jul2724/MOOSCore/Core/libMOOS/Utils/MOOSFileReader.cpp` and `ProcessConfigReader.cpp` for the
mission file, `ivp/src/lib_helmivp/Populator_BehaviorSet.cpp` with `lib_mbutil/FileBuffer.cpp`,
`lib_behaviors/IvPBehavior.cpp` and `lib_logic/LogicCondition.cpp` for the behavior file.
Contract, refusals and the ambiguities left unresolved:
docs/saved-inputs.md
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re

PIN = "moos-ivp-f56c6907"
LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
REPORT_LIMIT = 32 * 1024 * 1024
MAX_ROWS = 200_000
MAX_LINE_BYTES = 64 * 1024

# Constructs whose meaning one saved file does not determine: nsplug preprocessing and the reader's own
# shell-variable expansion (`${X}` falls back to getenv). No value is invented for them.
PREPROCESSOR = ("#include", "#ifdef", "#ifndef", "#else", "#endif", "#define")
SHELL_VARIABLE = re.compile(r"\$[{(]")
DEFINE_LINE = re.compile(r"^define\s*:", re.IGNORECASE)

INTEGER = re.compile(r"[+-]?[0-9]{1,19}\Z")
DECIMAL = re.compile(r"[+-]?(?:[0-9]{1,19}\.[0-9]{1,19}|\.[0-9]{1,19}|[0-9]{1,19}\.)(?:[eE][+-]?[0-9]{1,3})?\Z")
BOOLEAN = {"true": 1, "false": 0}

# `IvPBehavior::setParam`: these accumulate rather than overwrite, and a condition is compiled into a
# parse tree and evaluated only at run time - the helm keeps no source string, so this importer keeps
# the authored expression verbatim and applies none of that rewriting.
# `IvPBehavior::setParam` lowercases the name and strips one leading `_` before matching, so the
# accumulation lookup below folds the same way.
ACCUMULATING = ("condition", "spawnflag", "spawn_flag", "spawnxflag", "spawnx_flag", "runflag",
                "run_flag", "runxflag", "runx_flag", "activeflag", "active_flag", "inactiveflag",
                "inactive_flag", "idleflag", "idle_flag", "endflag", "end_flag", "configflag",
                "config_flag", "build_info", "precision")
# Keyed inserts: a later line with the same key replaces that key, a different key adds one.
ACCUMULATING_PER_KEY = ("post_mapping", "no_starve", "nostarve")
# The base class assigns a single member for these (`name`/`descriptor` reach `setBehaviorName`).
# `updates` overwrites the update variable while also appending to the info-var list.
OVERWRITING = ("name", "descriptor", "us", "pwt", "priwt", "priority", "comms_policy", "duration",
               "duration_status", "duration_reset", "duration_idle_decay", "perpetual", "updates")
# An unrecognised name makes the base `setParam` return false and is then offered to the concrete
# behavior subclass, whose own rule this pin does not settle: no accumulation claim is made for it.
ACCUMULATION_UNQUALIFIED = "unqualified_no_rule_established_by_the_pinned_source"
EXPRESSION_PARAMETERS = ("condition",)
INITIALIZE_KEYWORDS = ("initialize", "initialize_")
EXPRESSION_BASIS = "UNEVALUATED_AUTHORED_SOURCE_EXPRESSION_NEVER_EVALUATED_HERE"
DECLARATION_BASIS = "DECLARED_CONFIGURATION_NOT_OBSERVED_BEHAVIOR_STATUS_OR_COMMAND"
GLOBAL_LOOKUP_BASIS = ("PINNED_READER_GetValue_IS_BLOCK_UNAWARE_SO_A_BLOCK_LINE_CAN_SATISFY_A_GLOBAL_"
                       "LOOKUP_THIS_ROW_REPORTS_FILE_STRUCTURE_ONLY")
WHITESPACE_BASIS = ("AUTHORED_VALUE_AND_THE_DEFAULT_READERS_WHITESPACE_STRIPPED_FORM_BOTH_KEPT_BECAUSE_"
                    "WHICH_ONE_APPLIES_DEPENDS_ON_THE_CONSUMING_APPLICATION")
# `source_text` is always the authored line (or lines, for a continuation) exactly as saved. The text
# this importer parsed is beside it, because joining a continuation or splitting a brace off a header
# produces a lexical unit that no single saved line contains.
MODE_CONDITION_BASIS = ("EVERY_LINE_IN_A_SET_BLOCK_IS_ONE_ORDERED_UNEVALUATED_DECLARED_CONDITION_"
                        "EXPRESSION_LINKED_TO_ITS_MODE_SET_HEADER_NOT_A_PARAMETER_ASSIGNMENT")
ACCUMULATION_BASIS = ("ONLY_NAMES_THE_PINNED_IvPBehavior_setParam_ITSELF_HANDLES_ARE_QUALIFIED_AN_"
                      "UNKNOWN_OR_MODE_SET_NAME_IS_REPORTED_UNQUALIFIED_AND_NOTHING_IS_APPLIED_HERE")
SOURCE_EXACT = "SOURCE_TEXT_IS_THE_EXACT_AUTHORED_LINE"
SOURCE_RECONSTRUCTED = ("LEXICAL_TEXT_RECONSTRUCTED_BY_CONTINUATION_JOINING_OR_BRACE_SPLITTING_THE_"
                        "AUTHORED_LINES_THEMSELVES_ARE_IN_SOURCE_TEXT")

# Free text travels hex encoded, the convention the other declaration importers in this repo already
# use: the admitted CSV reader rejects quoted cells, and a MOOS value legitimately contains commas
# (`points = 60,-40 : 60,-160`). The encoding is lossless and reversible, so the authored bytes are
# recoverable exactly; the typed columns beside it carry the number when the literal is unambiguous.
FIELDS = ["declaration_kind", "scope_kind", "scope_name", "scope_occurrence", "setting_name",
          "setting_name_folded", "setting_value_hex", "setting_value_stripped_hex", "value_kind",
          "value_number", "assignment_order", "setting_occurrence", "accumulates_upstream",
          "expression_basis", "source_line", "source_line_end", "source_text_hex",
          "lexical_text_hex", "source_text_basis", "interpretation_basis"]

# A name or scope stays plain so rows can be selected by it; anything that would need quoting in the
# admitted reader is refused rather than reshaped.
PLAIN_CELL = re.compile(r"[^,\"\r\n]*\Z")


class Refused(ValueError):
    """A construct the saved file does not determine, or a malformed one. `code` is a fixed token."""

    def __init__(self, code, line=None):
        super().__init__(code if line is None else f"{code} at line {line}")
        self.code = code
        self.line = line


def read_bounded(path, limit=LIMIT):
    """Read at most one byte past the bound, so an oversized file is refused without reading it in."""
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Refused("INPUT_TOO_LARGE")
    return data


def text_hex(value):
    """Lossless, unquoted transport for an authored string (same helper shape as the peer importers)."""
    return "hex:" + value.encode("utf-8").hex()


def _plain(value, what, number):
    if not PLAIN_CELL.fullmatch(value):
        raise Refused(f"{what}_WOULD_NEED_A_QUOTED_CELL", number)
    return value


def _typed(value):
    """A type only for an unambiguous literal; anything else stays an authored string."""
    if INTEGER.fullmatch(value):
        return "integer", value
    if DECIMAL.fullmatch(value):
        return "decimal", value
    lowered = value.lower()
    if lowered in BOOLEAN:
        return "boolean", str(BOOLEAN[lowered])
    return "string", ""


def _strip_comment(text, number):
    """`GetNextValidLine` / `stripComment`: an inline `//` ends the line. `#` is not a comment here."""
    quote = text.find('"')
    comment = text.find("//")
    if comment == -1:
        return text
    if quote != -1 and quote < comment:
        # The pinned guard mixes a shift with a comparison here, so a `//` inside a quoted value has no
        # settled meaning: refuse instead of choosing one.
        closing = text.find('"', quote + 1)
        if closing == -1 or comment < closing:
            raise Refused("COMMENT_INSIDE_QUOTED_VALUE", number)
    return text[:comment]


def _guard_directive(stripped, number):
    """A recognized directive, checked before any comment branch: in a `.bhv` file `#` does start a
    comment, so `#include` would otherwise be dropped as one instead of refused."""
    if stripped.startswith(PREPROCESSOR):
        raise Refused("NSPLUG_PREPROCESSOR_DIRECTIVE_NOT_DETERMINED_BY_THIS_FILE", number)
    if DEFINE_LINE.match(stripped):
        raise Refused("READER_DEFINE_VARIABLE_NOT_DETERMINED_BY_THIS_FILE", number)


def _guard_unsupported(text, number):
    if SHELL_VARIABLE.search(text):
        raise Refused("SHELL_VARIABLE_EXPANSION_NOT_DETERMINED_BY_THIS_FILE", number)
    _guard_directive(text.strip(), number)


def _accumulation(folded, scope_kind):
    """Only what `IvPBehavior::setParam` settles; everything else stays a lexical name with no rule."""
    if scope_kind != "behavior":
        return ACCUMULATION_UNQUALIFIED
    name = folded[1:] if folded.startswith("_") else folded
    if name in ACCUMULATING:
        return "accumulates"
    if name in ACCUMULATING_PER_KEY:
        return "accumulates_per_key"
    if name in OVERWRITING:
        return "overwrites"
    return ACCUMULATION_UNQUALIFIED


class _Rows:
    """Accumulates declaration rows and the per-scope ordering the file itself establishes."""

    def __init__(self, capture_time_us, digest):
        self.capture_time_us = capture_time_us
        self.digest = digest
        self.rows = []
        self.counts = {}
        self.orders = {}
        self.occurrences = {}

    def add(self, kind, *, scope_kind, scope_name, scope_occurrence, name="", value=None,
            number, text, end=None, lexical=None, accumulates="", assignment=True, typed=True,
            expression=False):
        key = (scope_kind, scope_name, scope_occurrence)
        lexical = text if lexical is None else lexical
        if assignment:
            self.orders[key] = self.orders.get(key, 0) + 1
        folded = name.lower()
        seen = (key, folded)
        self.occurrences[seen] = self.occurrences.get(seen, 0) + 1 if name else 0
        stripped = "" if value is None else re.sub(r"[ \t]", "", value)
        kind_of_value, numeric = _typed(stripped) if value is not None and typed else ("", "")
        expression = (EXPRESSION_BASIS if expression or folded in EXPRESSION_PARAMETERS else "")
        if len(self.rows) >= MAX_ROWS:
            raise Refused("TOO_MANY_DECLARATION_ROWS", number)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.rows.append({
            "record_time_us": self.capture_time_us,
            "source_document_sha256": "sha256:" + self.digest,
            "declaration_kind": kind,
            "scope_kind": scope_kind,
            "scope_name": _plain(scope_name, "SCOPE_NAME", number),
            "scope_occurrence": scope_occurrence,
            "setting_name": _plain(name, "SETTING_NAME", number),
            "setting_name_folded": folded,
            "setting_value_hex": "" if value is None else text_hex(value),
            "setting_value_stripped_hex": "" if value is None else text_hex(stripped),
            "value_kind": kind_of_value,
            "value_number": numeric,
            "assignment_order": self.orders[key] if assignment else "",
            "setting_occurrence": self.occurrences[seen] if name else "",
            "accumulates_upstream": accumulates,
            "expression_basis": expression,
            "source_line": number,
            "source_line_end": number if end is None else end,
            "source_text_hex": text_hex(text),
            "lexical_text_hex": text_hex(lexical),
            "source_text_basis": SOURCE_EXACT if lexical == text else SOURCE_RECONSTRUCTED,
            "interpretation_basis": DECLARATION_BASIS,
        })


def convert_mission(text, capture_time_us, digest):
    """`.moos`: global assignment lines and `ProcessConfig = <name> { ... }` blocks (pinned reader)."""
    rows = _Rows(capture_time_us, digest)
    report = dict(comment_lines=0, blank_lines=0, process_blocks=0, global_settings=0,
                  process_settings=0)
    scope = None
    occurrence = {}
    awaiting_open = None
    for number, raw in enumerate(text.splitlines(), 1):
        if len(raw.encode("utf-8", "surrogateescape")) > MAX_LINE_BYTES:
            raise Refused("LINE_EXCEEDS_BOUND", number)
        if not raw.strip():
            report["blank_lines"] += 1
            continue
        if raw.lstrip().startswith("//"):
            report["comment_lines"] += 1
            continue
        _guard_unsupported(raw, number)
        line = _strip_comment(raw, number).strip()
        if not line:
            report["comment_lines"] += 1
            continue
        if awaiting_open is not None:
            # `GetConfiguration`: the next valid line after the header must start with `{`.
            if not line.startswith("{"):
                raise Refused("PROCESS_BLOCK_OPENING_BRACE_MISSING", number)
            if line != "{":
                raise Refused("TEXT_ON_THE_OPENING_BRACE_LINE", number)
            scope = awaiting_open
            awaiting_open = None
            continue
        if line == "}":
            if scope is None:
                raise Refused("CLOSING_BRACE_WITHOUT_AN_OPEN_BLOCK", number)
            scope = None
            continue
        if line.startswith("}"):
            # The pinned reader discards whatever follows the closing brace; refuse rather than drop.
            raise Refused("TEXT_AFTER_THE_CLOSING_BRACE", number)
        if "{" in line and "=" not in line.split("{", 1)[0]:
            raise Refused("UNEXPECTED_OPENING_BRACE", number)
        if "=" not in line:
            raise Refused("LINE_IS_NEITHER_COMMENT_ASSIGNMENT_NOR_BRACE", number)
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            raise Refused("ASSIGNMENT_WITHOUT_A_NAME", number)
        if "{" in value:
            if scope is not None:
                # Nested `{` is only traced upstream: no depth tracking, so no settled meaning.
                raise Refused("NESTED_BLOCK_INSIDE_AN_OPEN_BLOCK", number)
            if name.lower() != "processconfig":
                raise Refused("UNEXPECTED_OPENING_BRACE", number)
            # A `{` on the header line makes `GetConfiguration` fail to find the block at all.
            raise Refused("PROCESS_HEADER_CARRIES_ITS_OPENING_BRACE", number)
        if name.lower() == "processconfig":
            if scope is not None:
                raise Refused("PROCESS_HEADER_INSIDE_AN_OPEN_BLOCK", number)
            if not value:
                raise Refused("PROCESS_HEADER_WITHOUT_A_NAME", number)
            occurrence[value.lower()] = occurrence.get(value.lower(), 0) + 1
            awaiting_open = (value, occurrence[value.lower()])
            report["process_blocks"] += 1
            rows.add("mission_process_block", scope_kind="process", scope_name=value,
                     scope_occurrence=occurrence[value.lower()], number=number, text=raw, assignment=False)
            continue
        if scope is None:
            report["global_settings"] += 1
            rows.add("mission_global_setting", scope_kind="global", scope_name="",
                     scope_occurrence=1, name=name, value=value, number=number, text=raw)
        else:
            report["process_settings"] += 1
            rows.add("mission_process_setting", scope_kind="process", scope_name=scope[0],
                     scope_occurrence=scope[1], name=name, value=value, number=number, text=raw)
    if awaiting_open is not None:
        raise Refused("PROCESS_BLOCK_NEVER_OPENED")
    if scope is not None:
        raise Refused("PROCESS_BLOCK_NEVER_CLOSED")
    return rows, report


def _behavior_lines(text):
    """`fileBufferSlash`: a line ending in `\\` continues; leading/trailing braces stand alone.

    Yields `(first_line, last_line, authored, lexical, continued)`. `authored` is the saved line, or the
    saved lines joined by a newline when a continuation spans several; `lexical` is the unit this
    importer parses, which is a reconstruction whenever a continuation was joined or a brace split off.
    """
    units, pending, first, authored = [], "", None, []
    for number, raw in enumerate(text.splitlines(), 1):
        if len(raw.encode("utf-8", "surrogateescape")) > MAX_LINE_BYTES:
            raise Refused("LINE_EXCEEDS_BOUND", number)
        authored.append(raw)
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1]
            first = number if first is None else first
            continue
        start = number if first is None else first
        source = "\n".join(authored)
        lexical = pending + raw
        continued = pending != ""
        body = lexical.strip()
        if body.startswith("{") and body != "{":
            units.append((start, number, source, "{", continued))
            body, continued, lexical = body[1:].strip(), False, body[1:].strip()
        if body.endswith("{") and body != "{":
            # `Populator_BehaviorSet` splits a trailing brace off the header line; a trailing `}` after
            # text is *not* split upstream, which is why this importer refuses that shape instead.
            units.append((start, number, source, body[:-1].rstrip(), continued))
            units.append((start, number, source, "{", False))
        else:
            units.append((start, number, source, lexical, continued))
        pending, first, authored = "", None, []
    if pending:
        raise Refused("CONTINUATION_AT_END_OF_FILE")
    return units


def convert_behavior(text, capture_time_us, digest):
    """`.bhv`: `initialize` lines, `Behavior = <TYPE> { ... }` and `set <VAR> { ... }` (pinned helm)."""
    rows = _Rows(capture_time_us, digest)
    report = dict(comment_lines=0, blank_lines=0, behavior_blocks=0, behavior_settings=0,
                  initialize_lines=0, mode_sets=0, mode_conditions=0, continued_lines=0)
    scope = None
    occurrence = {}
    names = set()
    awaiting_open = None
    for number, end, source, raw, continued in _behavior_lines(text):
        if continued:
            report["continued_lines"] += 1
        if not raw.strip():
            report["blank_lines"] += 1
            continue
        lead = raw.lstrip()
        # A recognized directive first: `#` does start a comment here, so `#include` would otherwise be
        # dropped as one rather than refused.
        _guard_directive(lead, number)
        if lead.startswith("//") or lead.startswith("#"):
            report["comment_lines"] += 1
            continue
        _guard_unsupported(raw, number)
        line = _strip_comment(raw, number).strip()
        if not line:
            report["comment_lines"] += 1
            continue
        if awaiting_open is not None:
            if line != "{":
                raise Refused("BLOCK_OPENING_BRACE_MISSING", number)
            scope = awaiting_open
            awaiting_open = None
            continue
        if line == "{":
            # A bare top level brace block is silently consumed upstream as "misc": refuse instead.
            raise Refused("BRACE_IN_A_STATE_THAT_DOES_NOT_ACCEPT_ONE", number)
        if line == "}":
            if scope is None:
                raise Refused("CLOSING_BRACE_WITHOUT_AN_OPEN_BLOCK", number)
            scope = None
            continue
        if line.startswith("}") or line.endswith("{") and "=" not in line:
            raise Refused("BRACE_IN_A_STATE_THAT_DOES_NOT_ACCEPT_ONE", number)
        if line.endswith("}"):
            # Both brace-splitting counters upstream count `{`, so a `}` after text is never split off:
            # the line would silently keep the brace inside its value. Refuse instead of guessing.
            raise Refused("TEXT_BEFORE_THE_CLOSING_BRACE", number)
        keyword = line.split("=", 1)[0].strip().lower()
        opening = keyword.split(None, 1)[0] if keyword.split() else ""
        # The recognized keyword itself, not a prefix of it: `initializeBogus X = 1` is not an
        # `initialize` declaration upstream.
        if scope is None and opening in INITIALIZE_KEYWORDS:
            body = line.split("=", 1)
            if len(body) != 2 or not body[1].strip():
                raise Refused("INITIALIZE_WITHOUT_A_VALUE", number)
            variable = body[0].strip().split(None, 1)
            if len(variable) != 2:
                raise Refused("INITIALIZE_WITHOUT_A_VARIABLE", number)
            report["initialize_lines"] += 1
            rows.add("behavior_initialize", scope_kind="global", scope_name="", scope_occurrence=1,
                     name=variable[1], value=body[1].strip(), number=number, end=end, text=source,
                     lexical=raw, accumulates="deferred" if opening == "initialize_" else "")
            continue
        if scope is None and keyword == "behavior":
            kind = line.split("=", 1)[1].strip()
            if not kind or "{" in kind:
                raise Refused("BEHAVIOR_HEADER_MALFORMED", number)
            occurrence[kind.lower()] = occurrence.get(kind.lower(), 0) + 1
            awaiting_open = ("behavior", kind, occurrence[kind.lower()])
            report["behavior_blocks"] += 1
            rows.add("behavior_block", scope_kind="behavior", scope_name=kind,
                     scope_occurrence=occurrence[kind.lower()], number=number, end=end, text=source,
                     lexical=raw, assignment=False)
            continue
        if scope is None and lead.lower().startswith("set "):
            variable = lead[4:].strip()
            if "{" in variable or not variable:
                raise Refused("MODE_SET_HEADER_MALFORMED", number)
            occurrence[variable.lower()] = occurrence.get(variable.lower(), 0) + 1
            awaiting_open = ("mode_set", variable, occurrence[variable.lower()])
            report["mode_sets"] += 1
            # `ModeEntry::setHead(mode_var, mode_val)`: the head names the mode variable this block
            # declares and the value it declares for it. Without the `=` this grammar decodes no
            # target, so no variable name is claimed and the head stays explicitly unevaluated.
            mode_variable, separator, mode_value = variable.partition("=")
            rows.add("behavior_mode_set", scope_kind="mode_set", scope_name=variable,
                     scope_occurrence=occurrence[variable.lower()],
                     name=mode_variable.strip() if separator else "",
                     value=mode_value.strip() if separator else variable,
                     number=number, end=end, text=source, lexical=raw, assignment=False,
                     typed=bool(separator), expression=not separator)
            continue
        if scope is None:
            raise Refused("LINE_OUTSIDE_ANY_DECLARED_BLOCK", number)
        if scope[0] == "mode_set":
            # A `set` block has no parameters: `ModeEntry::addCondition` takes each line as one whole
            # logic condition, in order. The expression is kept as authored - no key or value is
            # invented from it, nothing is typed, the `,`/`=` rewrite is not applied and nothing is
            # evaluated. Structural and unsupported-construct refusals above already applied.
            report["mode_conditions"] += 1
            rows.add("behavior_mode_condition", scope_kind="mode_set", scope_name=scope[1],
                     scope_occurrence=scope[2], value=line, number=number, end=end, text=source,
                     lexical=raw, typed=False, expression=True)
            continue
        if "=" not in line:
            raise Refused("BLOCK_LINE_WITHOUT_AN_ASSIGNMENT", number)
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            raise Refused("ASSIGNMENT_WITHOUT_A_NAME", number)
        if name.lower() == "name":
            if value.lower() in names:
                raise Refused("DUPLICATE_BEHAVIOR_NAME", number)
            names.add(value.lower())
        report["behavior_settings"] += 1
        rows.add("behavior_setting", scope_kind="behavior", scope_name=scope[1],
                 scope_occurrence=scope[2], name=name, value=value, number=number, end=end,
                 text=source, lexical=raw, accumulates=_accumulation(name.lower(), "behavior"))
    if awaiting_open is not None:
        raise Refused("BLOCK_NEVER_OPENED")
    if scope is not None:
        raise Refused("BLOCK_NEVER_CLOSED")
    return rows, report


def convert(data, capture_time_us, *, declaration_format):
    """Saved mission/behavior bytes -> (CSV for the common reader, JSON-ready accounting)."""
    if declaration_format not in ("moos-mission", "moos-behavior"):
        raise Refused("EXPLICIT_DECLARATION_FORMAT_REQUIRED")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2 ** 63:
        raise Refused("EXPLICIT_CAPTURE_TIME_REQUIRED")
    if not isinstance(data, bytes) or not data or len(data) > LIMIT:
        raise Refused("BOUNDED_DECLARATION_BYTES_REQUIRED")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise Refused("INVALID_UTF8_DECLARATION_FILE") from error
    digest = hashlib.sha256(data).hexdigest()
    builder = convert_mission if declaration_format == "moos-mission" else convert_behavior
    rows, counts = builder(text, capture_time_us, digest)
    if not rows.rows:
        raise Refused("NO_DECLARATION_IN_THE_SELECTED_FILE")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, ["record_time_us", "source_document_sha256", *FIELDS],
                            lineterminator="\n")
    writer.writeheader()
    for row in rows.rows:
        writer.writerow(row)
        if output.tell() > OUTPUT_LIMIT:
            raise Refused("DECLARATION_CSV_EXCEEDS_BOUND")
    report = dict(
        pin=PIN, declaration_format=declaration_format, source_sha256="sha256:" + digest,
        source_bytes=len(data), declaration_rows=len(rows.rows),
        rows_by_kind=dict(sorted(rows.counts.items())), **counts,
        capture_time_us=capture_time_us,
        clock="CALLER_DECLARED_CAPTURE_MICROSECONDS_NO_TIME_EXISTS_IN_THE_SAVED_FILE",
        interpretation_basis=DECLARATION_BASIS,
        global_lookup_basis=GLOBAL_LOOKUP_BASIS,
        whitespace_basis=WHITESPACE_BASIS,
        expression_basis=EXPRESSION_BASIS,
        accumulation_basis=ACCUMULATION_BASIS,
        mode_condition_basis=MODE_CONDITION_BASIS,
        source_text_basis=("EVERY_ROW_CARRIES_THE_AUTHORED_LINES_IN_SOURCE_TEXT_AND_THE_UNIT_THIS_"
                           "IMPORTER_PARSED_IN_LEXICAL_TEXT_WITH_ITS_OWN_PER_ROW_BASIS"),
        source_retention="unchanged source file retained beside the CSV and this report")
    if len(json.dumps(report)) > REPORT_LIMIT:
        raise Refused("REPORT_EXCEEDS_BOUND")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved .moos mission or .bhv behavior file")
    parser.add_argument("new_output_directory", type=Path,
                        help="directory to create; existing inputs and evidence are never overwritten")
    parser.add_argument("--declaration-format", required=True,
                        choices=("moos-mission", "moos-behavior"))
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="the operator's capture clock; nothing in the saved file is a timestamp")
    args = parser.parse_args()
    created = False
    try:
        data = read_bounded(args.input)
        converted, report = convert(data, args.capture_time_us,
                                    declaration_format=args.declaration_format)
        args.new_output_directory.mkdir()  # Preserve existing inputs and evidence.
        created = True
        (args.new_output_directory / "observations.csv").write_text(converted, encoding="utf-8")
        (args.new_output_directory / "accounting.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (args.new_output_directory / "source.declaration").write_bytes(data)
    except Refused as refusal:
        parser.exit(2, f"saved MOOS declaration import refused: {refusal.code}\n")
    except (ValueError, OSError):
        # Only claim nothing was written when nothing could have been: once the output directory
        # exists, a failure can leave it partly filled, and saying otherwise would be false.
        if created:
            parser.exit(2, "saved MOOS declaration import failed after creating "
                           f"{args.new_output_directory}; its contents are incomplete\n")
        parser.exit(2, "saved MOOS declaration import failed; nothing was written\n")
    print(f"imported {report['declaration_rows']} declaration rows "
          f"({report['declaration_format']}, pin {report['pin']})")


if __name__ == "__main__":
    main()
