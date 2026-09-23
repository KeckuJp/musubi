"""A saved CAP 1.2 alert message -> common observations. Passive reading of a file, nothing else.

This converter NEVER contacts an alerting service, never publishes, never acknowledges, never
dispatches and never decides anything. It reads bytes a producer already wrote and carries what
that producer DECLARED into common output with the declaration kept attached.

What the source means is fixed by one official definition, pinned here and not re-authored:

  OASIS Common Alerting Protocol Version 1.2, OASIS Standard, 01 July 2010
  schema  CAP-v1.2.xsd  sha256 b7798ef25868b068c97b268bda02d067c7d4ba9373adc5638bf37105804ee723
  namespace urn:oasis:names:tc:emergency:cap:1.2

Four facts from that definition are load-bearing here, and each is enforced rather than assumed:

  * `category` is 1..* -- a CAP info block may carry SEVERAL categories. Reading only the first
    would silently discard the producer's classification, so every category is carried.
  * `urgency`, `severity` and `certainty` each define `Unknown` as a VALUE. A declared `Unknown`
    is a statement the producer made; it is not the same as an absent element, and the two are
    never merged here.
  * a coordinate pair is `[latitude],[longitude]` in decimal degrees, unprojected, WGS 84
    (EPSG:4326, 2 dimensions); a circle is that pair, a space, then a radius in KILOMETRES;
    altitude and ceiling are FEET above mean sea level per the same datum.
  * every dateTime MUST carry a numeric offset -- CAP states that `Z` MUST NOT be used and that
    UTC MUST be written `-00:00`. So a CAP time is anchored by the producer's own declaration,
    and no clock is invented here to place it.

What this is NOT. A CAP message is a REPORT. `severity`, `certainty` and `category` are the
sending party's own classification of something they say happened; this converter carries that
classification and never confirms it. Nothing here is evidence that an incident occurred, that a
person or object was found, or that any area is actually affected. No operational recommendation
is produced: `responseType` is carried because the producer wrote it, and it remains the
producer's word, never this tool's advice. An identifier, `geocode` valueName or `parameter`
naming any particular downstream system is a producer-assigned string only -- CAP itself says
`geocode` "presumes knowledge of the coding system on the part of recipients" -- so such a name
is never read here as proof of compatibility with the system it names.

NOT A FULL CAP READER, and the recovery contract is explicit. This path gives a semantic column
to the named subset of elements listed in `PARSED_*` below. Every other element the pinned schema
defines -- `headline`, `description`, `instruction`, `parameter`, `resource`, `contact`, `code`,
`note` and the rest -- is COUNTED BY NAME and is NOT carried: its text does not appear in any
column, and no digest of it is a substitute for it. Recovering those values requires the original
file, which the CALLER retains; `source_sha256` and `source_path` identify exactly which file and
which element position to go back to. The same applies to any element outside the pinned schema,
which is counted as unknown rather than dropped silently.
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
import xml.etree.ElementTree as ElementTree
import xml.parsers.expat as expat

NAMESPACE = "urn:oasis:names:tc:emergency:cap:1.2"
# Joining values with a separator is only safe for a CONTROLLED vocabulary, where no member can
# contain the separator. Enumerations qualify. Free text and numbers do NOT: a `geocode` valueName
# may contain a `+`, and so may an exponent (`1e+2`), so repeats of those are carried as a
# hex-encoded JSON list, which is reversible and keeps the grouping of a pair together.
KIND_SEPARATOR = "+"
NEEDS_QUOTING = re.compile(r'[,"\r\n]')

# The pinned schema's own enumerations, copied from CAP-v1.2.xsd. A value outside them is refused
# rather than passed through, because the whole point of these columns is the declared meaning.
STATUS_VALUES = ("Actual", "Exercise", "System", "Test", "Draft")
MSG_TYPES = ("Alert", "Update", "Cancel", "Ack", "Error")
SCOPES = ("Public", "Restricted", "Private")
CATEGORIES = ("Geo", "Met", "Safety", "Security", "Rescue", "Fire", "Health", "Env",
              "Transport", "Infra", "CBRNE", "Other")
RESPONSE_TYPES = ("Shelter", "Evacuate", "Prepare", "Execute", "Avoid", "Monitor", "Assess",
                  "AllClear", "None")
URGENCIES = ("Immediate", "Expected", "Future", "Past", "Unknown")
SEVERITIES = ("Extreme", "Severe", "Moderate", "Minor", "Unknown")
CERTAINTIES = ("Observed", "Likely", "Possible", "Unlikely", "Unknown")

# CAP: "YYYY-MM-DDThh:mm:ssXzh:zm". The offset is mandatory and alphabetic designators are
# forbidden, so `Z` and a bare local time are both refused by this pattern rather than guessed at.
CAP_DATETIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?([+-])(\d{2}):(\d{2})$")
# CAP: identifier and sender "MUST NOT include spaces, commas or restricted characters".
CAP_IDENTIFIER = re.compile(r"[^\s,<&]{1,128}$")
# A decimal number as the producer wrote it. The original text is emitted unchanged so that no
# precision is silently gained or lost between the source and the common output.
DECIMAL_TEXT = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")

EPOCH_ORDINAL = datetime.date(1970, 1, 1).toordinal()
# The largest real UTC offset is +/-14:00, so 14 hours permits no minutes beside it.
MAX_OFFSET_MINUTES = 14 * 60

# Finite bounds, so a hostile or broken file cannot make this converter allocate without limit.
MAX_INPUT = 16 * 1024 * 1024
MAX_INFO = 64
MAX_AREA = 256
MAX_GEOMETRY = 256
MAX_POLYGON_POINTS = 4096
# The binding cap on what this converter will WRITE. It is deliberately below the product of the
# two bounds above (64 x 256 = 16384), because a bound that the per-element limits could never
# reach would not be a bound at all. No real alert approaches it.
MAX_ROWS = 4096
# A row count is NOT a memory bound, because one alert can amplify. A single `event` or `source`
# string is declared once in the source but reaches EVERY row built from it, and hex doubles it on
# the way: a modest input can expand into an output orders of magnitude larger. So the expanded
# size carries its own byte budget, checked while writing rather than after the whole output has
# been built. Passing it is REFUSED; nothing is ever truncated to fit.
MAX_OUTPUT = 64 * 1024 * 1024

# Which elements of the pinned schema this path gives a semantic column to, which it counts as a
# known omission, and which may legitimately repeat. Anything not named at all is counted unknown.
# A singleton declared twice is REFUSED: the pinned schema permits one, and quietly keeping either
# the first or the last would hide a value the producer wrote.
PARSED_ALERT = ("identifier", "sender", "sent", "status", "msgType", "source", "scope",
                "references", "incidents", "info")
OMITTED_ALERT = ("restriction", "addresses", "note", "code")
REPEATABLE_ALERT = ("info", "code")
PARSED_INFO = ("language", "event", "urgency", "severity", "certainty", "effective", "onset",
               "expires", "category", "responseType", "area")
OMITTED_INFO = ("audience", "senderName", "headline", "description", "instruction", "web",
                "contact", "eventCode", "parameter", "resource")
REPEATABLE_INFO = ("category", "responseType", "area", "eventCode", "parameter", "resource")
PARSED_AREA = ("areaDesc", "altitude", "ceiling", "polygon", "circle", "geocode")
OMITTED_AREA = ()
REPEATABLE_AREA = ("polygon", "circle", "geocode")

RECOVERY_BASIS = (
    "THE_COUNTED_ELEMENTS_ARE_NOT_CARRIED_IN_ANY_COLUMN_this_path_gives_a_semantic_column_to_a_"
    "NAMED_SUBSET_of_the_pinned_schema_and_every_other_element_is_counted_by_name_only_its_text_"
    "is_NOT_in_this_output_and_no_digest_of_it_is_a_substitute_for_it_recovering_those_values_"
    "requires_THE_ORIGINAL_FILE_WHICH_THE_CALLER_RETAINS_identified_by_source_sha256_at_the_"
    "element_position_given_by_source_path_so_this_row_is_never_a_claim_that_the_whole_message_"
    "was_read_or_that_nothing_was_left_behind")

CLASSIFICATION_BASIS = (
    "REPORTED_BY_THE_SENDING_PARTY_AND_NOT_INDEPENDENTLY_VERIFIED_the_category_severity_urgency_"
    "and_certainty_on_this_row_are_the_senders_own_classification_of_what_they_say_happened_they_"
    "are_never_a_confirmed_fact_never_evidence_that_an_incident_occurred_never_evidence_that_any_"
    "person_or_object_was_found_or_located_and_never_this_tools_own_assessment_or_recommendation")

NOT_A_SERVICE = "NONE_THIS_TOOL_PUBLISHES_NOTHING_ACKNOWLEDGES_NOTHING_AND_DISPATCHES_NOTHING"

FIELDS = (
    "record_time_us",
    "cap_row_kind",
    "alert_identifier",
    "alert_sender",
    "alert_sent_text",
    "alert_sent_offset",
    "cap_time_basis",
    "alert_status",
    "alert_msg_type",
    "alert_scope",
    "alert_source_hex",
    "alert_references_hex",
    "alert_reference_count",
    "alert_incidents_hex",
    "update_basis",
    "info_index",
    "info_count",
    "info_language_hex",
    "info_categories",
    "info_category_count",
    "info_event_hex",
    "info_response_types",
    "info_urgency",
    "info_severity",
    "info_certainty",
    "declared_unknown_basis",
    "info_effective_text",
    "info_onset_text",
    "info_expires_text",
    "area_index",
    "area_count",
    "area_desc_hex",
    "area_circle_count",
    "area_circle_latitude_deg",
    "area_circle_longitude_deg",
    "area_circle_radius_km",
    "area_circle_list_hex",
    "area_polygon_count",
    "area_polygon_point_count",
    "area_polygon_list_hex",
    "area_geocode_count",
    "area_geocode_pairs_hex",
    "area_altitude_ft",
    "area_ceiling_ft",
    "geometry_basis",
    "classification_basis",
    "source_path",
    "omitted_known_count",
    "omitted_known_hex",
    "unknown_element_count",
    "unknown_element_hex",
    "recovery_basis",
    "source_sha256",
)


def hexed(value):
    """Free text from the producer, carried as this estate's `hex:` form.

    Several CAP fields legitimately contain commas, quotes or whitespace -- `references` is a
    list of `sender,identifier,sent` triples, and `incidents` may double-quote a name containing
    spaces -- and the common reader refuses quoted CSV input. Hex keeps the exact bytes without
    needing quoting, so nothing has to be stripped or rewritten to make it fit a cell.
    """
    if value is None:
        return ""
    return "hex:" + value.encode("utf-8").hex()


def plain(value, what):
    """A value the pinned schema promises is already cell-safe -- checked, never assumed."""
    if NEEDS_QUOTING.search(value):
        raise ValueError("%s contains a character the common reader cannot read: %r" % (what, value))
    return value


def text_of(element):
    """The text of a SCALAR element, or None when the element is absent.

    An element that is present but empty returns "" and is NOT collapsed into absence: a producer
    who wrote an empty element said something different from a producer who wrote nothing.

    Every field this is called for is one the pinned schema declares as a simple type, so it has
    no children. If one has children anyway, reading `.text` alone would return the text before
    the first child and silently drop the child and everything after it -- `<event>x<extension>
    ...</extension></event>` would become `x`, with no count and no trace. That is refused. It is
    the simplest correct answer: the container elements that legitimately hold children (`info`,
    `area`, `geocode`) never reach this function, and an omitted subtree is still counted once by
    `scan_children` without being read.
    """
    if element is None:
        return None
    if len(element):
        raise ValueError(
            "the pinned schema declares %s as a simple value, but it contains %d nested element(s); "
            "reading only its leading text would silently discard them, so it is refused"
            % (element.tag.rsplit("}", 1)[-1], len(element)))
    return element.text or ""


def hexed_list(values):
    """Repeats carried REVERSIBLY, as a hex-encoded JSON list.

    Joining with a separator is only lossless over a controlled vocabulary. These values are free
    text and numbers written by the producer: a `geocode` valueName may contain a `+`, and so may
    an exponent such as `1e+2`. A JSON list round-trips regardless of the characters inside it and
    keeps the grouping of a pair together, which a flattened join would destroy.
    """
    if not values:
        return ""
    return hexed(json.dumps(values, ensure_ascii=False, separators=(",", ":")))


def child(parent, name):
    return parent.find("{%s}%s" % (NAMESPACE, name))


def scan_children(element, parsed, omitted, repeatable, level, counted):
    """Account for EVERY child: parsed, counted as a known omission, or counted as unknown.

    Without this, a duplicate singleton and an element outside the pinned schema would both simply
    vanish -- `find` returns the first and ignores the rest, and nothing looks at a name the
    converter has no column for. A reader of the output could not tell a message this path read
    whole from one it read a third of.
    """
    counts = {}
    for item in element:
        tag = item.tag
        if not isinstance(tag, str):
            # A comment or processing instruction carries no CAP value; no handler produces one.
            continue
        if tag.startswith("{%s}" % NAMESPACE):
            local = tag[len(NAMESPACE) + 2:]
        else:
            counted["unknown"][tag] = counted["unknown"].get(tag, 0) + 1
            continue
        counts[local] = counts.get(local, 0) + 1
        known = local in parsed or local in omitted
        if not known:
            counted["unknown"][tag] = counted["unknown"].get(tag, 0) + 1
            continue
        if local not in repeatable and counts[local] > 1:
            raise ValueError(
                "the %s declares %s %d times; the pinned schema permits at most one, and a second "
                "value is refused rather than one of them being silently ignored"
                % (level, local, counts[local]))
    for name in omitted:
        if name in counts:
            key = "%s/%s" % (level, name)
            counted["omitted"][key] = counted["omitted"].get(key, 0) + counts[name]
    return counts


def children(parent, name):
    return parent.findall("{%s}%s" % (NAMESPACE, name))


def enum_value(element, allowed, what, required):
    value = text_of(element)
    if value is None:
        if required:
            raise ValueError("the pinned schema requires %s and the alert has none" % what)
        return ""
    value = value.strip()
    if value not in allowed:
        raise ValueError("%s is %r, which the pinned schema does not define" % (what, value))
    return value


def cap_time_us(value, what):
    """A CAP dateTime -> integer microseconds, by exact integer arithmetic.

    The offset is part of the producer's declaration, so the instant is theirs, not one this
    converter chose. Sub-microsecond precision is refused rather than rounded away.
    """
    match = CAP_DATETIME.fullmatch(value.strip())
    if not match:
        raise ValueError(
            "%s is %r, which is not a CAP dateTime; the pinned schema requires a numeric offset "
            "and forbids an alphabetic designator such as Z" % (what, value))
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    fraction, sign, offset_hour, offset_minute = match.group(7), match.group(8), int(
        match.group(9)), int(match.group(10))
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("%s is %r, which is not a real time of day" % (what, value))
    # The largest real UTC offset is +/-14:00. Bounding the hour and the minute separately would
    # admit 14:59, which is not an offset any zone uses, so the total is what is bounded.
    if offset_minute > 59 or offset_hour * 60 + offset_minute > MAX_OFFSET_MINUTES:
        raise ValueError("%s has offset %r, which is outside the range of real UTC offsets"
                         % (what, value))
    try:
        ordinal = datetime.date(year, month, day).toordinal()
    except ValueError as error:
        raise ValueError("%s is %r, which is not a real date: %s" % (what, value, error))
    microseconds = int((fraction or ".")[1:].ljust(6, "0") or "0") if fraction else 0
    offset = (offset_hour * 3600 + offset_minute * 60) * (1 if sign == "+" else -1)
    seconds = (ordinal - EPOCH_ORDINAL) * 86400 + hour * 3600 + minute * 60 + second - offset
    result = seconds * 1000000 + microseconds
    if result < 0:
        raise ValueError("%s is %r, which is before the epoch the common reader counts from"
                         % (what, value))
    return result


def offset_form(value):
    """Which of CAP's two UTC spellings the producer used, kept visible rather than normalised."""
    tail = value.strip()[-6:]
    if tail == "-00:00":
        return "UTC_WRITTEN_MINUS_ZERO_EXACTLY_AS_THE_PINNED_SCHEMA_REQUIRES"
    if tail == "+00:00":
        return "UTC_WRITTEN_PLUS_ZERO_WHICH_NAMES_THE_SAME_INSTANT_BUT_DEVIATES_FROM_THE_PINNED_SCHEMA"
    return "NUMERIC_OFFSET_AS_THE_PRODUCER_DECLARED_IT"


