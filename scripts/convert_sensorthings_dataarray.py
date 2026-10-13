#!/usr/bin/env python3
"""Supplied SensorThings1.1 dataArray responses, never an API client."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

try:
    from . import convert_opcua_recorded as existing
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import convert_opcua_recorded as existing

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_GROUPS = 512
MAX_COMPONENTS = 64
MAX_NATIVE_ROWS = 200000
MAX_SELECTED_ROWS = 10000
REQUIRED = ("id", "phenomenonTime", "resultTime", "result")
LINKS = ("Datastream@iot.navigationLink", "MultiDatastream@iot.navigationLink")


def digest(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def convert(text, datastream_text, *, datastream_link, property_definition, capture_time_us, result_kind="celsius",
            start_row=None, stop_row=None):
    window = start_row is not None or stop_row is not None
    if window and (type(start_row) is not int or type(stop_row) is not int or
                   not 0 <= start_row < stop_row or stop_row - start_row > MAX_SELECTED_ROWS):
        raise ValueError("explicit nonempty native row window of at most10000 rows required")
    if any(not v or len(v.encode("utf-8")) > MAX_INPUT_BYTES for v in (text, datastream_text)):
        raise ValueError("bounded dataArray and metadata inputs required")
    source = existing.saved_page_gate(text, property_definition, capture_time_us, "dataArray object required")
    stream = existing.strict_json(datastream_text)
    existing.validate_sensorthings_datastream(stream, property_definition, result_kind)
    if not isinstance(datastream_link, str) or not datastream_link or stream.get("@iot.selfLink") != datastream_link:
        raise ValueError("exact saved Datastream selfLink binding required")
    identifier = stream.get("@iot.id")
    if type(identifier) not in (int, str) or isinstance(identifier, str) and not identifier:
        raise ValueError("explicit Datastream id required")
    groups = source.get("value")
    if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
        raise ValueError("bounded dataArray groups required")
    if "@iot.count" in source and (type(source["@iot.count"]) is not int or source["@iot.count"] < 0):
        raise ValueError("invalid reported collection count")
    if "@iot.nextLink" in source and (not isinstance(source["@iot.nextLink"], str) or not source["@iot.nextLink"]):
        raise ValueError("invalid nextLink")
    # Copy only qualified metadata into the internal projection; preserve all original
    # metadata once in final output instead of repeating unknown objects per observation.
    selected_stream = {key: stream[key] for key in ("@iot.id", "observationType", "unitOfMeasurement")}
    selected_stream["ObservedProperty"] = {"definition": property_definition}
    source_sha, metadata_sha = digest(text), digest(datastream_text)
    report = dict(schema="sensorthings-dataarray/v1", source_sha256=source_sha,
                  metadata_sha256=metadata_sha, native_rows=0, selected_rows=0, excluded_rows=0,
                  selected_groups=0, excluded_groups=[], empty_selected_groups=[],
                  converted_values=0, withheld_values=0, repeated_observation_ids=0,
                  next_link=source.get("@iot.nextLink"), reported_count=source.get("@iot.count"),
                  scope="SUPPLIED_RESPONSE_ONLY_NEVER_SERVER_COMPLETENESS",
                  outside_selection="STRUCTURE_CHECKED_VALUES_UNITS_TIMES_NOT_INTERPRETED",
                  original_inputs="RETAIN_INPUT_PAIR_WITH_REPORT_EMPTY_SELECTION_HAS_NO_CSV_ROWS",
                  clock="Unknown")
    if result_kind != "celsius":
        report["result_kind"] = result_kind
    if window:
        report.update(start_row=start_row, stop_row=stop_row, window_excluded_rows=0,
                      repeated_id_scope="SELECTED_WINDOW_ONLY_NO_CROSS_WINDOW_COMPARISON",
                      scope="SUPPLIED_NATIVE_ROW_WINDOW_NEVER_SERVER_COMPLETENESS")
    output = io.StringIO(newline="")
    writer = None
    seen_ids = set()
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError("dataArray group must be an object")
        links = [key for key in LINKS if key in group]
        if len(links) != 1 or not isinstance(group[links[0]], str) or not group[links[0]]:
            raise ValueError("exactly one group navigationLink required")
        components, native = group.get("components"), group.get("dataArray")
        if (not isinstance(components, list) or not 1 <= len(components) <= MAX_COMPONENTS or
                any(not isinstance(key, str) or not key for key in components) or
                len(set(components)) != len(components) or not isinstance(native, list)):
            raise ValueError("bounded unique components and dataArray required")
        if any(not isinstance(row, list) or len(row) != len(components) for row in native):
            raise ValueError("dataArray row width does not match components")
        start = report["native_rows"]
        report["native_rows"] += len(native)
        if report["native_rows"] > MAX_NATIVE_ROWS:
            raise ValueError("native dataArray row bound exceeded")
        if links[0] != LINKS[0] or group[links[0]] != datastream_link:
            report["excluded_rows"] += len(native)
            report["excluded_groups"].append(dict(index=group_index, rows=len(native), link_kind=links[0]))
            continue
        report["selected_groups"] += 1
        if not all(key in components for key in REQUIRED) or "@iot.id" in components:
            raise ValueError("selected components require unambiguous id and observation fields")
        left = max(0, min(len(native), start_row - start)) if window else 0
        right = max(0, min(len(native), stop_row - start)) if window else len(native)
        selected = native[left:right]
        if window:
            report["window_excluded_rows"] += len(native) - len(selected)
            report["excluded_rows"] += len(native) - len(selected)
        if len(selected) > MAX_SELECTED_ROWS:
            raise ValueError("selected dataArray group bound exceeded")
        if not native:
            report["empty_selected_groups"].append(group_index)
            continue
        positions = {key: components.index(key) for key in REQUIRED}
        observations = []
        if not selected:
            continue
        for values in selected:
            item = {key: values[position] for key, position in positions.items()}
            item["@iot.id"] = item.pop("id")
            item["Datastream"] = selected_stream
            observations.append(item)
        projected, semantic_report = existing.convert_sensorthings(
            json.dumps({"value": observations}), property_definition, capture_time_us, result_kind=result_kind)
        # Internal adopted page <=16MiB, with one hex source field. Its parsing
        # bound is independent of the final output budget (including test seams).
        old_limit = csv.field_size_limit(32 * 1024 * 1024 + 4)
        try:
            for row_index, row in enumerate(csv.DictReader(io.StringIO(projected))):
                row_index += left
                identity = row["observation_id_hex"]
                repeated = identity in seen_ids
                seen_ids.add(identity)
                report["repeated_observation_ids"] += int(repeated)
                first = report["selected_rows"] == 0
                row.update(source_sha256=source_sha, source_index=start+row_index,
                           source_record_hex="hex:"+text.encode().hex() if first else "",
                           dataarray_metadata_sha256=metadata_sha,
                           dataarray_metadata_hex="hex:"+datastream_text.encode().hex() if first else "",
                           dataarray_group_index=group_index, dataarray_row_index=row_index,
                           dataarray_global_row_index=start+row_index,
                           dataarray_repeat_disposition="REPEATED_IN_DISTINCT_GROUP" if repeated else "FIRST_REPORTED_ID")
                if writer is None:
                    writer = csv.DictWriter(output, list(row), lineterminator="\n")
                    writer.writeheader()
                writer.writerow(row)
                report["selected_rows"] += 1
                if output.tell() > MAX_OUTPUT_BYTES:
                    raise ValueError("dataArray output bound exceeded")
        finally:
            csv.field_size_limit(old_limit)
        report["converted_values"] += semantic_report["converted_values"]
        report["withheld_values"] += semantic_report["withheld_values"]
    if not report["selected_groups"]:
        raise ValueError("selected Datastream link absent")
    if window and stop_row > report["native_rows"]:
        raise ValueError("native row window exceeds supplied response")
    report["output_sha256"] = digest(output.getvalue())
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--datastream-link", required=True)
    parser.add_argument("--observed-property", required=True)
    parser.add_argument("--capture-time-us", required=True, type=int)
    parser.add_argument("--result-kind", choices=("celsius", "count", "truth"), default="celsius")
    parser.add_argument("--start-row", type=int)
    parser.add_argument("--stop-row", type=int)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.report.exists() or args.output.resolve() == args.report.resolve():
            raise ValueError("output paths must be distinct and new")
        texts = []
        for path in (args.input, args.metadata):
            with path.open("rb") as source:
                raw = source.read(MAX_INPUT_BYTES+1)
            if len(raw) > MAX_INPUT_BYTES:
                raise ValueError("input bound exceeded")
            texts.append(raw.decode("utf-8"))
        output, report = convert(*texts, datastream_link=args.datastream_link,
                                 property_definition=args.observed_property, capture_time_us=args.capture_time_us,
                                 result_kind=args.result_kind, start_row=args.start_row, stop_row=args.stop_row)
        with args.output.open("x", encoding="utf-8", newline="") as target: target.write(output)
        with args.report.open("x", encoding="utf-8") as target: json.dump(report, target, indent=2)
        return 0
    except (ValueError, OSError, KeyError, TypeError, OverflowError, RecursionError) as exc:
        print(f"SensorThings dataArray rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
