"""Authored saved-response documents only. No inference call, no API, no image, no model.

Every fixture here is written by this file. Nothing is sent anywhere and no model is downloaded or
run. The chain is the advertised one: an authored response document through this converter and
through the **real** common reader. An authored document is code and specification confirmation; it
is never evidence of any model's accuracy and never a real record.
"""
import csv
import decimal
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_vlm_response import FIELDS, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/vlm-response/profile.toml"


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


def text_part(text="A grey culvert with debris at the inlet.", logprobs=0, annotations=0):
    return {"type": "output_text", "text": text,
            "annotations": [{"type": "url_citation"} for _ in range(annotations)],
            "logprobs": [{"token": "A", "logprob": -0.1, "bytes": [65], "top_logprobs": []}
                         for _ in range(logprobs)]}


def message(parts=None, status="completed"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "status": status,
            "content": [text_part()] if parts is None else parts}


def response(output=None, model="gpt-4o-2024-08-06", status="completed", metadata=None, **extra):
    document = {"id": "resp_abc123", "object": "response", "created_at": 1758585600,
                "model": model, "status": status,
                "output": [message()] if output is None else output}
    if metadata is not None:
        document["metadata"] = metadata
    document.update(extra)
    return json.dumps(document).encode("utf-8")


def rows_of(data, **kwargs):
    output, report = convert(data, **kwargs)
    return list(csv.DictReader(io.StringIO(output))), report, output