def declared_time(element, what):
    """An optional CAP time: validated in full, then emitted as the producer's own text."""
    value = text_of(element)
    if value is None:
        return ""
    cap_time_us(value, what)
    return plain(value.strip(), what)


def decimal_text(value, what, low=None, high=None, bound=""):
    """Validate a number EXACTLY, then emit the producer's ORIGINAL text.

    This follows the discipline the adopted saved-inspection-features converter already uses. The
    magnitude is carried through `decimal.Decimal`, never through a float, because:

      * a float turns a lexically nonzero magnitude such as `1e-400` into `0`, silently changing
        "a very small number" into "exactly zero";
      * a float rounds a value just outside a range INTO that range -- `90.00000000000000001` is
        not a latitude, and only a `Decimal` comparison says so;
      * re-formatting would change the digits the producer wrote, so the source text is what
        reaches the cell and only its shape and magnitude are checked.

    A magnitude the common reader could not represent is refused rather than quietly flattened.
    """
    value = value.strip()
    if not DECIMAL_TEXT.fullmatch(value):
        raise ValueError("%s is %r, which is not a decimal number" % (what, value))
    try:
        quantity = decimal.Decimal(value)
    except decimal.InvalidOperation:
        raise ValueError("%s is %r, which is not a decimal number" % (what, value))
    if not quantity.is_finite():
        raise ValueError("%s is %r, which is not finite" % (what, value))
    if low is not None and quantity < decimal.Decimal(low):
        raise ValueError("%s is %r, below the range %s" % (what, value, bound))
    if high is not None and quantity > decimal.Decimal(high):
        raise ValueError("%s is %r, above the range %s" % (what, value, bound))
    reachable = float(value)
    if not math.isfinite(reachable):
        raise ValueError("%s is %r, which overflows the reader's float and is refused rather than "
                         "carried as an infinity" % (what, value))
    if reachable == 0 and quantity != 0:
        raise ValueError("%s is %r, which underflows to zero at the reader and is refused rather "
                         "than carried as an exact zero it is not" % (what, value))
    return value, quantity


