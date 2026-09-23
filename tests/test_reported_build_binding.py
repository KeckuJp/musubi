"""A node's own reported firmware image identifier, bound to the observations it sits beside.

Authored frames on disk only: nothing is requested, nothing is transmitted, no device is touched
and no real capture is read. The chain is entirely adopted components -- the pinned passive
DroneCAN converter, then the software identity binding, then the built common reader -- so this
module adds a declaration and its proof, not a second decoder.

What the binding says: *this node declared this image identifier on these saved rows*. What it
never says: that the named image is installed, that it is running, that it was built from the
commit reported beside it, or that two nodes reporting equal tokens are running the same binary.
The definition leaves the hash function implementation-defined, so the token is opaque and equality
is **reported equality only**.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_software_identity_binding import convert

try:  # the pinned passive decoder and definitions; required CI fetches both at their commits
    import dronecan  # noqa: F401
    from scripts.convert_dronecan_capture import convert as dronecan_convert
    from tests.test_dronecan_capture import (message_frames, node_info, node_status, numbered,
                                             service_frames, text)
    DECODER = True
except ModuleNotFoundError:  # pragma: no cover - exercised only where the pin is absent
    DECODER = False

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/software-identity-binding/profile.toml"
IMAGE = "SOURCE_SOFTWARE=NAME:node_name_reported_hex,DIGEST:node_software_image_crc_hex"
COMMIT = "SOURCE_SOFTWARE=NAME:node_name_reported_hex,PIN:node_software_vcs_commit_hex"
CITATION = ("dronecan-recorded/profile.toml node_software_image_crc_hex: the source calls the exact "
            "hash function implementation-defined")
NAMESPACE = "dronecan-node-self-reported-not-a-naming-authority"
ALPHA, BETA = "hex:" + b"alpha.node".hex(), "hex:" + b"beta.node".hex()
FIRST_IMAGE, SECOND_IMAGE = "hex:aabbccdd11223344", "hex:0011223344556677"
FIRST_COMMIT, SECOND_COMMIT = "hex:11223344", "hex:55667788"


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


@unittest.skipUnless(DECODER, "the pinned passive DroneCAN decoder and definitions; required CI "
                              "fetches them at their pinned commits")
class ReportedBuildBinding(unittest.TestCase):
    def observations(self):
        """One authored capture: a present image, an absent one, a changed one, and a status row."""
        frames = []
        frames += service_frames(
            node_info(hardware=(1, 2), software=(3, 4), flags=3, unique=bytes(range(1, 17)),
                      name="alpha.node", commit=0x11223344, crc=0xAABBCCDD11223344),
            source=20, destination=1, transfer_id=5)
        frames += service_frames(
            node_info(hardware=(1, 2), software=(3, 4), flags=1, unique=bytes(range(17, 33)),
                      name="beta.node", commit=0x11223344, crc=0xAABBCCDD11223344),
            source=21, destination=1, transfer_id=6)
        frames += service_frames(
            node_info(hardware=(1, 2), software=(3, 5), flags=3, unique=bytes(range(1, 17)),
                      name="alpha.node", commit=0x55667788, crc=0x0011223344556677),
            source=20, destination=1, transfer_id=7)
        frames += message_frames(node_status(1234, 1, 0), source=20, transfer_id=1)
        return dronecan_convert(text(numbered([(f.message_id, f.bytes.hex())
                                               for f in frames])))[0].encode()

    def bound(self):
        return convert(self.observations(), paired_identities=(IMAGE, COMMIT),
                       source_citation=CITATION, namespace=NAMESPACE)

    def test_a_reported_image_identifier_binds_with_its_node_and_keeps_absence_absent(self):
        output, report = self.bound()
        rows = list(csv.DictReader(io.StringIO(output)))
        image = [r for r in rows if r["identity_kind"] == "NAME+DIGEST"]
        self.assertEqual(
            [(json.loads(unhex(r["identity_value_hex"])), r["identity_missing_components"])
             for r in image],
            [({"NAME": None, "DIGEST": None}, "NAME+DIGEST"),
             ({"NAME": ALPHA, "DIGEST": SECOND_IMAGE}, ""),
             ({"NAME": ALPHA, "DIGEST": FIRST_IMAGE}, ""),
             ({"NAME": BETA, "DIGEST": None}, "DIGEST")])
        self.assertTrue(all(r["identity_collision"] ==
                            "TWO_OR_MORE_DISTINCT_VALUES_IN_THE_BOUND_OBSERVATIONS" for r in image))
        self.assertEqual(report["pairs_with_a_missing_component"], 3)
        self.assertTrue(all(value.startswith("hex:")
                            for row in image
                            for value in json.loads(unhex(row["identity_value_hex"])).values()
                            if value is not None))

    def test_the_source_commit_and_the_image_identifier_are_never_merged(self):
        rows = list(csv.DictReader(io.StringIO(self.bound()[0])))
        image = [r for r in rows if r["identity_kind"] == "NAME+DIGEST"]
        commit = [r for r in rows if r["identity_kind"] == "NAME+PIN"]
        self.assertTrue(image and commit)
        self.assertEqual({unhex(r["identity_source_hex"]) for r in image},
                         {"node_name_reported_hex,node_software_image_crc_hex"})
        self.assertEqual({unhex(r["identity_source_hex"]) for r in commit},
                         {"node_name_reported_hex,node_software_vcs_commit_hex"})
        self.assertEqual(
            sorted(json.loads(unhex(r["identity_value_hex"]))["PIN"] or "" for r in commit),
            ["", FIRST_COMMIT, FIRST_COMMIT, SECOND_COMMIT])
        for row in rows:
            self.assertIn("NOT_A_BUILD_ATTESTATION", row["identity_basis"])
            self.assertIn("NOT_EVIDENCE_THAT_THE_NAMED_SOFTWARE_PRODUCED_ANYTHING",
                          row["identity_basis"])
            self.assertEqual(unhex(row["identity_citation_hex"]), CITATION)
            self.assertEqual(unhex(row["identity_namespace_hex"]), NAMESPACE)

    def test_the_join_reselects_its_own_rows_and_the_whole_binding_reaches_common_output(self):
        observations = self.observations()
        output, _ = convert(observations, paired_identities=(IMAGE, COMMIT),
                            source_citation=CITATION, namespace=NAMESPACE)
        source = list(csv.DictReader(io.StringIO(observations.decode())))
        for row in csv.DictReader(io.StringIO(output)):
            selector = json.loads(unhex(row["identity_join_hex"]))
            hits = [s for s in source if all(s[c] == v for c, v in selector.items())]
            self.assertEqual(len(hits), int(row["identity_rows_carrying_value"]))
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI supplies the built shared reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run([reader, str(PROFILE), str(path),
                                                "--allow-equal-time"],
                                               capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 8)
            self.assertEqual(
                [o["fields"].get("identity_missing_components") for o in common["observations"]],
                ["NAME+DIGEST", None, None, "DIGEST", "NAME+PIN", None, None, None])


if __name__ == "__main__":
    unittest.main()
