"""Authored inputs; the ISOXML chain runs the adopted converter itself. No device, no real log."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from scripts.convert_software_identity_binding import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/software-identity-binding/profile.toml"
from scripts.convert_isoxml_rate import APPLIED_CONVERTER_ID, APPLIED_CONVERTER_REVISION
CITATION = "isoxml-rate/README.md: the TASKDATA root's own declaration of which side wrote the file"


def saved(rows, header="record_time_us,writer_name,writer_version"):
    return (header + "\n" + "".join(",".join(str(cell) for cell in row) + "\n"
                                    for row in rows)).encode()


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


class SoftwareIdentityBindingTests(unittest.TestCase):
    def test_the_isoxml_writers_are_bound_to_the_observations_they_produced(self):
        """Authored TASKDATA and TIM through the adopted converter, then this binding."""
        from scripts.convert_isoxml_rate import convert as isoxml_convert
        template = (b'<TIM A="" D="4"><DLV A="0002" B="" C="DET-1"/>'
                    b'<DLV A="0002" B="" C="DET-2"/></TIM>')
        description = ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2" DataTransferOrigin="1"'
                       ' ManagementSoftwareManufacturer="Field Desk" ManagementSoftwareVersion="7.2"'
                       ' TaskControllerManufacturer="Terminal Co" TaskControllerVersion="3.1">'
                       '<DVC A="DVC-1"><DET A="DET-1" B="1" C="4" D="Left" E="1" F="0"/></DVC>'
                       '<DVC A="DVC-2"><DET A="DET-2" B="1" C="4" D="Right" E="2" F="0"/></DVC>'
                       '</ISO11783_TaskData>').encode()
        data = (struct.pack("<IH", 1000, 10000) + bytes([1]) + struct.pack("<Bi", 0, 10000)
                + struct.pack("<IH", 2000, 10000) + bytes([1]) + struct.pack("<Bi", 1, 20000))
        produced, _ = isoxml_convert(template, data, taskdata=description)
        source_rows = list(csv.DictReader(io.StringIO(produced)))
        self.assertEqual(len(source_rows), 2)  # the adopted converter's own observations
        output, report = convert(
            produced.encode(), source_citation=CITATION,
            read_components=("SOURCE_SOFTWARE:NAME=declared_management_software_hex",
                             "SOURCE_SOFTWARE:VERSION=declared_management_software_version_hex",
                             "SOURCE_SOFTWARE:NAME=declared_task_controller_hex",
                             "SOURCE_SOFTWARE:VERSION=declared_task_controller_version_hex",
                             "CONVERSION_TOOL:NAME=applied_converter_id",
                             "CONVERSION_TOOL:VERSION=applied_converter_revision"),
            declared_components=("REFERENCE_SPECIFICATION:PIN=ISOv4Plugin@5bcf596c60fa11bcf638b308df478df906b965a5",
                                 "ADAPTER_PROFILE:NAME=isoxml_actual_rate_changes_v1"))
        rows = list(csv.DictReader(io.StringIO(output)))
        read = [(r["identity_role"], r["identity_kind"], unhex(unhex(r["identity_value_hex"])),
                 r["identity_evidence"], r["identity_rows_carrying_value"]) for r in rows[:4]]
        self.assertEqual(read, [
            ("SOURCE_SOFTWARE", "NAME", "Field Desk", "READ_FROM_THE_OBSERVATION_ROWS", "2"),
            ("SOURCE_SOFTWARE", "VERSION", "7.2", "READ_FROM_THE_OBSERVATION_ROWS", "2"),
            ("SOURCE_SOFTWARE", "NAME", "Terminal Co", "READ_FROM_THE_OBSERVATION_ROWS", "2"),
            ("SOURCE_SOFTWARE", "VERSION", "3.1", "READ_FROM_THE_OBSERVATION_ROWS", "2")])
        self.assertEqual([(r["identity_role"], r["identity_kind"], r["identity_evidence"],
                           unhex(r["identity_value_hex"])) for r in rows[4:]],
                         [("CONVERSION_TOOL", "NAME", "READ_FROM_THE_OBSERVATION_ROWS",
                           APPLIED_CONVERTER_ID),
                          ("CONVERSION_TOOL", "VERSION", "READ_FROM_THE_OBSERVATION_ROWS",
                           APPLIED_CONVERTER_REVISION),
                          ("REFERENCE_SPECIFICATION", "PIN", "CALLER_DECLARED",
                           "ISOv4Plugin@5bcf596c60fa11bcf638b308df478df906b965a5"),
                          ("ADAPTER_PROFILE", "NAME", "CALLER_DECLARED",
                           "isoxml_actual_rate_changes_v1")])
        self.assertEqual([r["identity_rows_carrying_value"] for r in rows[4:6]], ["2", "2"])
        self.assertEqual(json.loads(unhex(rows[5]["identity_join_hex"])),
                         {"applied_converter_revision": APPLIED_CONVERTER_REVISION})
        self.assertIn("REFERENCE_SPECIFICATION_IS_A_DOCUMENT_OR_UPSTREAM_SOURCE",
                      rows[6]["identity_basis"])
        digest = "sha256:" + hashlib.sha256(produced.encode()).hexdigest()
        self.assertTrue(all(r["bound_observations_sha256"] == digest for r in rows))
        self.assertTrue(all(r["bound_row_count"] == "2" for r in rows))
        self.assertEqual((rows[0]["bound_first_record_time_us"], rows[0]["bound_last_record_time_us"]),
                         (source_rows[0]["record_time_us"], source_rows[1]["record_time_us"]))
        self.assertIn("NOT_AUTHENTICATED", rows[0]["identity_basis"])
        self.assertEqual((report["components_read_from_rows"], report["components_caller_declared"],
                          report["components_unknown"]), (6, 2, 0))
        self.assertEqual(report["authenticated_identity_records"], "NOT_DERIVED")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "rates.csv").write_text(produced)
            subprocess.run([os.sys.executable,
                str(ROOT / "scripts/convert_software_identity_binding.py"),
                str(base / "rates.csv"), str(base / "out"), "--source-citation", CITATION,
                "--read-component", "SOURCE_SOFTWARE:NAME=declared_management_software_hex",
                "--read-component", "SOURCE_SOFTWARE:VERSION=declared_management_software_version_hex",
                "--read-component", "SOURCE_SOFTWARE:NAME=declared_task_controller_hex",
                "--read-component", "SOURCE_SOFTWARE:VERSION=declared_task_controller_version_hex",
                "--read-component", "CONVERSION_TOOL:NAME=applied_converter_id",
                "--read-component", "CONVERSION_TOOL:VERSION=applied_converter_revision",
                "--declare-component",
                "REFERENCE_SPECIFICATION:PIN=ISOv4Plugin@5bcf596c60fa11bcf638b308df478df906b965a5",
                "--declare-component", "ADAPTER_PROFILE:NAME=isoxml_actual_rate_changes_v1"],
                check=True, capture_output=True)
            self.assertEqual((base / "out/observations.csv").read_text(), output)
            if not os.environ.get("MUSUBI_TELEMETRY_READER"):
                self.skipTest("MUSUBI_TELEMETRY_READER not configured; the common-output half of "
                              "this test is the point, so a missing reader is an explicit skip")
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(PROFILE), str(base / "out/observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 8)
        fields = common["observations"][0]["fields"]
        self.assertEqual(fields["identity_role"], "SOURCE_SOFTWARE")
        self.assertEqual(fields["identity_evidence"], "READ_FROM_THE_OBSERVATION_ROWS")
        self.assertEqual(fields["bound_row_count"], 2)
        tool = common["observations"][4]["fields"]
        self.assertEqual((tool["identity_role"], tool["identity_evidence"]),
                         ("CONVERSION_TOOL", "READ_FROM_THE_OBSERVATION_ROWS"))
        self.assertIn("never_interchanged", common["profile_units"]["identity_role"])
        self.assertIn("not_authenticated", common["profile_units"]["identity_basis"])

    def test_a_caller_claim_about_the_converter_never_overwrites_what_the_rows_say(self):
        """The executed route and a caller's claim disagree: both survive, neither is chosen."""
        header = "record_time_us,applied_converter_id,applied_converter_revision"
        output, report = convert(
            saved([(1000, APPLIED_CONVERTER_ID, APPLIED_CONVERTER_REVISION),
                   (2000, APPLIED_CONVERTER_ID, APPLIED_CONVERTER_REVISION)], header=header),
            read_components=("CONVERSION_TOOL:VERSION=applied_converter_revision",),
            declared_components=("CONVERSION_TOOL:VERSION=9",))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([(unhex(r["identity_value_hex"]), r["identity_evidence"],
                           r["identity_rows_carrying_value"]) for r in rows],
                         [(APPLIED_CONVERTER_REVISION, "READ_FROM_THE_OBSERVATION_ROWS", "2"),
                          ("9", "CALLER_DECLARED", "")])
        self.assertNotEqual(unhex(rows[0]["identity_value_hex"]),
                            unhex(rows[1]["identity_value_hex"]))
        self.assertEqual((report["components_read_from_rows"],
                          report["components_caller_declared"]), (1, 1))
        blank, blank_report = convert(
            saved([(1000, APPLIED_CONVERTER_ID, "")], header=header),
            read_components=("CONVERSION_TOOL:VERSION=applied_converter_revision",),
            declared_components=("CONVERSION_TOOL:VERSION=9",))
        first = next(csv.DictReader(io.StringIO(blank)))
        self.assertEqual((first["identity_evidence"], first["identity_value_hex"]),
                         ("UNKNOWN_NOT_REPORTED", ""))
        self.assertEqual(blank_report["components_unknown"], 1)
        with self.assertRaises(ValueError) as absent:
            convert(saved([(1000, "x", "y")]),
                    read_components=("CONVERSION_TOOL:VERSION=applied_converter_revision",))
        self.assertIn("not in the saved header", str(absent.exception))

    def test_unknown_stays_unknown_and_is_never_filled_in(self):
        data = saved([(1000, "logger", ""), (2000, "logger", "")])
        output, report = convert(data, read_components=("SOURCE_SOFTWARE:VERSION=writer_version",),
                                 declared_components=("CONVERSION_TOOL:VERSION=",))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([(r["identity_role"], r["identity_evidence"], r["identity_value_hex"],
                           r["identity_rows_carrying_value"]) for r in rows],
                         [("SOURCE_SOFTWARE", "UNKNOWN_NOT_REPORTED", "", "0"),
                          ("CONVERSION_TOOL", "UNKNOWN_NOT_REPORTED", "", "")])
        self.assertEqual(unhex(rows[0]["identity_source_hex"]), "writer_version")
        self.assertEqual((report["components_unknown"], report["distinct_values_emitted"]), (2, 0))

    def test_a_mixed_source_file_keeps_both_identities_and_both_instances(self):
        data = saved([(1000, "alpha", "1.0"), (2000, "beta", "2.0"), (3000, "alpha", "1.0")])
        output, report = convert(data, read_components=("SOURCE_SOFTWARE:NAME=writer_name",),
                                 declared_components=("SOURCE_SOFTWARE:NAME=alpha",),
                                 instance="second-instance-of-alpha")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([(unhex(r["identity_value_hex"]), r["identity_rows_carrying_value"],
                           r["identity_collision"]) for r in rows[:2]],
                         [("alpha", "2", "TWO_OR_MORE_DISTINCT_VALUES_IN_THE_BOUND_OBSERVATIONS"),
                          ("beta", "1", "TWO_OR_MORE_DISTINCT_VALUES_IN_THE_BOUND_OBSERVATIONS")])
        self.assertEqual(unhex(rows[2]["identity_value_hex"]), "alpha")
        self.assertEqual(unhex(rows[2]["identity_instance_hex"]), "second-instance-of-alpha")
        self.assertEqual(rows[2]["identity_evidence"], "CALLER_DECLARED")
        self.assertEqual((report["components_with_a_collision"], report["distinct_values_emitted"]),
                         (1, 3))

    def test_a_paired_identity_stays_on_its_own_row_and_can_be_joined_back(self):
        """Names and versions swapped across rows must not cross-product, and must be re-findable."""
        data = saved([(1000, "alpha", "1.0"), (2000, "beta", "2.0"),
                      (3000, "alpha", "2.0"), (4000, "beta", "1.0"),
                      (5000, "gamma", "")])
        output, report = convert(
            data, paired_identities=("SOURCE_SOFTWARE=NAME:writer_name,VERSION:writer_version",))
        rows = list(csv.DictReader(io.StringIO(output)))
        pairs = [(json.loads(unhex(r["identity_value_hex"])), r["identity_rows_carrying_value"],
                  r["identity_missing_components"]) for r in rows]
        self.assertEqual(pairs, [
            ({"NAME": "alpha", "VERSION": "1.0"}, "1", ""),
            ({"NAME": "alpha", "VERSION": "2.0"}, "1", ""),
            ({"NAME": "beta", "VERSION": "1.0"}, "1", ""),
            ({"NAME": "beta", "VERSION": "2.0"}, "1", ""),
            ({"NAME": "gamma", "VERSION": None}, "1", "VERSION")])
        self.assertTrue(all(r["identity_kind"] == "NAME+VERSION" for r in rows))
        self.assertTrue(all(r["identity_evidence"] == "READ_FROM_THE_OBSERVATION_ROWS" for r in rows))
        self.assertTrue(all("TWO_OR_MORE_DISTINCT" in r["identity_collision"] for r in rows))
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        original = list(csv.DictReader(io.StringIO(data.decode())))
        for row in rows:
            self.assertEqual(row["bound_observations_sha256"], digest)
            selector = json.loads(unhex(row["identity_join_hex"]))
            selected = [source for source in original
                        if all(source[column] == value for column, value in selector.items())]
            self.assertEqual(len(selected), int(row["identity_rows_carrying_value"]))
        self.assertEqual((report["paired_identities"], report["distinct_pairs_emitted"],
                          report["pairs_with_a_missing_component"]), (1, 5, 1))

    def test_what_it_cannot_supply_itself_is_refused(self):
        good = saved([(1000, "logger", "1.0")])
        for kwargs, why in (
                (dict(), "nothing selected at all"),
                (dict(read_components=("SOURCE_SOFTWARE:NAME=nope",)), "a column not in the header"),
                (dict(read_components=("SOURCE_SOFTWARE:NAME=",)), "a column-read with no column"),
                (dict(read_components=("WRITER:NAME=writer_name",)), "an unknown role"),
                (dict(read_components=("SOURCE_SOFTWARE:BUILD=writer_name",)), "an unknown kind"),
                (dict(read_components=("SOURCE_SOFTWARE=writer_name",)), "no role/kind separator"),
                (dict(declared_components=("SOURCE_SOFTWARE:NAME",)), "no value separator"),
                (dict(read_components=("SOURCE_SOFTWARE:NAME=writer_name",
                                       "SOURCE_SOFTWARE:NAME=writer_name")), "a duplicate read"),
                (dict(read_components=("SOURCE_SOFTWARE:NAME=writer_name",), instance=""),
                 "an empty instance label"),
                (dict(declared_components=("SOURCE_SOFTWARE:NAME=" + "x" * 257,)),
                 "an oversized declared value"),
                (dict(paired_identities=("SOURCE_SOFTWARE=NAME:writer_name",)),
                 "a paired identity with only one part"),
                (dict(paired_identities=("SOURCE_SOFTWARE=NAME:writer_name,VERSION:writer_name",)),
                 "a paired identity repeating one column"),
                (dict(paired_identities=("SOURCE_SOFTWARE=NAME:writer_name,NAME:writer_version",)),
                 "a paired identity repeating one kind, which the JSON object would overwrite"),
                (dict(paired_identities=("SOURCE_SOFTWARE=NAME:writer_name,VERSION:nope",)),
                 "a paired identity column not in the header"),
                (dict(paired_identities=("SOURCE_SOFTWARE=writer_name,writer_version",)),
                 "a paired identity with no kinds"),
                (dict(paired_identities=("WRITER=NAME:writer_name,VERSION:writer_version",)),
                 "a paired identity with an unknown role")):
            with self.assertRaises(ValueError, msg=why):
                convert(good, **kwargs)
        for broken, why in (
                (saved([(2000, "a", "1"), (1000, "b", "2")]), "a saved time that decreases"),
                (saved([(1000, "a", "1")], header="record_time_us,writer_name,record_time_us"),
                 "a duplicate saved column name"),
                (b"record_time_us,writer_name,writer_version\n", "a file with no rows")):
            with self.assertRaises(ValueError, msg=why):
                convert(broken, read_components=("SOURCE_SOFTWARE:NAME=writer_name",))
        wide, report = convert(saved([(1000, "W" * 60000, "1")]),
                               read_components=("SOURCE_SOFTWARE:NAME=writer_name",))
        self.assertEqual(report["output_bytes"], len(wide.encode("utf-8")))
        with self.assertRaises(ValueError) as unparsed:
            convert(saved([(1000, "W" * 200000, "1")]),
                    read_components=("SOURCE_SOFTWARE:NAME=writer_name",))
        self.assertIn("cannot be parsed as written", str(unparsed.exception))