def coordinate_pair(value, what):
    """`[latitude],[longitude]` in decimal degrees, WGS 84, per the pinned schema's terminology."""
    parts = value.split(",")
    if len(parts) != 2:
        raise ValueError("%s is %r, which is not a [latitude],[longitude] pair" % (what, value))
    latitude, latitude_value = decimal_text(
        parts[0], "%s latitude" % what, -90, 90, "a WGS 84 geographic latitude occupies")
    longitude, longitude_value = decimal_text(
        parts[1], "%s longitude" % what, -180, 180, "a WGS 84 geographic longitude occupies")
    return (latitude, longitude), (latitude_value, longitude_value)


def parse_circle(value):
    """A CAP circle: coordinate pair, a space, then a radius in kilometres."""
    parts = value.split()
    if len(parts) != 2:
        raise ValueError("circle %r is not a coordinate pair followed by a radius" % value)
    (latitude, longitude), _ = coordinate_pair(parts[0], "circle centre")
    # The pinned schema gives no maximum radius, so none is invented here; a negative one is not a
    # radius. The finite-and-reachable check inside `decimal_text` protects the magnitude itself.
    radius, _ = decimal_text(parts[1], "circle radius", 0, None, "a real radius occupies")
    return latitude, longitude, radius


def parse_polygon(value):
    """A CAP polygon: >= 4 whitespace-delimited pairs, first and last equal IN VALUE.

    Closure is compared as a pair of `Decimal` magnitudes, not as text. The pinned schema requires
    the first and last pairs to *be the same*, which is a statement about the point and not about
    its spelling: `38.47,-120.14` and `38.470,-120.140` are one point written two ways, and
    refusing the second as "not closed" would reject a conforming polygon.
    """
    pairs = value.split()
    if len(pairs) < 4:
        raise ValueError(
            "polygon has %d coordinate pairs; the pinned schema requires at least 4" % len(pairs))
    if len(pairs) > MAX_POLYGON_POINTS:
        raise ValueError("polygon has more coordinate pairs than the finite bound")
    corners = [coordinate_pair(pair, "polygon point")[1] for pair in pairs]
    if corners[0] != corners[-1]:
        raise ValueError("polygon does not close: its first and last coordinate pairs differ in "
                         "value, which the pinned schema requires them not to do")
    return len(pairs)


