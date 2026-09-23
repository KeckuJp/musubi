"""A saved visual-language model response -> common observations. Passive reading of a file.

This converter NEVER calls an inference API, never sends an image or a prompt anywhere, never
downloads or runs a model, and never judges a model's output. It reads a response document a caller
already saved and carries what the PRODUCER declared, with the declaration kept attached.

What the source means is fixed by two official definitions, pinned here and not re-authored:

  OpenAI REST API, OpenAPI description version 2.3.0
    openapi.yaml  sha256 1d1c5db507ba5de13ce36b5a7b91952e18aa568400930b85085580afedb485e4
    components.schemas.Response / OutputItem / OutputMessage / OutputMessageContent /
    OutputTextContent / RefusalContent / LogProb / Metadata / ModelIdsResponses
  openai-python v2.6.1, src/openai/types/responses/response.py
    sha256 599728b609cdbb752b460ac294c5cd609022b159460b5d8c5fd845f85e981584

Five facts from those definitions are load-bearing here, and each is enforced rather than assumed:

  * an assertion's KIND is typed by the producer. `OutputMessageContent` is either
    `OutputTextContent` (`type: "output_text"`) or `RefusalContent` (`type: "refusal"`). A refusal
    is not empty text and is never carried as one.
  * `output_text` at the top level is a CONVENIENCE PROPERTY the SDK computes by walking
    `output[].content[]`, and it returns "" when no text block exists. It is therefore NOT producer
    data and NOT authoritative: this converter walks the structure itself and never reads it.
  * `logprobs` is a REQUIRED array on a text part, of `LogProb{token, logprob, bytes, top_logprobs}`.
    Those are per-token quantities from the sampler. They are NOT a calibrated confidence in the
    assertion, and no score for the assertion exists anywhere in the schema.
  * the producer's status vocabulary is finite and is the uncertainty basis: response `status` is
    one of completed/failed/in_progress/cancelled/queued/incomplete, `incomplete_details.reason` is
    one of max_output_tokens/max_messages/content_filter/steered, and a message carries its own
    in_progress/completed/incomplete.
  * **the response carries NO reference to the input image.** `ResponseProperties` has no input
    field. `metadata` is explicitly a set of key-value pairs the CALLER attaches. So an image
    association can only ever be a CALLER DECLARATION here, never producer-carried, and it is
    labelled as such on every row.

What this is NOT. Every row is a MODEL ASSERTION that a producer reported, never a verified fact
about an image. Nothing here is evidence that the asserted content is true, that the model saw what
it says it saw, or that two documents concern the same image -- equal filenames are not equal
images, and no such equality is asserted. A model identifier is the producer's label; a name that
embeds a date is still a label and **not a weights digest and not proof of immutability**. The
absence of a weights digest is recorded as a limit of the source, not turned into a certification
requirement. No claim is made about VLMs in general or about any model's accuracy.
"""
import argparse
import csv
import decimal
import hashlib
import io
import json
from pathlib import Path
import re
import sys

# The pinned schema's own enumerations. A value outside them is refused rather than passed through.
RESPONSE_STATUS = ("completed", "failed", "in_progress", "cancelled", "queued", "incomplete")
INCOMPLETE_REASONS = ("max_output_tokens", "max_messages", "content_filter", "steered")
MESSAGE_STATUS = ("in_progress", "completed", "incomplete")
CONTENT_KINDS = {"output_text": "OUTPUT_TEXT", "refusal": "REFUSAL"}

NEEDS_QUOTING = re.compile(r'[,"\r\n]')
# A PURELY LEXICAL test: does the label end in something date-shaped. `ModelIdsShared` is
# `anyOf: [{type: string}, {enum: [...]}]`, so the pinned document ADMITS ANY STRING and its enum is
# a list of examples, not a closed set. Nothing about aliasing, snapshots, versions or immutability
# follows from the shape of a label, in either direction, and this pattern is never read as if it did.
DATE_SHAPED_SUFFIX = re.compile(r".*-\d{4}-\d{2}-\d{2}$")
# `Metadata`, in the pinned document's own words: "Set of 16 key-value pairs", "Keys are strings
# with a maximum length of 64 CHARACTERS. Values are strings with a maximum length of 512
# CHARACTERS", and `additionalProperties: {type: string}`. Characters, not bytes; no charset limit.
METADATA_MAX_PAIRS = 16
METADATA_MAX_KEY_CHARS = 64
METADATA_MAX_VALUE_CHARS = 512

