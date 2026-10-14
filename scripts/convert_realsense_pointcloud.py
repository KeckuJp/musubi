#!/usr/bin/env python3
"""Bind explicitly declared saved producer output; never run ROS or a camera."""
import argparse
import csv
import io
import json
import re
from pathlib import Path

try:
    from scripts.convert_rosbag_observations import convert, LIMIT
except ModuleNotFoundError:
    from convert_rosbag_observations import convert, LIMIT

WRAPPER = "bafc21080c5c8e259dadbb309797949aee0dd950"
SDK = "38a41441971387197193ad3aeae3cefe6a11f2cb"
BASIS = "DECLARED_REALSENSE_ROS_4_56_4_SDK_2_56_5_VERTICES_METRES"
OUTPUT_LIMIT = 128 * 1024 * 1024  # Same finite compact CSV bound as the reused reader.


def convert_saved(path, *, topic, camera_name, ordered, texture,
                  start_ns=None, stop_ns=None, point_window=None):
    if (not isinstance(camera_name, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", camera_name)
            or type(ordered) is not bool or texture not in ("none", "RGB8", "Y8")):
        raise ValueError("explicit finite camera name, ordered flag and texture required")
    output, report = convert(path, pointcloud_unit="m", pointcloud_compact=True,
                             topics=[topic], start_ns=start_ns, stop_ns=stop_ns,
                             point_window=point_window)
    binding = dict(wrapper_commit=WRAPPER, sdk_commit=SDK,
                   basis="CALLER_DECLARED_SOURCE_CONDITIONS_NOT_AUTHENTICATION",
                   camera_name=camera_name, topic=topic, ordered=ordered, texture=texture,
                   unit_basis=BASIS, axes="DECLARED_FRAME_UNTRANSFORMED",
                   texture_meaning="NOT_QUALIFIED_OR_CALIBRATED")
    sources = report["pointcloud_sources"]
    if not sources or report["unsupported_records"]:
        raise ValueError("selected topic must contain qualified PointCloud2 records")
    expected = [dict(name=n, offset=i * 4, datatype=7, count=1)
                for i, n in enumerate(("x", "y", "z"))]
    step = 16
    if texture != "none":
        expected.append(dict(name="rgb" if texture == "RGB8" else "intensity",
                             offset=16, datatype=7, count=1))
        step = 20
    for source in sources:
        record = source["decoded"]
        layout = [{key: field[key] for key in ("name", "offset", "datatype", "count")}
                  for field in record["fields"]]
        if (record["header"]["frame_id"] != camera_name + "_depth_optical_frame"
                or layout != expected or record["point_step"] != step
                or record["row_step"] != record["width"] * step
                or record["is_bigendian"] is not False
                or record["is_dense"] is not (not ordered)
                or (not ordered and record["height"] != 1)):
            raise ValueError("saved layout/frame disagrees with declared fixed producer")
    # Reuse numeric conversion, missingness and exact CDR binding without re-decoding.
    reader = csv.DictReader(io.StringIO(output))
    result = io.StringIO()
    writer = csv.DictWriter(result, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in reader:
        if (not ordered and row["point_status"] != "EMPTY_CLOUD"
                and float(row["point_z_m"]) <= 0):
            raise ValueError("unordered fixed producer omits nonpositive depth vertices")
        descriptor = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
        descriptor["producer_binding_ref"] = "report.json#/producer_binding"
        row["source_record_hex"] = "hex:" + json.dumps(descriptor, separators=(",", ":")).encode().hex()
        row["point_unit_basis"] = BASIS
        writer.writerow(row)
        if result.tell() > OUTPUT_LIMIT:
            raise ValueError("producer-bound output exceeds bound")
    report["producer_binding"] = binding
    if len(json.dumps(report, separators=(",", ":")).encode()) > LIMIT:
        raise ValueError("producer-bound report exceeds bound")
    output = result.getvalue()
    if len(output.encode()) > OUTPUT_LIMIT:
        raise ValueError("producer-bound output exceeds bound")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--camera-name", required=True)
    parser.add_argument("--layout", choices=("ordered", "unordered"), required=True)
    parser.add_argument("--texture", choices=("none", "RGB8", "Y8"), required=True)
    parser.add_argument("--start-ns", type=int)
    parser.add_argument("--stop-ns", type=int)
    parser.add_argument("--point-start", type=int)
    parser.add_argument("--point-stop", type=int)
    args = parser.parse_args()
    try:
        output, report = convert_saved(args.input, topic=args.topic,
            camera_name=args.camera_name, ordered=args.layout == "ordered",
            texture=args.texture, start_ns=args.start_ns, stop_ns=args.stop_ns,
            point_window=(args.point_start, args.point_stop)
                if args.point_start is not None or args.point_stop is not None else None)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(
            json.dumps(report, separators=(",", ":")), encoding="utf-8")
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