def update_basis(msg_type, references, reference_count):
    """How this message relates to earlier ones, from what the producer actually declared."""
    if msg_type in ("Update", "Cancel"):
        if references is None:
            return ("%s_DECLARING_NO_REFERENCES_so_the_earlier_message_it_replaces_or_cancels_is_"
                    "NOT_IDENTIFIED_BY_THIS_FILE_and_none_is_guessed_here" % msg_type.upper())
        return ("%s_REFERENCING_%d_EARLIER_MESSAGE_IDENTIFIERS_carried_verbatim_the_referenced_"
                "messages_themselves_are_not_in_this_file_and_are_not_retrieved"
                % (msg_type.upper(), reference_count))
    if references is not None:
        return ("%s_WITH_%d_REFERENCES_the_producer_related_this_message_to_earlier_ones_without_"
                "declaring_it_an_update_or_cancellation" % (msg_type.upper(), reference_count))
    return ("%s_WITH_NO_REFERENCES_this_file_does_not_say_whether_an_earlier_related_message_"
            "exists_and_absence_of_a_reference_is_never_read_as_proof_that_this_is_the_first"
            % msg_type.upper())


def declared_unknown_basis(urgency, severity, certainty):
    """`Unknown` is a value CAP defines. Saying it is not the same as saying nothing."""
    stated = [name for name, value in (("urgency", urgency), ("severity", severity),
                                       ("certainty", certainty)) if value == "Unknown"]
    if not stated:
        return ("NONE_the_producer_gave_a_specific_value_for_urgency_severity_and_certainty")
    return ("THE_PRODUCER_EXPLICITLY_DECLARED_Unknown_FOR_" + KIND_SEPARATOR.join(stated)
            + "_which_the_pinned_schema_defines_as_a_value_meaning_they_state_they_do_not_know_"
              "and_is_NEVER_the_same_as_the_element_being_absent_or_as_this_tool_not_reading_it")