# Every property the pinned `Response` admits, derived from components.schemas.Response together
# with the ResponseProperties and ModelResponseProperties it composes (36 names). A key outside this
# set is UNKNOWN; a key inside it that this path gives no column to is an OMITTED KNOWN field. The
# two are counted separately, because they mean different things to a consumer.
RESPONSE_KNOWN = frozenset({
    "background", "completed_at", "conversation", "created_at", "error", "id",
    "incomplete_details", "instructions", "max_output_tokens", "max_tool_calls", "metadata",
    "model", "moderation", "object", "output", "output_text", "parallel_tool_calls",
    "previous_response_id", "prompt", "prompt_cache_diagnostics", "prompt_cache_key",
    "prompt_cache_options", "prompt_cache_retention", "reasoning", "safety_identifier",
    "service_tier", "status", "temperature", "text", "tool_choice", "tools", "top_logprobs",
    "top_p", "truncation", "usage", "user"})
RESPONSE_READ = frozenset({"object", "id", "created_at", "model", "status", "incomplete_details",
                           "previous_response_id", "metadata", "output", "error"})
# `OutputMessage` required id/type/role/content/status, with `phase` optional.
MESSAGE_KNOWN = frozenset({"id", "type", "role", "content", "status", "phase"})
MESSAGE_READ = frozenset({"id", "type", "role", "content", "status"})
CONTENT_KNOWN = {"output_text": frozenset({"type", "text", "annotations", "logprobs"}),
                 "refusal": frozenset({"type", "refusal"})}
# The pinned `ResponseError` requires code and message and permits `misalignment`; the inline
# `incomplete_details` object defines `reason`. Classifying these two as "read" at the top level
# said nothing about the keys INSIDE them, so an extra key there used to vanish entirely.
ERROR_KNOWN = frozenset({"code", "message", "misalignment"})
ERROR_READ = frozenset({"code", "message"})
INCOMPLETE_KNOWN = frozenset({"reason"})
MAX_INPUT = 8 * 1024 * 1024
MAX_UNKNOWN_PATHS = 64
# 2**40 seconds is about 1.1e12; its adjusted decimal exponent is 12.
MAX_TIME_ADJUSTED_EXPONENT = 12
# Enough for a whole-second timestamp plus a microsecond fraction and room to spare; a value with
# more digits than this is refused cheaply rather than driving big-integer arithmetic.
MAX_TIME_SIGNIFICANT_DIGITS = 64
MAX_JSON_DEPTH = 64
MAX_OUTPUT_ITEMS = 256
MAX_CONTENT_PARTS = 256
MAX_ROWS = 4096
MAX_OUTPUT = 32 * 1024 * 1024

ASSERTION_BASIS = (
    "REPORTED_BY_THE_PRODUCER_AS_A_MODEL_ASSERTION_AND_NEVER_VERIFIED_HERE_this_row_carries_what_a_"
    "saved_response_says_a_model_said_it_is_not_evidence_that_the_asserted_content_is_true_not_"
    "evidence_that_the_model_saw_what_it_describes_and_never_this_tools_own_interpretation_of_any_"
    "image")

LOGPROB_BASIS_PRESENT = (
    "PER_TOKEN_SAMPLER_LOGPROBS_WERE_PRESENT_AND_ONLY_COUNTED_HERE_the_pinned_schema_defines_each_"
    "as_a_token_with_its_logprob_which_is_a_quantity_about_TOKEN_SELECTION_and_is_NEVER_a_"
    "calibrated_confidence_in_the_assertion_the_schema_defines_no_score_for_an_assertion_at_all")
LOGPROB_BASIS_ABSENT = (
    "NO_PER_TOKEN_LOGPROBS_WERE_PRESENT_which_the_pinned_schema_permits_as_an_empty_required_array_"
    "and_absence_of_logprobs_is_NEVER_read_as_certainty_or_as_zero_confidence")