class CitationAndNamespaceTests(unittest.TestCase):
    """A read row used to lose the caller's citation: identity_source_hex was overwritten with the
    column name. Both now travel on every row, and a namespace is a separate opt-in statement."""

    DATA = saved([(1000, "alpha", "1.0"), (2000, "beta", "2.0"),
                  (3000, "alpha", "2.0"), (4000, "beta", "1.0")])

    def rows(self, **kwargs):
        output, report = convert(self.DATA, **kwargs)
        return list(csv.DictReader(io.StringIO(output))), report

    def test_a_read_row_keeps_the_column_and_the_citation_and_the_namespace(self):
        rows, report = self.rows(
            read_components=("SOURCE_SOFTWARE:NAME=writer_name",),
            declared_components=("REFERENCE_SPECIFICATION:NAME=upstream-project",),
            source_citation="vendor release note RN-14", namespace="acme.example/products")
        read = [r for r in rows if r["identity_evidence"] == "READ_FROM_THE_OBSERVATION_ROWS"]
        declared = [r for r in rows if r["identity_evidence"] == "CALLER_DECLARED"]
        self.assertTrue(read and declared)
        for row in read:
            self.assertEqual(unhex(row["identity_source_hex"]), "writer_name")
            self.assertEqual(unhex(row["identity_citation_hex"]), "vendor release note RN-14")
            self.assertEqual(unhex(row["identity_namespace_hex"]), "acme.example/products")
        for row in declared:
            self.assertEqual(unhex(row["identity_citation_hex"]), "vendor release note RN-14")
            self.assertEqual(unhex(row["identity_namespace_hex"]), "acme.example/products")
        for row in rows:
            self.assertIn("CALLER_SUPPLIED_WORDS", row["identity_provenance_basis"])
            self.assertIn("NEITHER_STRING_CONFERS_THAT_AUTHORITY", row["identity_provenance_basis"])
            self.assertIn("NEVER_EVIDENCE_OF_AUTHENTICITY_OWNERSHIP_OR_LINEAGE",
                          row["identity_provenance_basis"])
        self.assertEqual(report["declared_namespace"], "acme.example/products")
        self.assertEqual(report["declared_source_citation"], "vendor release note RN-14")

    def test_an_absent_citation_or_namespace_stays_blank_and_is_never_defaulted(self):
        rows, report = self.rows(read_components=("SOURCE_SOFTWARE:NAME=writer_name",))
        for row in rows:
            self.assertEqual(row["identity_citation_hex"], "")
            self.assertEqual(row["identity_namespace_hex"], "")
            self.assertEqual(unhex(row["identity_source_hex"]), "writer_name")
        self.assertIsNone(report["declared_namespace"])
        cited, _ = self.rows(read_components=("SOURCE_SOFTWARE:NAME=writer_name",),
                             source_citation="a document that names no authority")
        for row in cited:
            self.assertEqual(unhex(row["identity_citation_hex"]),
                             "a document that names no authority")
            self.assertEqual(row["identity_namespace_hex"], "")

    def test_one_value_under_two_namespaces_stays_two_distinguishable_claims(self):
        first, _ = self.rows(read_components=("SOURCE_SOFTWARE:NAME=writer_name",),
                             namespace="acme.example/products")
        second, _ = self.rows(read_components=("SOURCE_SOFTWARE:NAME=writer_name",),
                              namespace="integrator.example/rebrands")
        pick = lambda rows: next(r for r in rows
                                 if unhex(r["identity_value_hex"]) == "alpha")
        one, two = pick(first), pick(second)
        self.assertEqual(unhex(one["identity_value_hex"]), unhex(two["identity_value_hex"]))
        self.assertEqual(one["bound_observations_sha256"], two["bound_observations_sha256"])
        self.assertNotEqual(one["identity_namespace_hex"], two["identity_namespace_hex"])
        self.assertEqual(unhex(one["identity_namespace_hex"]), "acme.example/products")
        self.assertEqual(unhex(two["identity_namespace_hex"]), "integrator.example/rebrands")

    def test_a_paired_identity_still_joins_back_and_reaches_the_common_reader(self):
        rows, _ = self.rows(
            paired_identities=("SOURCE_SOFTWARE=NAME:writer_name,VERSION:writer_version",),
            source_citation="vendor release note RN-14", namespace="acme.example/products")
        self.assertEqual(len(rows), 4)
        source = list(csv.DictReader(io.StringIO(self.DATA.decode())))
        for row in rows:
            filter_ = json.loads(unhex(row["identity_join_hex"]))
            hits = [s for s in source if all(s[c] == v for c, v in filter_.items())]
            self.assertEqual(len(hits), int(row["identity_rows_carrying_value"]))
            self.assertEqual(unhex(row["identity_source_hex"]), "writer_name,writer_version")
            self.assertEqual(unhex(row["identity_citation_hex"]), "vendor release note RN-14")
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI sets the built shared reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            output, _ = convert(self.DATA,
                                paired_identities=("SOURCE_SOFTWARE=NAME:writer_name,"
                                                   "VERSION:writer_version",),
                                source_citation="vendor release note RN-14",
                                namespace="acme.example/products")
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run([reader, str(PROFILE), str(path),
                                                "--allow-equal-time"],
                                               capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 4)
            fields = common["observations"][0]["fields"]
            self.assertEqual(unhex(fields["identity_namespace_hex"]), "acme.example/products")
            self.assertEqual(unhex(fields["identity_citation_hex"]), "vendor release note RN-14")

    THREE_PART = ("SOURCE_SOFTWARE=NAME:writer_name,VERSION:writer_version,PIN:writer_pin",)
    GAPPED = saved([(1000, "alpha", "1.0", "p1"), (2000, "alpha", "", "p1"),
                    (3000, "", "", "p1"), (4000, "", "", ""), (5000, "beta", "2.0", "p2")],
                   header="record_time_us,writer_name,writer_version,writer_pin")

    def test_two_or_more_missing_parts_are_all_named_and_still_reach_the_common_reader(self):
        output, report = convert(self.GAPPED, paired_identities=self.THREE_PART,
                                 source_citation="vendor release note RN-14",
                                 namespace="acme.example/products")
        self.assertNotIn('"', output)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(
            [(json.loads(unhex(r["identity_value_hex"])), r["identity_rows_carrying_value"],
              r["identity_missing_components"]) for r in rows],
            [({"NAME": None, "VERSION": None, "PIN": None}, "1", "NAME+VERSION+PIN"),
             ({"NAME": None, "VERSION": None, "PIN": "p1"}, "1", "NAME+VERSION"),
             ({"NAME": "alpha", "VERSION": None, "PIN": "p1"}, "1", "VERSION"),
             ({"NAME": "alpha", "VERSION": "1.0", "PIN": "p1"}, "1", ""),
             ({"NAME": "beta", "VERSION": "2.0", "PIN": "p2"}, "1", "")])
        for row in rows:
            self.assertEqual(row["identity_kind"], "NAME+VERSION+PIN")
            self.assertTrue(set(filter(None, row["identity_missing_components"].split("+")))
                            <= set(row["identity_kind"].split("+")))
            self.assertEqual(unhex(row["identity_citation_hex"]), "vendor release note RN-14")
            self.assertEqual(unhex(row["identity_namespace_hex"]), "acme.example/products")
            self.assertEqual(unhex(row["identity_source_hex"]),
                             "writer_name,writer_version,writer_pin")
        self.assertTrue(all(r["identity_collision"] ==
                            "TWO_OR_MORE_DISTINCT_VALUES_IN_THE_BOUND_OBSERVATIONS" for r in rows))
        self.assertEqual((report["distinct_pairs_emitted"], report["pairs_with_a_missing_component"],
                          report["components_with_a_collision"]), (5, 3, 1))
        source = list(csv.DictReader(io.StringIO(self.GAPPED.decode())))
        for row in rows:
            filter_ = json.loads(unhex(row["identity_join_hex"]))
            hits = [s for s in source if all(s[c] == v for c, v in filter_.items())]
            self.assertEqual(len(hits), int(row["identity_rows_carrying_value"]))
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI sets the built shared reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run([reader, str(PROFILE), str(path),
                                                "--allow-equal-time"],
                                               capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 5)
            self.assertEqual(
                [o["fields"].get("identity_missing_components") for o in common["observations"]],
                ["NAME+VERSION+PIN", "NAME+VERSION", "VERSION", None, None])

    def test_a_comma_inside_a_saved_value_never_reaches_the_emitted_file_unhexed(self):
        """An ordinary saved name may contain a comma; it is carried hex-encoded, not quoted."""
        data = ('record_time_us,writer_name,writer_version\n1000,"ac,me","1,0"\n').encode()
        output, _ = convert(data, paired_identities=(
            "SOURCE_SOFTWARE=NAME:writer_name,VERSION:writer_version",))
        self.assertNotIn('"', output)
        row = list(csv.DictReader(io.StringIO(output)))[0]
        self.assertEqual(json.loads(unhex(row["identity_value_hex"])),
                         {"NAME": "ac,me", "VERSION": "1,0"})
        self.assertEqual(row["identity_missing_components"], "")