def geometry_basis(circles, polygons, geocodes, has_area):
    if not has_area:
        return ("NO_AREA_BLOCK_the_producer_declared_no_location_for_this_info_block_and_none_is_"
                "inferred_from_the_sender_the_event_or_anything_else")
    present = []
    if circles:
        present.append("CIRCLE_WGS84_CENTRE_WITH_RADIUS_IN_KILOMETRES")
    if polygons:
        present.append("POLYGON_WGS84_EPSG4326_LATITUDE_LONGITUDE_PAIRS")
    if geocodes:
        present.append("GEOCODE_WHOSE_MEANING_THE_PINNED_SCHEMA_SAYS_PRESUMES_KNOWLEDGE_OF_THE_"
                       "CODING_SYSTEM_ON_THE_PART_OF_THE_RECIPIENT_SO_THE_NAME_OF_A_CODING_SYSTEM_"
                       "HERE_IS_A_PRODUCER_ASSIGNED_STRING_AND_NOT_PROOF_OF_COMPATIBILITY_WITH_"
                       "ANY_SYSTEM_IT_NAMES")
    if not present:
        return ("AREA_DESCRIPTION_ONLY_the_producer_named_an_area_in_free_text_and_declared_no_"
                "machine_readable_geometry_so_no_coordinates_are_derived_from_the_description")
    return KIND_SEPARATOR.join(present)


def read_alert(data):
    """Parse the saved bytes into a CAP 1.2 alert element, refusing anything else.

    A document type declaration can define entities, and expanding an attacker's entities is not
    something a passive reader needs to do. The refusal is made by the PARSER, not by scanning the
    bytes for `<!DOCTYPE`: a byte scan has to guess the encoding, and would miss a declaration in
    UTF-16 or one pushed past a fixed prefix by a long comment. expat has already decoded the
    document by the time these handlers run, so the refusal holds for every encoding it accepts
    and for a declaration at any offset. No XML library is added -- expat and `TreeBuilder` are
    both stdlib, and `TreeBuilder` yields the same elements the rest of this file already reads.
    """
    if not isinstance(data, bytes):
        raise ValueError("a saved CAP file is read as bytes")
    if not data.strip():
        raise ValueError("the saved CAP file is empty")
    if len(data) > MAX_INPUT:
        raise ValueError("the saved CAP file is %d bytes, past the %d byte input bound"
                         % (len(data), MAX_INPUT))

    def refuse_doctype(*_unused):
        raise ValueError("the saved file declares a DOCTYPE, which this converter refuses to read")

    def refuse_entity(*_unused):
        raise ValueError("the saved file declares an XML entity, which this converter refuses to "
                         "read rather than expand")

    builder = ElementTree.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.EntityDeclHandler = refuse_entity
    parser.UnparsedEntityDeclHandler = refuse_entity
    parser.ExternalEntityRefHandler = refuse_entity
    # expat reports a namespaced name as `uri}local`; ElementTree spells the same thing `{uri}local`.
    parser.StartElementHandler = lambda name, attributes: builder.start(
        ("{" + name) if "}" in name else name, attributes)
    parser.EndElementHandler = lambda name: builder.end(("{" + name) if "}" in name else name)
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(data, True)
        root = builder.close()
    except expat.ExpatError as error:
        raise ValueError("the saved file is not well-formed XML: %s" % error)
    expected = "{%s}alert" % NAMESPACE
    if root.tag != expected:
        raise ValueError(
            "the saved file's root element is %r; this converter reads only %r, and an alert in a "
            "different CAP version is NOT read as if it were 1.2" % (root.tag, expected))
    return root