FIELDS = (
    "record_time_us",
    "response_id_hex",
    "response_status",
    "incomplete_reason",
    "response_time_basis",
    "model_reported_hex",
    "model_label_shape",
    "model_version_basis",
    "previous_response_id_hex",
    "output_item_index",
    "output_item_count",
    "output_item_kind",
    "message_status",
    "content_index",
    "content_count",
    "assertion_kind",
    "assertion_text_hex",
    "assertion_basis",
    "logprob_count",
    "logprob_basis",
    "annotation_count",
    "image_association_hex",
    "image_association_basis",
    "message_id_hex",
    "response_error_code",
    "response_error_message_hex",
    "unhandled_output_item_count",
    "unhandled_output_item_kinds_hex",
    "unknown_field_count",
    "unknown_field_paths_hex",
    "omitted_known_field_count",
    "omitted_known_field_paths_hex",
    "validation_scope_basis",
    "metadata_entry_count",
    "metadata_uncarried_count",
    "metadata_uncarried_keys_hex",
    "derived_output_text_basis",
    "source_document_sha256",
)

NOT_A_RUNTIME = "NONE_THIS_TOOL_RUNS_NO_MODEL_CALLS_NO_API_AND_SENDS_NOTHING_ANYWHERE"

VALIDATION_SCOPE_BASIS = (
    "EXACTLY_WHAT_WAS_CHECKED_AND_NO_MORE_checked_against_the_pinned_document_are_the_response_and_"
    "message_status_values_the_incomplete_reason_the_content_part_kind_the_metadata_shape_types_pair_"
    "count_and_CHARACTER_lengths_the_presence_of_the_required_error_code_and_message_and_that_"
    "created_at_converts_EXACTLY_to_microseconds_by_integer_arithmetic_that_NO_AMBIENT_DECIMAL_"
    "PRECISION_CAN_ALTER_where_seconds_is_the_SOURCES_UNIT_AND_NOT_A_"
    "GRANULARITY_CONSTRAINT_so_a_fractional_second_is_carried_exactly_and_only_nonzero_precision_"
    "finer_than_one_microsecond_is_refused_NOT_CHECKED_are_the_ELEMENTS_of_the_logprobs_and_"
    "annotations_arrays_which_are_only_confirmed_to_be_arrays_and_counted_so_this_row_does_NOT_claim_"
    "that_each_element_conforms_to_the_pinned_LogProb_or_Annotation_schema_and_the_usage_object_the_"
    "error_misalignment_object_tool_calls_and_reasoning_items_are_counted_where_present_but_their_"
    "INTERIORS_ARE_NOT_VALIDATED_AT_ALL_and_because_no_uncarried_value_appears_in_any_column_THE_"
    "ORIGINAL_DOCUMENT_IDENTIFIED_BY_source_document_sha256_MUST_BE_RETAINED_BY_THE_CALLER_to_"
    "recover_any_of_them")


def field_accounting(container, known, read, level, counted):
    """Classify every key present: read, a known field this path omits, or unknown.

    The source digest lets a consumer go back to the document, but it does not tell them WHAT was
    left unprocessed. These counts do. Paths are aggregated by level and field name rather than by
    item index, so the accounting stays bounded however many items a response carries.
    """
    for key in container:
        if key in read:
            continue
        bucket = "omitted" if key in known else "unknown"
        path = "%s.%s" % (level, key)
        counted[bucket][path] = counted[bucket].get(path, 0) + 1
        if len(counted[bucket]) > MAX_UNKNOWN_PATHS:
            raise ValueError("the response carries more distinct %s field paths than the finite "
                             "bound" % bucket)


def hexed(value):
    if value is None:
        return ""
    return "hex:" + value.encode("utf-8").hex()


def hexed_list(values):
    """Repeats carried reversibly; a producer's item kind could contain any character."""
    if not values:
        return ""
    return hexed(json.dumps(values, ensure_ascii=False, separators=(",", ":")))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("a duplicate key %r makes the saved response ambiguous" % key)
        result[key] = value
    return result


def invalid_constant(name):
    raise ValueError("the saved response contains the non-JSON constant %s" % name)


def text_field(container, name, what, required=True):
    value = container.get(name)
    if value is None:
        if required:
            raise ValueError("the pinned schema requires %s and the response has none" % what)
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("%s is not a non-empty string" % what)
    return value