class VlmResponse(unittest.TestCase):
    def test_a_saved_assertion_reaches_common_output_with_its_provenance(self):
        rows, report, _ = rows_of(response(metadata={"frame": "frame-000012"}),
                                 image_key="frame")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["assertion_kind"], "OUTPUT_TEXT")
        self.assertEqual(unhex(row["assertion_text_hex"]),
                         "A grey culvert with debris at the inlet.")
        self.assertEqual(unhex(row["model_reported_hex"]), "gpt-4o-2024-08-06")
        self.assertEqual(unhex(row["response_id_hex"]), "resp_abc123")
        self.assertEqual(row["response_status"], "completed")
        self.assertEqual(row["incomplete_reason"], "NONE_DECLARED")
        self.assertEqual(row["message_status"], "completed")
        self.assertEqual(int(row["record_time_us"]), 1758585600 * 1000000)
        self.assertEqual(len(row["source_document_sha256"]), 64)
        self.assertEqual(row["model_label_shape"], "ENDS_WITH_A_LITERAL_DATE_SHAPED_SUFFIX")
        self.assertIn("UNRESOLVED_BY_THESE_DEFINITIONS", row["model_version_basis"])
        self.assertIn("NO_weights_digest_anywhere", row["model_version_basis"])
        self.assertIn("NEVER_VERIFIED_HERE", row["assertion_basis"])
        self.assertEqual(report["verified_image_facts"],
                         "NONE_EVERY_ROW_REMAINS_AN_UNVERIFIED_MODEL_ASSERTION")
        self.assertIn("RUNS_NO_MODEL", report["inference_calls"])

    def test_a_refusal_is_its_own_kind_and_is_never_carried_as_text(self):
        rows, report, _ = rows_of(response(output=[message(
            parts=[{"type": "refusal", "refusal": "I can't help with that."}])]))
        self.assertEqual(rows[0]["assertion_kind"], "REFUSAL")
        self.assertEqual(unhex(rows[0]["assertion_text_hex"]), "I can't help with that.")
        self.assertEqual(report["refusals"], 1)
        self.assertEqual(rows[0]["logprob_count"], "0")
        self.assertIn("NEVER_read_as_certainty", rows[0]["logprob_basis"])

    def test_the_image_association_is_a_caller_declaration_and_says_so(self):
        declared, _, _ = rows_of(response(metadata={"frame": "frame-000012"}), image_key="frame")
        self.assertEqual(unhex(declared[0]["image_association_hex"]), "frame-000012")
        self.assertIn("CALLER_DECLARED", declared[0]["image_association_basis"])
        self.assertIn("NOT_carried_or_confirmed_by_the_producer",
                      declared[0]["image_association_basis"])
        self.assertIn("an_equal_name_or_filename_is_NEVER_the_same_image",
                      declared[0]["image_association_basis"])

        none, _, _ = rows_of(response(metadata={"frame": "frame-000012"}))
        self.assertEqual(none[0]["image_association_hex"], "")
        self.assertIn("NO_CALLER_ASSOCIATION_WAS_DECLARED", none[0]["image_association_basis"])

        absent, _, _ = rows_of(response(metadata={"other": "x"}), image_key="frame")
        self.assertEqual(absent[0]["image_association_hex"], "")
        self.assertIn("ABSENT_FROM_THE_PRODUCERS_METADATA", absent[0]["image_association_basis"])

        bare, _, _ = rows_of(response(), image_key="frame")
        self.assertIn("CARRIES_NO_METADATA_OBJECT", bare[0]["image_association_basis"])

    def test_a_model_label_never_gains_version_semantics_from_its_shape(self):
        """Regression. Neither implication follows, and the label is carried verbatim either way.

        `ModelIdsShared` is `anyOf: [{type: string}, {enum: [...]}]`, so the pinned document admits
        ANY string and its enumeration is examples. An undated stable-looking label is therefore not
        a moving alias, and an arbitrary date-shaped string is not a dated snapshot.
        """
        for label, shape in (("gpt-4o", "NO_LITERAL_DATE_SHAPED_SUFFIX"),
                             ("a-stable-internal-name", "NO_LITERAL_DATE_SHAPED_SUFFIX"),
                             ("gpt-4o-2024-08-06", "ENDS_WITH_A_LITERAL_DATE_SHAPED_SUFFIX"),
                             ("not-a-model-9999-99-99", "ENDS_WITH_A_LITERAL_DATE_SHAPED_SUFFIX")):
            with self.subTest(label=label):
                rows, _, _ = rows_of(response(model=label))
                self.assertEqual(unhex(rows[0]["model_reported_hex"]), label)
                self.assertEqual(rows[0]["model_label_shape"], shape)
                self.assertIn("UNRESOLVED_BY_THESE_DEFINITIONS", rows[0]["model_version_basis"])
                for invented in ("moving_alias_and", "named_a_dated_snapshot_this"):
                    self.assertNotIn(invented, rows[0]["model_version_basis"])
        bases = {rows_of(response(model=m))[0][0]["model_version_basis"]
                 for m in ("gpt-4o", "gpt-4o-2024-08-06", "x-1999-01-01")}
        self.assertEqual(len(bases), 1)

    def test_created_at_converts_exactly_including_fractional_seconds(self):
        """Seconds is the source's UNIT, not a granularity constraint.

        An earlier version refused a fractional second on the ground that the source "declares whole
        seconds". It declares `number` with `format: unixtime`; `number` admits a fraction, so a
        fractional second is a real timestamp and must be carried exactly.
        """
        for literal, expected_us in ((b"1758585600", 1758585600000000),
                                     (b"1.7585856e9", 1758585600000000),
                                     (b"1758585600.0", 1758585600000000),
                                     (b"1758585600.5", 1758585600500000),
                                     (b"1758585600.25", 1758585600250000),
                                     (b"1758585600.000001", 1758585600000001),
                                     (b"1.7585856005e9", 1758585600500000),
                                     (b"0", 0)):
            with self.subTest(literal=literal):
                rows, _, _ = rows_of(response().replace(
                    b'"created_at": 1758585600', b'"created_at": ' + literal))
                self.assertEqual(int(rows[0]["record_time_us"]), expected_us)

        self.assertEqual(float("1758585600.0000001"), float(1758585600))
        for literal in (b"1758585600.0000001", b"1758585600.000000000000001", b"1e-7"):
            with self.subTest(literal=literal), self.assertRaises(ValueError) as caught:
                convert(response().replace(b'"created_at": 1758585600',
                                           b'"created_at": ' + literal))
            self.assertIn("finer than the one microsecond", str(caught.exception))

        for literal in (b"1e999999", b"-1e9", b"1e40", b"-0.5"):
            with self.subTest(literal=literal), self.assertRaises(ValueError):
                convert(response().replace(b'"created_at": 1758585600',
                                           b'"created_at": ' + literal))

        rows, _, _ = rows_of(response())
        self.assertIn("NEVER_the_time_any_image_was_captured", rows[0]["response_time_basis"])

    def test_a_high_significance_subsecond_tail_is_not_rounded_away(self):
        """Regression for a real defect: `Decimal.scaleb` obeys the context and rounds.

        The default context precision is 28 significant digits. This value carries 39, so the
        previous `scaleb(6)` implementation discarded the nonzero tail and accepted the value as a
        whole second -- silently moving the instant the producer wrote.
        """
        literal = b"1758585600.00000000000000000000000000001"
        self.assertEqual(len(decimal.Decimal(literal.decode()).as_tuple().digits), 39)
        self.assertGreater(len(decimal.Decimal(literal.decode()).as_tuple().digits),
                           decimal.getcontext().prec)
        self.assertEqual(decimal.Decimal(literal.decode()).scaleb(6),
                         decimal.Decimal("1758585600000000.000000000000"))
        with self.assertRaises(ValueError) as caught:
            convert(response().replace(b'"created_at": 1758585600',
                                       b'"created_at": ' + literal))
        self.assertIn("finer than the one microsecond", str(caught.exception))

    def test_the_conversion_does_not_depend_on_the_decimal_context(self):
        """No context-sensitive operation is left, so a hostile context cannot change a value."""
        from scripts.convert_vlm_response import response_time_us
        exact = decimal.Decimal("1758585600.000001")
        tail = decimal.Decimal("1758585600.00000000000000000000000000001")
        for precision in (3, 9, 28, 200):
            with self.subTest(precision=precision), decimal.localcontext() as context:
                context.prec = precision
                self.assertEqual(response_time_us(exact), 1758585600000001)
                with self.assertRaises(ValueError):
                    response_time_us(tail)

    def test_trailing_zeros_and_tiny_nonzero_magnitudes(self):
        from scripts.convert_vlm_response import response_time_us
        for literal in ("1758585600", "1758585600.0", "1758585600.000000", "1.7585856e9",
                        "1.75858560000e9", "17585856e2"):
            with self.subTest(literal=literal):
                self.assertEqual(response_time_us(decimal.Decimal(literal)), 1758585600000000)
        self.assertEqual(response_time_us(decimal.Decimal("0.000001")), 1)
        self.assertEqual(response_time_us(decimal.Decimal("1E-6")), 1)
        for literal in ("0.0000001", "1E-7", "1E-999999", "1E-100000000"):
            with self.subTest(literal=literal), self.assertRaises(ValueError):
                response_time_us(decimal.Decimal(literal))
        with self.assertRaises(ValueError):
            response_time_us(decimal.Decimal("1." + "1" * 5000))

    def test_deeply_nested_json_is_a_bounded_failure(self):
        deep = b'{"object":"response","a":' + b"[" * 20000 + b"]" * 20000 + b"}"
        with self.assertRaises(ValueError):
            convert(deep)

    def test_metadata_follows_the_pinned_document_not_an_invented_limit(self):
        key, value = "画像" * 10, "雨" * 512
        self.assertEqual(len(key), 20)
        self.assertEqual(len(value), 512)
        self.assertGreater(len(value.encode("utf-8")), 512)
        rows, _, _ = rows_of(response(metadata={key: value}), image_key=key)
        self.assertEqual(unhex(rows[0]["image_association_hex"]), value)
        self.assertIn("CALLER_DECLARED", rows[0]["image_association_basis"])

        empty, _, _ = rows_of(response(metadata={"frame": ""}), image_key="frame")
        self.assertEqual(empty[0]["image_association_hex"], "hex:")
        self.assertEqual(unhex(empty[0]["image_association_hex"]), "")
        self.assertIn("an_EMPTY_STRING_here_is_a_value_the_caller_actually_stored",
                      empty[0]["image_association_basis"])
        absent, _, _ = rows_of(response(metadata={"other": "x"}), image_key="frame")
        self.assertEqual(absent[0]["image_association_hex"], "")
        self.assertNotEqual(empty[0]["image_association_hex"],
                            absent[0]["image_association_hex"])
        null, _, _ = rows_of(response(metadata=None) if False else
                             response().replace(b'"status"', b'"metadata": null, "status"'),
                             image_key="frame")
        self.assertIn("CARRIES_NO_METADATA_OBJECT", null[0]["image_association_basis"])

    def test_malformed_metadata_is_refused_even_with_no_image_key_chosen(self):
        for bad, why in (
                ([["frame", "f"]], "a JSON array is not an absent object"),
                ("frame=f", "a string is not a metadata object"),
                ({"frame": 12}, "the pinned document types every value as a string"),
                ({"frame": None}, "a null value is not a string"),
                ({"k" * 65: "f"}, "a key past the pinned 64 characters"),
                ({"frame": "x" * 513}, "a value past the pinned 512 characters"),
                ({"k%d" % i: "v" for i in range(17)}, "more than the pinned 16 pairs")):
            with self.subTest(why=why):
                with self.assertRaises(ValueError):
                    convert(response(metadata=bad))
                with self.assertRaises(ValueError):
                    convert(response(metadata=bad), image_key="frame")

    def test_unknown_and_omitted_known_fields_are_counted_separately(self):
        rows, report, output = rows_of(response(
            output=[{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                     "phase": "final", "vendor_extra": "UNCARRIED-MESSAGE-VALUE",
                     "content": [dict(text_part(), vendor_part="UNCARRIED-PART-VALUE")]}],
            usage={"input_tokens": 5}, service_tier="auto",
            vendor_top="UNCARRIED-TOP-VALUE"))
        unknown = dict(json.loads(unhex(rows[0]["unknown_field_paths_hex"])))
        omitted = dict(json.loads(unhex(rows[0]["omitted_known_field_paths_hex"])))
        self.assertEqual(unknown, {"response.vendor_top": 1, "output.message.vendor_extra": 1,
                                   "output.message.content.output_text.vendor_part": 1})
        self.assertEqual(omitted, {"response.usage": 1, "response.service_tier": 1,
                                   "output.message.phase": 1})
        self.assertEqual(rows[0]["unknown_field_count"], "3")
        self.assertEqual(rows[0]["omitted_known_field_count"], "3")
        self.assertEqual(report["unknown_fields"], unknown)
        self.assertEqual(report["omitted_known_fields"], omitted)
        for secret in ("UNCARRIED-TOP-VALUE", "UNCARRIED-MESSAGE-VALUE", "UNCARRIED-PART-VALUE"):
            self.assertNotIn(secret.encode().hex(), output)

    def test_nested_error_and_incomplete_keys_are_accounted(self):
        """They were classified "read" at the top level, which said nothing about their interiors."""
        rows, report, output = rows_of(response(
            status="failed",
            error={"code": "server_error", "message": "upstream timed out",
                   "misalignment": {"detail": "UNCARRIED-MISALIGNMENT"},
                   "vendor_error_extra": "UNCARRIED-ERROR-VALUE"},
            incomplete_details={"reason": "content_filter",
                                "vendor_incomplete_extra": "UNCARRIED-INCOMPLETE-VALUE"}))
        unknown = dict(json.loads(unhex(rows[0]["unknown_field_paths_hex"])))
        omitted = dict(json.loads(unhex(rows[0]["omitted_known_field_paths_hex"])))
        self.assertEqual(unknown, {"response.error.vendor_error_extra": 1,
                                   "response.incomplete_details.vendor_incomplete_extra": 1})
        self.assertEqual(omitted, {"response.error.misalignment": 1})
        self.assertEqual(report["unknown_fields"], unknown)
        for secret in ("UNCARRIED-MISALIGNMENT", "UNCARRIED-ERROR-VALUE",
                       "UNCARRIED-INCOMPLETE-VALUE"):
            self.assertNotIn(secret.encode().hex(), output)
        self.assertIn("INTERIORS_ARE_NOT_VALIDATED_AT_ALL", rows[0]["validation_scope_basis"])
        self.assertIn("MUST_BE_RETAINED_BY_THE_CALLER", rows[0]["validation_scope_basis"])

    def test_metadata_entries_not_carried_are_counted_once_per_document(self):
        two_parts = [message(parts=[text_part("first"),
                                    {"type": "refusal", "refusal": "second"}])]
        rows, report, output = rows_of(response(
            output=two_parts,
            metadata={"frame": "frame-000012", "operator": "UNCARRIED-OPERATOR",
                      "run": "UNCARRIED-RUN"}), image_key="frame")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["metadata_entry_count"], "3")
            self.assertEqual(row["metadata_uncarried_count"], "2")
            self.assertEqual(json.loads(unhex(row["metadata_uncarried_keys_hex"])),
                             ["operator", "run"])
        self.assertEqual(report["metadata_entries"], 3)
        self.assertEqual(report["metadata_uncarried_keys"], ["operator", "run"])
        for secret in ("UNCARRIED-OPERATOR", "UNCARRIED-RUN"):
            self.assertNotIn(secret.encode().hex(), output)

        empty, _, _ = rows_of(response(metadata={"frame": "", "other": "x"}), image_key="frame")
        self.assertEqual(empty[0]["image_association_hex"], "hex:")
        self.assertEqual(empty[0]["metadata_uncarried_count"], "1")
        absent, _, _ = rows_of(response(metadata={"other": "x"}), image_key="frame")
        self.assertEqual(absent[0]["image_association_hex"], "")
        self.assertEqual(absent[0]["metadata_uncarried_count"], "1")
        self.assertEqual(json.loads(unhex(absent[0]["metadata_uncarried_keys_hex"])), ["other"])
        nokey, _, _ = rows_of(response(metadata={"a": "1", "b": "2"}))
        self.assertEqual(nokey[0]["metadata_uncarried_count"], "2")
        bare, _, _ = rows_of(response())
        self.assertEqual(bare[0]["metadata_entry_count"], "")
        self.assertEqual(bare[0]["metadata_uncarried_count"], "0")

    def test_the_message_id_and_error_details_are_carried(self):
        plain, _, _ = rows_of(response())
        self.assertEqual(unhex(plain[0]["message_id_hex"]), "msg_1")
        self.assertEqual(plain[0]["response_error_code"], "NONE_DECLARED")
        self.assertEqual(plain[0]["response_error_message_hex"], "")
        failed, report, _ = rows_of(response(
            status="failed", error={"code": "server_error", "message": "upstream timed out"}))
        self.assertEqual(failed[0]["response_status"], "failed")
        self.assertEqual(failed[0]["response_error_code"], "server_error")
        self.assertEqual(unhex(failed[0]["response_error_message_hex"]), "upstream timed out")
        for bad in ({"message": "no code"}, {"code": "server_error"}, "not an object"):
            with self.assertRaises(ValueError):
                convert(response(status="failed", error=bad))

    def test_the_validation_scope_is_stated_rather_than_overclaimed(self):
        rows, _, _ = rows_of(response(output=[message(parts=[
            {"type": "output_text", "text": "x", "annotations": [],
             "logprobs": [{"not_a_logprob": True}, 17]}])]))
        self.assertEqual(rows[0]["logprob_count"], "2")
        self.assertIn("ELEMENTS_of_the_logprobs_and_annotations_arrays",
                      rows[0]["validation_scope_basis"])
        self.assertIn("does_NOT_claim", rows[0]["validation_scope_basis"])
        with self.assertRaises(ValueError):
            convert(response(output=[message(parts=[
                {"type": "output_text", "text": "x", "annotations": [], "logprobs": {}}])]))

    def test_producer_carried_linkage_is_distinguished_from_the_caller_declaration(self):
        rows, _, _ = rows_of(response(previous_response_id="resp_earlier",
                                      metadata={"frame": "f1"}), image_key="frame")
        self.assertEqual(unhex(rows[0]["previous_response_id_hex"]), "resp_earlier")
        self.assertIn("IS_producer_carried_linkage",
                      PROFILE.read_text(encoding="utf-8"))
        self.assertIn("CALLER_DECLARED", rows[0]["image_association_basis"])

    def test_a_truncated_or_filtered_assertion_stays_visible(self):
        for reason in ("max_output_tokens", "content_filter", "steered", "max_messages"):
            rows, report, _ = rows_of(response(
                status="incomplete", output=[message(status="incomplete")],
                incomplete_details={"reason": reason}))
            self.assertEqual(rows[0]["response_status"], "incomplete")
            self.assertEqual(rows[0]["incomplete_reason"], reason)
            self.assertEqual(rows[0]["message_status"], "incomplete")
            self.assertEqual(report["incomplete_reason"], reason)
        present = rows_of(response(incomplete_details={}))[0]
        self.assertEqual(present[0]["incomplete_reason"], "PRESENT_WITHOUT_REASON")

    def test_output_items_with_no_column_are_counted_not_dropped(self):
        rows, report, output = rows_of(response(output=[
            {"id": "rs_1", "type": "reasoning", "summary": []},
            message(),
            {"id": "fs_1", "type": "file_search_call",
             "queries": ["UNCARRIED-QUERY-TOKEN"]},
            {"id": "rs_2", "type": "reasoning", "summary": []}]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["output_item_count"], "4")
        self.assertEqual(rows[0]["output_item_index"], "1")
        self.assertEqual(rows[0]["unhandled_output_item_count"], "3")
        self.assertEqual(json.loads(unhex(rows[0]["unhandled_output_item_kinds_hex"])),
                         [["file_search_call", 1], ["reasoning", 2]])
        self.assertEqual(report["unhandled_output_item_kinds"],
                         {"file_search_call": 1, "reasoning": 2})
        self.assertNotIn("UNCARRIED-QUERY-TOKEN".encode().hex(), output)

    def test_the_sdk_derived_output_text_field_is_never_read(self):
        """It is a computed property that returns "" when no text block exists (pinned writer)."""
        rows, _, _ = rows_of(response(output=[message()], output_text="NOT THE PRODUCERS FIELD"))
        self.assertEqual(unhex(rows[0]["assertion_text_hex"]),
                         "A grey culvert with debris at the inlet.")
        self.assertNotIn("NOT THE PRODUCERS FIELD".encode().hex(),
                         rows[0]["assertion_text_hex"])
        self.assertIn("NEVER_READ", rows[0]["derived_output_text_basis"])

    def test_several_content_parts_each_keep_their_own_assertion(self):
        rows, _, _ = rows_of(response(output=[message(parts=[
            text_part("first", logprobs=2, annotations=1),
            {"type": "refusal", "refusal": "second"}])]))
        self.assertEqual([r["content_index"] for r in rows], ["0", "1"])
        self.assertEqual([r["content_count"] for r in rows], ["2", "2"])
        self.assertEqual([r["assertion_kind"] for r in rows], ["OUTPUT_TEXT", "REFUSAL"])
        self.assertEqual(rows[0]["logprob_count"], "2")
        self.assertEqual(rows[0]["annotation_count"], "1")
        self.assertIn("NEVER_a_calibrated_confidence", rows[0]["logprob_basis"])

    def test_the_important_failures_are_refused_rather_than_repaired(self):
        for data, why in (
                (b'{"object":"chat.completion","id":"x"}', "a different object kind"),
                (response().replace(b'"object": "response"', b'"object": "response_chunk"'),
                 "an object kind outside the pinned one"),
                (response(status="done"), "a status outside the pinned enumeration"),
                (response(incomplete_details={"reason": "too_long"}),
                 "an incomplete reason outside the pinned enumeration"),
                (response(output=[message(status="finished")]),
                 "a message status outside the pinned enumeration"),
                (response(output=[message(parts=[{"type": "output_audio", "data": "x"}])]),
                 "a content part kind the pinned schema does not define"),
                (response(output=[message(parts=[
                    {"type": "output_text", "text": "x", "annotations": []}])]),
                 "an output_text part without the required logprobs array"),
                (response(output=[message(parts=[{"type": "refusal"}])]),
                 "a refusal part without its refusal string"),
                (response(output=[{"id": "m", "type": "message", "role": "user",
                                   "status": "completed", "content": []}]),
                 "an output message whose role is not assistant"),
                (response(output=[]), "a response that states no assertion at all"),
                (response(output=[{"id": "rs", "type": "reasoning", "summary": []}]),
                 "a response whose only items carry no assertion"),
                (response().replace(b'"model": "gpt-4o-2024-08-06"', b'"model": ""'),
                 "an empty model label"),
                (response().replace(b'"created_at": 1758585600', b'"created_at": "yesterday"'),
                 "a non-numeric created_at"),
                (response().replace(b'"created_at": 1758585600', b'"created_at": -1'),
                 "a negative created_at"),
                (response().replace(b'"id": "resp_abc123"', b'"id": "a", "id": "b"'),
                 "a duplicate key that makes the document ambiguous"),
                (b'{"object":"response","id":"x","created_at":1,"model":"m","status":"completed"}',
                 "a response with no output array"),
                (b"[]", "a JSON array rather than an object"),
                (b"{", "malformed JSON"),
                (b"\xff\xfe", "input that is not UTF-8"),
                (b"   ", "an empty file"),
                ("{}", "a str rather than the saved bytes")):
            with self.assertRaises(ValueError, msg=why):
                convert(data)
        with self.assertRaises(ValueError, msg="a metadata value past the pinned 512 byte bound"):
            convert(response(metadata={"frame": "x" * 513}), image_key="frame")
        with self.assertRaises(ValueError, msg="a caller key outside the pinned key shape"):
            convert(response(metadata={"frame": "f"}), image_key="k" * 65)

    def test_the_expanded_output_is_bounded_in_utf8_bytes(self):
        big = response(output=[message(parts=[text_part("雨" * 4000)] * 32)])
        with self.assertRaises(ValueError, msg="an expansion past the byte budget") as caught:
            convert(big, output_budget=100_000)
        self.assertIn("UTF-8 bytes", str(caught.exception))
        self.assertIn("nothing is truncated", str(caught.exception))

    def test_every_emitted_column_is_declared_and_the_rows_reach_common_output(self):
        output, _ = convert(response(output=[message(parts=[
            text_part("first", logprobs=2), {"type": "refusal", "refusal": "second"}])],
            metadata={"frame": "frame-000012"}), image_key="frame")
        declared = PROFILE.read_text(encoding="utf-8").split("[units]")[1]
        for name in FIELDS:
            self.assertIn("\n%s = " % name, declared, name)
        self.assertNotIn('"', output)
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI supplies the built shared reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [reader, str(PROFILE), str(path), "--allow-equal-time"],
                capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["assertion_kind"], "OUTPUT_TEXT")
            self.assertEqual(fields["logprob_count"], 2)
            self.assertEqual(fields["response_status"], "completed")
            self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_the_command_line_writes_the_same_rows_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, out = base / "response.json", base / "out"
            source.write_bytes(response(metadata={"frame": "f1"}))
            subprocess.run([sys.executable, "-m", "scripts.convert_vlm_response", str(source),
                            str(out), "--image-key", "frame"], cwd=ROOT, check=True,
                           capture_output=True)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["rows"], 1)
            self.assertEqual(report["image_association"], "CALLER_DECLARED")
            self.assertEqual(report["model_reported"], "gpt-4o-2024-08-06")
            second = subprocess.run([sys.executable, "-m", "scripts.convert_vlm_response",
                                     str(source), str(out)], cwd=ROOT, capture_output=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("never overwrites", second.stderr.decode())


if __name__ == "__main__":
    unittest.main()