def alert_columns(root, counted):
    """The alert-level declaration, shared by every row this file produces."""
    scan_children(root, PARSED_ALERT, OMITTED_ALERT, REPEATABLE_ALERT, "alert", counted)
    identifier = text_of(child(root, "identifier"))
    sender = text_of(child(root, "sender"))
    sent = text_of(child(root, "sent"))
    for value, what in ((identifier, "identifier"), (sender, "sender"), (sent, "sent")):
        if value is None:
            raise ValueError("the pinned schema requires %s and the alert has none" % what)
    for value, what in ((identifier, "identifier"), (sender, "sender")):
        if not CAP_IDENTIFIER.fullmatch(value):
            raise ValueError(
                "%s is %r; the pinned schema says it must not include spaces, commas or the "
                "restricted characters < and &" % (what, value))
    references = text_of(child(root, "references"))
    # CAP: whitespace-separated extended message identifiers, each `sender,identifier,sent`.
    reference_count = len(references.split()) if references else 0
    msg_type = enum_value(child(root, "msgType"), MSG_TYPES, "msgType", True)
    return {
        "alert_identifier": identifier,
        "alert_sender": sender,
        "alert_sent_text": plain(sent.strip(), "sent"),
        "alert_sent_offset": plain(sent.strip()[-6:], "sent offset"),
        "cap_time_basis": offset_form(sent),
        "alert_status": enum_value(child(root, "status"), STATUS_VALUES, "status", True),
        "alert_msg_type": msg_type,
        "alert_scope": enum_value(child(root, "scope"), SCOPES, "scope", True),
        "alert_source_hex": hexed(text_of(child(root, "source"))),
        "alert_references_hex": hexed(references),
        "alert_reference_count": reference_count,
        "alert_incidents_hex": hexed(text_of(child(root, "incidents"))),
        "update_basis": update_basis(msg_type, references, reference_count),
        "record_time_us": cap_time_us(sent, "sent"),
        "classification_basis": CLASSIFICATION_BASIS,
    }


def info_columns(info, index, total, counted):
    """One CAP info block's declared classification, kept whole."""
    scan_children(info, PARSED_INFO, OMITTED_INFO, REPEATABLE_INFO, "info", counted)
    categories = [enum_value(element, CATEGORIES, "category", True)
                  for element in children(info, "category")]
    if not categories:
        raise ValueError("the pinned schema requires at least one category and the info block "
                         "declares none")
    event = text_of(child(info, "event"))
    if event is None:
        raise ValueError("the pinned schema requires event and the info block has none")
    responses = [enum_value(element, RESPONSE_TYPES, "responseType", True)
                 for element in children(info, "responseType")]
    urgency = enum_value(child(info, "urgency"), URGENCIES, "urgency", True)
    severity = enum_value(child(info, "severity"), SEVERITIES, "severity", True)
    certainty = enum_value(child(info, "certainty"), CERTAINTIES, "certainty", True)
    return {
        "info_index": index,
        "info_count": total,
        "info_language_hex": hexed(text_of(child(info, "language"))),
        # Every category, never only the first: the pinned schema allows several and dropping
        # any of them would discard part of the producer's own classification.
        "info_categories": KIND_SEPARATOR.join(categories),
        "info_category_count": len(categories),
        "info_event_hex": hexed(event),
        "info_response_types": KIND_SEPARATOR.join(responses) if responses else "NONE_DECLARED",
        "info_urgency": urgency,
        "info_severity": severity,
        "info_certainty": certainty,
        "declared_unknown_basis": declared_unknown_basis(urgency, severity, certainty),
        "info_effective_text": declared_time(child(info, "effective"), "effective"),
        "info_onset_text": declared_time(child(info, "onset"), "onset"),
        "info_expires_text": declared_time(child(info, "expires"), "expires"),
    }