def enum_field(container, name, allowed, what, required=True):
    value = container.get(name)
    if value is None:
        if required:
            raise ValueError("the pinned schema requires %s and the response has none" % what)
        return ""
    if value not in allowed:
        raise ValueError("%s is %r, which the pinned schema does not define" % (what, value))
    return value


def model_label_shape(model):
    """A LEXICAL observation about the label's spelling, and deliberately nothing more."""
    if DATE_SHAPED_SUFFIX.fullmatch(model):
        return "ENDS_WITH_A_LITERAL_DATE_SHAPED_SUFFIX"
    return "NO_LITERAL_DATE_SHAPED_SUFFIX"


# One fixed sentence, identical on every row, because the conclusion is the same in every case:
# there is none. Earlier this column asserted that an undated label named a moving alias and a
# date-shaped one named a dated snapshot. NEITHER IMPLICATION FOLLOWS: the pinned document admits
# any string as a model id, so a date-shaped label may be an arbitrary caller string and an undated
# label may be perfectly stable. Resolving it would need an explicit pinned mapping from label to
# snapshot, which these two definitions do not contain.
MODEL_VERSION_BASIS = (
    "UNRESOLVED_BY_THESE_DEFINITIONS_the_exact_label_the_producer_reported_is_carried_verbatim_and_"
    "the_neighbouring_shape_column_records_ONLY_whether_it_ends_in_a_literal_date_shaped_suffix_"
    "NOTHING_about_aliasing_snapshots_versioning_or_immutability_is_concluded_from_that_shape_in_"
    "EITHER_DIRECTION_because_the_pinned_document_types_a_model_id_as_ANY_STRING_with_its_"
    "enumeration_as_examples_rather_than_a_closed_set_so_a_date_shaped_label_is_not_thereby_a_"
    "snapshot_and_an_undated_label_is_not_thereby_a_moving_alias_resolving_this_would_require_an_"
    "EXPLICIT_PINNED_MAPPING_from_label_to_snapshot_which_these_definitions_do_not_provide_and_the_"
    "schema_carries_NO_weights_digest_anywhere_which_is_a_LIMIT_OF_THE_SOURCE_recorded_here_and_"
    "never_a_certification_this_tool_demands")


def response_time_us(value):
    """`created_at` -> common microseconds, by EXACT integer arithmetic on the decimal digits.

    The pinned document types it `number` with `format: unixtime` and describes it as a "Unix
    timestamp (in seconds)". **Seconds is the UNIT, not a granularity constraint** -- `number` admits
    a fractional value, so a fractional second is a real timestamp and is carried exactly.

    `Decimal.scaleb` is NOT used, and an earlier version of this function was wrong to claim it never
    rounds: it obeys the current decimal context, whose default precision is 28 significant digits.
    `Decimal("1758585600.00000000000000000000000000001")` carries 39, so `scaleb(6)` discarded the
    nonzero tail and the value was accepted as a whole second -- silently moving the instant the
    producer wrote. A very small exponent could underflow to zero the same way.

    So the conversion reads the coefficient and exponent directly and works in Python integers, which
    are exact and context-free: value = coefficient * 10**exponent, hence microseconds =
    coefficient * 10**(exponent + 6). When that exponent is negative the division is performed only
    if it leaves no remainder; a nonzero remainder is an unrepresentable tail and is REFUSED, never
    rounded.

    Every guard that could otherwise be expensive runs first and cheaply, on `adjusted` and on the
    digit count, so neither a huge positive exponent nor a huge negative one ever reaches big-integer
    arithmetic.
    """
    if isinstance(value, bool) or not isinstance(value, (int, decimal.Decimal)):
        raise ValueError("the pinned schema requires a numeric created_at")
    quantity = decimal.Decimal(value)
    if not quantity.is_finite():
        raise ValueError("created_at is not finite")
    if quantity.is_zero():
        return 0
    if quantity.is_signed():
        raise ValueError("created_at is negative, outside the range this path accepts")
    # `adjusted` is floor(log10(|value|)) and is O(1). 2**40 seconds is about 1.1e12, adjusted 12.
    if quantity.adjusted() > MAX_TIME_ADJUSTED_EXPONENT:
        raise ValueError("created_at has magnitude 1e%d, far outside the range this path accepts"
                         % quantity.adjusted())
    # A nonzero magnitude below 1e-6 cannot be a whole number of microseconds, whatever its digits.
    # Refusing here keeps `1e-999999` from ever reaching a 10**k that would materialise.
    if quantity.adjusted() <= -7:
        raise ValueError(
            "created_at is %s, a nonzero magnitude finer than the one microsecond the common output "
            "holds; it is refused rather than rounded" % quantity)
    if quantity >= 2 ** 40:
        raise ValueError("created_at is outside the range this path accepts")
    _sign, digits, exponent = quantity.as_tuple()
    if len(digits) > MAX_TIME_SIGNIFICANT_DIGITS:
        raise ValueError("created_at carries %d significant digits, past the %d this path reads"
                         % (len(digits), MAX_TIME_SIGNIFICANT_DIGITS))
    coefficient = int("".join(map(str, digits)))
    scaled_exponent = exponent + 6
    if scaled_exponent >= 0:
        microseconds = coefficient * 10 ** scaled_exponent
    else:
        divisor = 10 ** -scaled_exponent
        if coefficient % divisor:
            raise ValueError(
                "created_at is %s, which carries nonzero precision finer than the one microsecond "
                "the common output holds; it is refused rather than rounded" % quantity)
        microseconds = coefficient // divisor
    if not 0 <= microseconds < 2 ** 40 * 1000000:
        raise ValueError("created_at is outside the microsecond range this path accepts")
    return microseconds