class RecordedExperimentComposition(unittest.TestCase):
    """The estate's own `name (unit)` header shape, and the run identity it was blocking.

    Several adopted recorded-output converters write their header as `name (unit)`, and the common
    reader already reads that against a profile declaring the bare name. This binding refused it, so
    those outputs could not be given a declared run or entity identity at all.
    """

    def test_the_estate_header_shape_is_accepted_and_nothing_unsafe_is(self):
        from scripts.convert_software_identity_binding import declared_name
        for name in ("record_time", "record_time (us)", "tx (m)", "speed (m/s)", "load (%)"):
            self.assertEqual(declared_name(name), name)
        for bad in ("a,b", 'a"b', "a\nb", "x ()", "x (a,b)", "x  (m)",
                    "x (waytoolongunitname)", "x (m) (m)", " x", 7):
            with self.assertRaises(ValueError, msg=repr(bad)):
                declared_name(bad)

    def test_two_runs_of_one_recorded_experiment_stay_distinguishable(self):
        """A recorded pose run, given a declared run identity bound to its exact bytes."""
        from scripts.convert_pose_text import convert as pose
        observations, _ = pose("# authored, not a captured dataset\n"
                               "1305031910.765238 1.2 0.4 1.5 0 0 0 1\n"
                               "1305031910.797230 1.3 0.4 1.5 0 0 0 1\n")
        self.assertTrue(observations.startswith("record_time (us),"))
        bound = []
        for run in ("run-A", "run-B"):
            output, report = convert(
                observations.encode(), time_column="record_time (us)",
                declared_components=("SOURCE_SOFTWARE:NAME=slam-variant-1",), instance=run,
                source_citation="authored experiment note", namespace="lab.example/runs")
            row = list(csv.DictReader(io.StringIO(output)))[0]
            bound.append(row)
            self.assertEqual(unhex(row["identity_instance_hex"]), run)
            self.assertEqual(row["bound_row_count"], "2")
            self.assertTrue(row["bound_observations_sha256"].startswith("sha256:"))
            self.assertEqual(report["declared_namespace"], "lab.example/runs")
        self.assertEqual(unhex(bound[0]["identity_value_hex"]),
                         unhex(bound[1]["identity_value_hex"]))
        self.assertNotEqual(bound[0]["identity_instance_hex"], bound[1]["identity_instance_hex"])
        for row in bound:
            self.assertIn("TWO_EQUAL_NAMES_ARE_NEVER_ASSUMED_TO_BE_ONE_INSTANCE",
                          row["identity_basis"])

    def test_the_same_path_is_reused_on_another_compatible_saved_output(self):
        """A second producer, same header shape, same binding, same reader -- no new adapter."""
        from scripts.convert_trajectory_csv import convert as trajectory
        observations = trajectory(
            "time,x_true,y_true,x_pred,y_pred,total_time_ms\n"
            "0.0,0.0,0.0,0.01,0.0,12\n0.1,0.1,0.0,0.11,0.0,11\n", "time")
        self.assertTrue(observations.startswith("record_time (us),"))
        output, _ = convert(observations.encode(), time_column="record_time (us)",
                            declared_components=("SOURCE_SOFTWARE:NAME=student-planner-v3",),
                            instance="assignment-4-attempt-2")
        row = list(csv.DictReader(io.StringIO(output)))[0]
        self.assertEqual(unhex(row["identity_value_hex"]), "student-planner-v3")
        self.assertEqual(unhex(row["identity_instance_hex"]), "assignment-4-attempt-2")
        self.assertIn("NOT_EVIDENCE_THAT_THE_NAMED_SOFTWARE_PRODUCED_ANYTHING",
                      row["identity_basis"])
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI supplies the built shared reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [reader, str(PROFILE), str(path), "--allow-equal-time"],
                capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 1)
            self.assertEqual(unhex(common["observations"][0]["fields"]["identity_instance_hex"]),
                             "assignment-4-attempt-2")


if __name__ == "__main__":
    unittest.main()