def area_columns(area, index, total, counted):
    """One CAP area block: its description and whichever geometry forms the producer declared."""
    scan_children(area, PARSED_AREA, OMITTED_AREA, REPEATABLE_AREA, "area", counted)
    description = text_of(child(area, "areaDesc"))
    if description is None:
        raise ValueError("the pinned schema requires areaDesc and the area block has none")
    circles = [(text_of(element) or "").strip() for element in children(area, "circle")]
    polygons = [(text_of(element) or "").strip() for element in children(area, "polygon")]
    geocodes = children(area, "geocode")
    for group, what in ((circles, "circle"), (polygons, "polygon"), (geocodes, "geocode")):
        if len(group) > MAX_GEOMETRY:
            raise ValueError("the area declares more %s elements than the finite bound" % what)
    parsed_circles = [parse_circle(value) for value in circles]
    polygon_points = sum(parse_polygon(value) for value in polygons)

    pairs = []
    for geocode in geocodes:
        scan_children(geocode, ("valueName", "value"), (), (), "geocode", counted)
        name, value = text_of(child(geocode, "valueName")), text_of(child(geocode, "value"))
        if name is None or value is None:
            raise ValueError("the pinned schema requires both valueName and value in a geocode")
        # Kept as a pair, not as two parallel joined lists: a name and its value belong together,
        # and either of them may itself contain whatever character a join would have used.
        pairs.append([name, value])

    altitude = text_of(child(area, "altitude"))
    ceiling = text_of(child(area, "ceiling"))
    if ceiling is not None and altitude is None:
        raise ValueError("the area declares a ceiling without an altitude, which the pinned "
                         "schema says MUST NOT be done")
    columns = {
        "area_index": index,
        "area_count": total,
        "area_desc_hex": hexed(description),
        "area_circle_count": len(circles),
        # Split out only when there is exactly one circle. With several, picking one would be an
        # invention, so the columns stay blank and the raw text below keeps all of them.
        "area_circle_latitude_deg": parsed_circles[0][0] if len(parsed_circles) == 1 else "",
        "area_circle_longitude_deg": parsed_circles[0][1] if len(parsed_circles) == 1 else "",
        "area_circle_radius_km": parsed_circles[0][2] if len(parsed_circles) == 1 else "",
        "area_circle_list_hex": hexed_list(circles),
        "area_polygon_count": len(polygons),
        "area_polygon_point_count": polygon_points,
        "area_polygon_list_hex": hexed_list(polygons),
        "area_geocode_count": len(geocodes),
        "area_geocode_pairs_hex": hexed_list(pairs),
        # The pinned schema states no altitude range, so none is invented; `decimal_text` still
        # refuses a magnitude that would reach the reader as an infinity or as a false zero.
        "area_altitude_ft": decimal_text(altitude, "altitude")[0] if altitude is not None else "",
        "area_ceiling_ft": decimal_text(ceiling, "ceiling")[0] if ceiling is not None else "",
        "geometry_basis": geometry_basis(circles, polygons, geocodes, True),
    }
    return columns


def blank_area_columns(has_area):
    columns = {name: "" for name in FIELDS if name.startswith("area_")}
    columns["geometry_basis"] = geometry_basis((), (), (), has_area)
    return columns


def blank_info_columns(total):
    columns = {name: "" for name in FIELDS if name.startswith("info_")}
    columns["info_count"] = total
    columns["declared_unknown_basis"] = (
        "NO_INFO_BLOCK_the_producer_declared_no_classification_at_all_which_the_pinned_schema_"
        "permits_and_which_is_never_read_here_as_an_absence_of_urgency_severity_or_certainty_in_"
        "the_world")
    return columns