def checked_metadata(document):
    """Validate `metadata` against the pinned document WHENEVER it is present.

    Its shape is checked even when no image key was named, because a malformed metadata member is a
    malformed document regardless of what this path was asked to read from it. A JSON array is NOT
    quietly treated as an absent object: `metadata` is an object or null, and anything else is
    refused. Lengths are counted in CHARACTERS and the charset is unrestricted, because the pinned
    document says "maximum length of 64 characters" / "512 characters" and imposes no charset -- an
    earlier version of this file invented an ASCII key rule and a byte bound, and rejected legal
    Unicode. `additionalProperties: {type: string}` makes every value a string, so an EMPTY STRING is
    a legal value and is kept distinguishable from an absent key and from a null metadata member.
    """
    if "metadata" not in document:
        return None
    metadata = document["metadata"]
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError(
            "metadata is %s; the pinned document types it as an object or null, and another shape "
            "is refused rather than read as an absent object" % type(metadata).__name__)
    if len(metadata) > METADATA_MAX_PAIRS:
        raise ValueError("metadata carries %d pairs; the pinned document describes a set of %d"
                         % (len(metadata), METADATA_MAX_PAIRS))
    for key, value in metadata.items():
        if len(key) > METADATA_MAX_KEY_CHARS:
            raise ValueError("a metadata key is %d characters, past the pinned %d"
                             % (len(key), METADATA_MAX_KEY_CHARS))
        if not isinstance(value, str):
            raise ValueError("a metadata value is %s; the pinned document types every value as a "
                             "string" % type(value).__name__)
        if len(value) > METADATA_MAX_VALUE_CHARS:
            raise ValueError("a metadata value is %d characters, past the pinned %d"
                             % (len(value), METADATA_MAX_VALUE_CHARS))
    return metadata


def caller_association(metadata, image_key):
    """The image association, which this schema can only ever supply as a CALLER declaration."""
    if image_key is None:
        return "", ("NO_CALLER_ASSOCIATION_WAS_DECLARED_and_the_pinned_schema_carries_NO_reference_"
                    "to_any_input_image_on_the_response_object_so_this_row_states_no_image_at_all")
    if not isinstance(image_key, str) or len(image_key) > METADATA_MAX_KEY_CHARS:
        raise ValueError("the caller's metadata key is a string of at most %d characters"
                         % METADATA_MAX_KEY_CHARS)
    if metadata is None:
        return "", ("A_CALLER_KEY_WAS_NAMED_BUT_THE_RESPONSE_CARRIES_NO_METADATA_OBJECT_so_no_"
                    "association_exists_and_none_is_invented")
    if image_key not in metadata:
        return "", ("A_CALLER_KEY_WAS_NAMED_AND_IS_ABSENT_FROM_THE_PRODUCERS_METADATA_so_no_"
                    "association_exists_for_this_response_and_none_is_invented")
    value = metadata[image_key]
    # An empty string is legal here and reaches the cell as `hex:` with no payload, which is a
    # different value from the blank an absent key produces. The basis column says which happened.
    return hexed(value), (
        "CALLER_DECLARED_UNDER_A_CALLER_NAMED_METADATA_KEY_the_pinned_schema_describes_metadata_as_"
        "key_value_pairs_THE_CALLER_ATTACHES_so_this_is_the_CALLERS_STATEMENT_about_which_input_the_"
        "response_concerns_and_is_NOT_carried_or_confirmed_by_the_producer_it_is_NEVER_evidence_"
        "that_the_model_received_that_image_and_an_equal_name_or_filename_is_NEVER_the_same_image_"
        "an_EMPTY_STRING_here_is_a_value_the_caller_actually_stored_and_is_never_the_same_as_an_"
        "absent_key_or_a_null_metadata_member")


