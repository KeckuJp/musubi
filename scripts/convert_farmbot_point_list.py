"""Adapt one saved FarmBot v15.30.5 points response, not an entire account."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_ardupilot_battery_csv as shared

LIMIT = 16 * 1024**2
EXTRA = ["source_response_sha256", "source_array_index"]


def convert(raw, *, version, unit, capture_time_us):
    if type(raw) is not bytes or len(raw) > LIMIT:
        raise ValueError("bounded original response bytes required")
    if version != "v15.30.5" or unit != "mm":
        raise ValueError("qualified version and explicit unit required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("bounded capture time required")
    points = json.loads(raw.decode(), object_pairs_hook=shared.unique_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    if not isinstance(points, list) or len(points) > 100000:
        raise ValueError("finite point array required")
    source_sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline="")
    fields = ["record_time_us", "source_record_hex", *shared.FARM_POINT_FIELDS, *EXTRA]
    writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader()
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError("point object required")
        envelope = dict(exporter_version=version, coordinate_unit=unit,
                        capture_time_us=capture_time_us, point=point)
        try:
            text = shared.convert_json(json.dumps(envelope, separators=(",", ":")), shared.FARM_POINT_FORMAT)
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("invalid point resource") from exc
        rows = list(csv.DictReader(io.StringIO(text)))
        if len(rows) != 1 or list(rows[0]) != fields[:-2]:
            raise ValueError("unexpected shared converter shape")
        writer.writerow(dict(rows[0], source_response_sha256=source_sha, source_array_index=index))
        if output.tell() > LIMIT:
            raise ValueError("output exceeds bound; select smaller saved responses")
    result = output.getvalue()
    return result, dict(source_response_sha256=source_sha, source_items=len(points),
        converted_items=len(points), skipped_items=0, empty_response=not points,
        completeness="SUPPLIED_ARRAY_ONLY_PAGINATION_AND_ACCOUNT_COMPLETENESS_NOT_VERIFIED",
        clock="Unknown", source_version=version, coordinate_unit=unit,
        capture_time_us=capture_time_us, output_sha256="sha256:"+hashlib.sha256(result.encode()).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("response", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--capture-time-us", required=True, type=int)
    args = parser.parse_args()
    try:
        with args.response.open("rb") as source:
            raw = source.read(LIMIT+1)
        text, report = convert(raw, version=args.version, unit=args.unit, capture_time_us=args.capture_time_us)
        args.output.mkdir()
        (args.output/"source-response.json").write_bytes(raw)
        (args.output/"observations.csv").write_text(text)
        (args.output/"report.json").write_text(json.dumps(report, indent=2)+"\n")
    except (ValueError, OSError, UnicodeError, RecursionError):
        parser.exit(2, "saved point list refused; check version, shape, units and bounds\n")


if __name__ == "__main__":
    main()