def convert(data, output_budget=None):
    """Saved CAP 1.2 bytes -> (CSV text for the common reader, a report of what was read).

    One row per area block, because an area is the smallest unit that carries a distinct location.
    An info block with no area still produces exactly one row, and an alert with no info block
    still produces exactly one row: a message is never silently dropped for declaring less.

    `output_budget` bounds the EXPANDED output in bytes, defaulting to `MAX_OUTPUT`. It is a
    parameter so that the amplification can be exercised against a small budget instead of by
    building a real multi-gigabyte file.
    """
    budget = MAX_OUTPUT if output_budget is None else output_budget
    root = read_alert(data)
    # Document totals, accumulated by adding each scope EXACTLY ONCE as it is walked. Summing the
    # per-row columns instead would double-count: the alert's own omissions repeat on every row.
    document = {"omitted": {}, "unknown": {}}

    def absorb(scope):
        for kind in ("omitted", "unknown"):
            for key, count in scope[kind].items():
                document[kind][key] = document[kind].get(key, 0) + count
        return scope

    alert_counted = {"omitted": {}, "unknown": {}}
    alert = alert_columns(root, alert_counted)
    absorb(alert_counted)
    infos = children(root, "info")
    if len(infos) > MAX_INFO:
        raise ValueError("the alert declares more info blocks than the finite bound")

    def accounted(row, path, *scopes):
        """Attach this row's own omission and unknown-element accounting, and where to recover it."""
        omitted, unknown = {}, {}
        for scope in scopes:
            for key, count in scope["omitted"].items():
                omitted[key] = omitted.get(key, 0) + count
            for key, count in scope["unknown"].items():
                unknown[key] = unknown.get(key, 0) + count
        row["source_path"] = path
        row["omitted_known_count"] = sum(omitted.values())
        row["omitted_known_hex"] = hexed_list(sorted(omitted.items()))
        row["unknown_element_count"] = sum(unknown.values())
        row["unknown_element_hex"] = hexed_list(sorted(unknown.items()))
        row["recovery_basis"] = RECOVERY_BASIS
        return row

    rows = []
    areas_read = 0
    if not infos:
        row = dict(alert, cap_row_kind="ALERT_WITHOUT_INFO")
        row.update(blank_info_columns(0))
        row.update(blank_area_columns(False))
        rows.append(accounted(row, "/alert", alert_counted))
    for index, info in enumerate(infos):
        info_counted = {"omitted": {}, "unknown": {}}
        info_path = "/alert/info[%d]" % index
        shared = dict(alert, **info_columns(info, index, len(infos), info_counted))
        absorb(info_counted)
        areas = children(info, "area")
        if len(areas) > MAX_AREA:
            raise ValueError("an info block declares more area blocks than the finite bound")
        if not areas:
            row = dict(shared, cap_row_kind="INFO_WITHOUT_AREA")
            row.update(blank_area_columns(False))
            rows.append(accounted(row, info_path, alert_counted, info_counted))
            continue
        for area_index, area in enumerate(areas):
            area_counted = {"omitted": {}, "unknown": {}}
            row = dict(shared, cap_row_kind="AREA")
            row.update(area_columns(area, area_index, len(areas), area_counted))
            absorb(area_counted)
            rows.append(accounted(row, "%s/area[%d]" % (info_path, area_index),
                                  alert_counted, info_counted, area_counted))
            areas_read += 1
    if len(rows) > MAX_ROWS:
        raise ValueError("the alert would produce %d rows, past the %d row output bound"
                         % (len(rows), MAX_ROWS))

    digest = hashlib.sha256(data).hexdigest()
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(FIELDS)
    # The budget is in BYTES, so it is measured in bytes. `StringIO.tell` counts characters, and a
    # cell can hold non-ASCII text: `identifier` and `sender` are carried exactly as the producer
    # wrote them, and the pinned schema forbids only spaces, commas, `<` and `&` in them -- not
    # non-ASCII. Counting characters would therefore have permitted an output several times the
    # promised size. Each row is serialised into a small reusable buffer and encoded ALONE, so the
    # work per row is the size of that row and never the size of the output accumulated so far.
    written = len(buffer.getvalue().encode("utf-8"))
    row_buffer = io.StringIO()
    row_writer = csv.writer(row_buffer, lineterminator="\n")
    for index, row in enumerate(rows):
        row["source_sha256"] = digest
        values = [row[name] for name in FIELDS]
        for name, value in zip(FIELDS, values):
            if isinstance(value, str) and NEEDS_QUOTING.search(value):
                raise ValueError("%s would need CSV quoting, which the common reader refuses"
                                 % name)
        row_buffer.seek(0)
        row_buffer.truncate(0)
        row_writer.writerow(values)
        chunk = row_buffer.getvalue()
        # Checked after EVERY row, so the overshoot is one row rather than the whole expansion.
        # The row dicts themselves share their strings by reference, so this serialisation is
        # where an amplified alert actually costs memory, and this is where it is stopped.
        written += len(chunk.encode("utf-8"))
        if written > budget:
            raise ValueError(
                "the expanded output passed the %d byte budget at row %d of %d (%d UTF-8 bytes "
                "written); a single declared text reaches every row built from it, so a small "
                "source can expand far beyond its own size. Nothing is truncated to fit"
                % (budget, index + 1, len(rows), written))
        buffer.write(chunk)
    report = {
        "rows": len(rows),
        "info_blocks_read": len(infos),
        "areas_read": areas_read,
        "alert_identifier": alert["alert_identifier"],
        "alert_sender": alert["alert_sender"],
        "msg_type": alert["alert_msg_type"],
        "source_sha256": digest,
        # Counted, never carried. The values themselves stay in the caller's original file.
        "known_elements_not_carried": dict(sorted(document["omitted"].items())),
        "unknown_elements_not_carried": dict(sorted(document["unknown"].items())),
        "recovery": RECOVERY_BASIS,
        "published_alerts": NOT_A_SERVICE,
        "verified_incidents": "NONE_EVERY_CLASSIFICATION_ON_EVERY_ROW_REMAINS_THE_SENDERS_REPORT",
    }
    return buffer.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="a saved CAP 1.2 alert XML file")
    parser.add_argument("output", type=Path, help="directory for observations.csv and report.json")
    arguments = parser.parse_args(argv)
    size = arguments.source.stat().st_size
    if size > MAX_INPUT:
        raise ValueError("the saved CAP file is %d bytes, past the %d byte input bound"
                         % (size, MAX_INPUT))
    with arguments.source.open("rb") as stream:
        # One byte past the bound, so a file that grew between the stat and the read is caught
        # rather than silently truncated to something that would parse as a different message.
        data = stream.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise ValueError("the saved CAP file grew past the %d byte input bound while being read"
                         % MAX_INPUT)
    output, report = convert(data)
    # A fresh directory, never an existing one: this tool does not overwrite an artifact somebody
    # else wrote, and silently replacing one would destroy evidence rather than produce it.
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