def read_response(data):
    if not isinstance(data, bytes):
        raise ValueError("a saved response is read as bytes")
    if not data.strip():
        raise ValueError("the saved response file is empty")
    if len(data) > MAX_INPUT:
        raise ValueError("the saved response is %d bytes, past the %d byte input bound"
                         % (len(data), MAX_INPUT))
    try:
        # `parse_float=Decimal` keeps a written magnitude EXACT. With the default float,
        # `created_at` of 1758585600.0000001 rounds to an integer and the fractional part -- real
        # time the producer wrote -- disappears without a word. A deeply nested document raises
        # RecursionError inside the decoder, which is a bounded failure here and never a success.
        document = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_constant=invalid_constant, parse_float=decimal.Decimal)
    except UnicodeDecodeError as error:
        raise ValueError("the saved response is not UTF-8: %s" % error)
    except json.JSONDecodeError as error:
        raise ValueError("the saved response is not well-formed JSON: %s" % error)
    except RecursionError:
        raise ValueError("the saved response nests deeper than this path will decode")
    if not isinstance(document, dict):
        raise ValueError("a saved response is a JSON object")
    if document.get("object") != "response":
        raise ValueError(
            "the saved document declares object %r; this converter reads only the pinned "
            "\"response\" object and never guesses at another shape" % document.get("object"))
    return document


def convert(data, image_key=None, output_budget=None):
    """Saved response bytes -> (CSV text for the common reader, a report of what was read).

    One row per content part of each output message, because a content part is the smallest unit
    that carries one typed assertion. An output item this path gives no column to is COUNTED by its
    producer-declared kind and never silently dropped.
    """
    budget = MAX_OUTPUT if output_budget is None else output_budget
    document = read_response(data)
    digest = hashlib.sha256(data).hexdigest()

    counted = {"unknown": {}, "omitted": {}}
    field_accounting(document, RESPONSE_KNOWN, RESPONSE_READ, "response", counted)
    identifier = text_field(document, "id", "id")
    model = text_field(document, "model", "model")
    status = enum_field(document, "status", RESPONSE_STATUS, "status")
    incomplete = document.get("incomplete_details")
    if incomplete is None:
        reason = "NONE_DECLARED"
    elif not isinstance(incomplete, dict):
        raise ValueError("incomplete_details is an object or null")
    else:
        field_accounting(incomplete, INCOMPLETE_KNOWN, INCOMPLETE_KNOWN,
                         "response.incomplete_details", counted)
        reason = enum_field(incomplete, "reason", INCOMPLETE_REASONS,
                            "incomplete_details.reason", required=False) or "PRESENT_WITHOUT_REASON"
    # `error` is required to carry a code and a message when it is present, and it is what gives a
    # `failed` status its meaning, so it is carried rather than counted as an omission.
    error = document.get("error")
    if error is None:
        error_code, error_message = "NONE_DECLARED", None
    elif not isinstance(error, dict):
        raise ValueError("error is an object or null in the pinned document")
    else:
        field_accounting(error, ERROR_KNOWN, ERROR_READ, "response.error", counted)
        error_code = text_field(error, "code", "error.code")
        error_message = text_field(error, "message", "error.message")
        if NEEDS_QUOTING.search(error_code):
            raise ValueError("an error code carrying a separator is refused")
    metadata = checked_metadata(document)
    association_hex, association_basis = caller_association(metadata, image_key)
    # `metadata` was classified "read", yet only the one selected entry is ever carried. Every other
    # entry the caller attached was dropped with no trace. They are now counted by key, ONCE PER
    # DOCUMENT -- the selected entry is excluded only when it was actually carried, so an EMPTY
    # selected value (which is carried) is not counted here while an ABSENT key leaves every entry
    # uncarried. Their VALUES are not carried; the original document remains the only way back.
    carried_key = image_key if (metadata is not None and image_key is not None
                                and image_key in metadata) else None
    uncarried_keys = sorted(key for key in (metadata or {}) if key != carried_key)

    items = document.get("output")
    if not isinstance(items, list):
        raise ValueError("the pinned schema requires output to be an array")
    if len(items) > MAX_OUTPUT_ITEMS:
        raise ValueError("the response declares more output items than the finite bound")

    shared = {
        "record_time_us": response_time_us(document.get("created_at")),
        "response_id_hex": hexed(identifier),
        "response_status": status,
        "incomplete_reason": reason,
        "response_time_basis": (
            "THE_PRODUCERS_DECLARED_CREATION_TIME_OF_THE_RESPONSE_in_unix_seconds_widened_to_"
            "microseconds_it_is_when_the_producer_says_it_made_this_response_and_is_NEVER_the_time_"
            "any_image_was_captured_never_a_synchronised_clock_and_never_authenticated"),
        "model_reported_hex": hexed(model),
        "model_label_shape": model_label_shape(model),
        "model_version_basis": MODEL_VERSION_BASIS,
        "response_error_code": error_code,
        "response_error_message_hex": hexed(error_message),
        "validation_scope_basis": VALIDATION_SCOPE_BASIS,
        "metadata_entry_count": "" if metadata is None else len(metadata),
        "metadata_uncarried_count": len(uncarried_keys),
        "metadata_uncarried_keys_hex": hexed_list(uncarried_keys),
        "previous_response_id_hex": hexed(
            text_field(document, "previous_response_id", "previous_response_id", required=False)),
        "output_item_count": len(items),
        "assertion_basis": ASSERTION_BASIS,
        "image_association_hex": association_hex,
        "image_association_basis": association_basis,
        "derived_output_text_basis": (
            "THE_TOP_LEVEL_output_text_FIELD_IS_NEVER_READ_the_pinned_writer_defines_it_as_a_"
            "CONVENIENCE_PROPERTY_THE_SDK_COMPUTES_by_walking_the_output_list_and_it_returns_an_"
            "EMPTY_STRING_when_no_text_block_exists_so_it_cannot_tell_absent_from_empty_this_path_"
            "walks_the_producers_own_structure_instead"),
        "source_document_sha256": digest,
    }

    rows, unhandled = [], {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("an output item is an object")
        kind = item.get("type")
        if not isinstance(kind, str) or not kind:
            raise ValueError("the pinned schema requires a type on every output item")
        if kind != "message":
            # A tool call, a reasoning item or any other variant the pinned schema defines. This
            # path gives it no column; it is counted by kind so a reader can see it existed.
            unhandled[kind] = unhandled.get(kind, 0) + 1
            continue
        if item.get("role") != "assistant":
            raise ValueError("the pinned schema fixes an output message role to assistant")
        field_accounting(item, MESSAGE_KNOWN, MESSAGE_READ, "output.message", counted)
        message_identifier = text_field(item, "id", "output message id")
        message_status = enum_field(item, "status", MESSAGE_STATUS, "output message status")
        parts = item.get("content")
        if not isinstance(parts, list):
            raise ValueError("an output message carries a content array")
        if len(parts) > MAX_CONTENT_PARTS:
            raise ValueError("an output message declares more content parts than the finite bound")
        for part_index, part in enumerate(parts):
            if not isinstance(part, dict):
                raise ValueError("a content part is an object")
            part_kind = part.get("type")
            if part_kind in CONTENT_KNOWN:
                field_accounting(part, CONTENT_KNOWN[part_kind], CONTENT_KNOWN[part_kind],
                                 "output.message.content." + part_kind, counted)
            if part_kind not in CONTENT_KINDS:
                raise ValueError(
                    "a content part declares type %r; the pinned schema defines a content part as "
                    "output_text or refusal only, and an unknown kind is refused rather than "
                    "carried as text" % part_kind)
            if part_kind == "refusal":
                body = text_field(part, "refusal", "refusal text")
                logprobs, annotations = [], []
            else:
                body = part.get("text")
                if not isinstance(body, str):
                    raise ValueError("the pinned schema requires text on an output_text part")
                logprobs = part.get("logprobs")
                annotations = part.get("annotations")
                if not isinstance(logprobs, list) or not isinstance(annotations, list):
                    raise ValueError("the pinned schema requires logprobs and annotations arrays "
                                     "on an output_text part")
            rows.append(dict(
                shared, output_item_index=index, output_item_kind="MESSAGE",
                message_id_hex=hexed(message_identifier), message_status=message_status, content_index=part_index, content_count=len(parts),
                assertion_kind=CONTENT_KINDS[part_kind], assertion_text_hex=hexed(body),
                logprob_count=len(logprobs),
                logprob_basis=LOGPROB_BASIS_PRESENT if logprobs else LOGPROB_BASIS_ABSENT,
                annotation_count=len(annotations)))

    if not rows:
        raise ValueError(
            "the saved response carries no assistant message content part, so it states no "
            "assertion; %d output item(s) of other kinds were present and are named in the report"
            % sum(unhandled.values()))
    if len(rows) > MAX_ROWS:
        raise ValueError("the response would produce %d rows, past the %d row output bound"
                         % (len(rows), MAX_ROWS))

    unhandled_pairs = sorted(unhandled.items())
    unknown_pairs = sorted(counted["unknown"].items())
    omitted_pairs = sorted(counted["omitted"].items())
    for row in rows:
        row["unhandled_output_item_count"] = sum(unhandled.values())
        row["unhandled_output_item_kinds_hex"] = hexed_list(unhandled_pairs)
        row["unknown_field_count"] = sum(counted["unknown"].values())
        row["unknown_field_paths_hex"] = hexed_list(unknown_pairs)
        row["omitted_known_field_count"] = sum(counted["omitted"].values())
        row["omitted_known_field_paths_hex"] = hexed_list(omitted_pairs)

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
            raise ValueError(
                "the expanded output passed the %d byte budget at row %d of %d (%d UTF-8 bytes "
                "written); nothing is truncated to fit" % (budget, index + 1, len(rows), written))
        buffer.write(chunk)

    report = {
        "rows": len(rows),
        "output_items": len(items),
        "assertions": len(rows),
        "refusals": sum(1 for row in rows if row["assertion_kind"] == "REFUSAL"),
        "unhandled_output_item_kinds": dict(unhandled_pairs),
        "metadata_entries": None if metadata is None else len(metadata),
        "metadata_uncarried_keys": uncarried_keys,
        "unknown_fields": dict(unknown_pairs),
        "omitted_known_fields": dict(omitted_pairs),
        "element_validation": "LOGPROB_AND_ANNOTATION_ELEMENTS_ARE_COUNTED_NOT_SCHEMA_VALIDATED",
        "response_status": status,
        "incomplete_reason": reason,
        "model_reported": model,
        "image_association": "CALLER_DECLARED" if association_hex else "NONE_DECLARED",
        "source_document_sha256": digest,
        "inference_calls": NOT_A_RUNTIME,
        "verified_image_facts": "NONE_EVERY_ROW_REMAINS_AN_UNVERIFIED_MODEL_ASSERTION",
    }
    return buffer.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="a saved response JSON document")
    parser.add_argument("output", type=Path, help="directory for observations.csv and report.json")
    parser.add_argument("--image-key", default=None,
                        help="a metadata key the CALLER declares names the input image; carried as "
                             "a caller declaration only, never as producer-carried provenance")
    arguments = parser.parse_args(argv)
    size = arguments.source.stat().st_size
    if size > MAX_INPUT:
        raise ValueError("the saved response is %d bytes, past the %d byte input bound"
                         % (size, MAX_INPUT))
    with arguments.source.open("rb") as stream:
        data = stream.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise ValueError("the saved response grew past the %d byte input bound while being read"
                         % MAX_INPUT)
    output, report = convert(data, arguments.image_key)
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
