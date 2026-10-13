"""Select saved rosbag2 v9 file-zstd SQLite messages; no acquisition or ROS runtime."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_rosbag_observations as ros

MAX_METADATA_BYTES = 256000
MAX_MEMBERS = 64
MAX_COMPRESSED_BYTES = 64 * 1024**2
MAX_EXPANDED_BYTES = 2 * 1024**3
MAX_WINDOW_BYTES = 16 * 1024**2
MAX_REPORT_BYTES = 16 * 1024**2
CHUNK_BYTES = 1024**2


def safe_name(name):
    if (not isinstance(name, str) or not name or len(name) > 1024
            or "\\" in name or any(ord(c) < 32 for c in name)
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError("unsupported declared member path")
    return name.split("/")


def read_regular(root_fd, name, limit):
    """Open each component relative to the held directory, never following links."""
    parts = safe_name(name)
    directory = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ValueError("source regular-file byte bound")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                   value.st_mtime_ns, value.st_ctime_ns)
        if len(data) > limit or len(data) != before.st_size or signature(before) != signature(after):
            raise ValueError("source changed or exceeds byte bound")
        return data
    finally:
        os.close(directory)


def convert(path, *, start_ns, stop_ns, topics=None, pointcloud_unit=None,
            pointcloud_compact=False, scan_compact=False, imu_reported_covariance=False,
            allow_missing_voltage=False, joint_kinds=None, stamped_motion_role=None,
            list_compact=None, allow_missing_joint_values=False,
            scan_unavailable_infinity=False, interface_kinds=None, legacy_schema=None,
            nav2_through_poses=False, outdoornav_control_state=False, autoware_gear=False,
            tf_relation=False, husarion_estop=False, husarion_model=False):
    if type(allow_missing_voltage) is not bool or (allow_missing_voltage and
            (pointcloud_compact or scan_compact)):
        raise ValueError("missing voltage requires noncompact battery output")
    if type(imu_reported_covariance) is not bool or (imu_reported_covariance and
            (pointcloud_compact or scan_compact)):
        raise ValueError("IMU covariance requires noncompact qualified ROS2 messages")
    if (type(pointcloud_compact) is not bool or type(scan_compact) is not bool
            or pointcloud_compact and (pointcloud_unit != "m" or scan_compact)
            or not pointcloud_compact and pointcloud_unit is not None):
        raise ValueError("select compact scan or compact cloud with explicit metre units")
    if (type(start_ns) is not int or type(stop_ns) is not int
            or not 0 <= start_ns < stop_ns <= 2**63 - 1):
        raise ValueError("explicit nonempty nanosecond interval required")
    if topics is not None and (not isinstance(topics, (list, tuple)) or not 1 <= len(topics) <= 32
            or any(not isinstance(t, str) or not t or len(t) > 256
                   or any(ord(c) < 32 for c in t) for t in topics)
            or len(set(topics)) != len(topics)):
        raise ValueError("one to 32 unique exact topics required")
    root_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata_raw = read_regular(root_fd, "metadata.yaml", MAX_METADATA_BYTES)
        metadata = ros.YAML(typ="safe").load(metadata_raw.decode("utf-8"))
        info = metadata.get("rosbag2_bagfile_information") if isinstance(metadata, dict) else None
        if (not isinstance(info, dict) or type(info.get("version")) is not int or info["version"] != 9
                or info.get("storage_identifier") != "sqlite3"
                or info.get("compression_mode") != "file" or info.get("compression_format") != "zstd"):
            raise ValueError("metadata v9 sqlite3 file-zstd required")
        names = info.get("relative_file_paths")
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_MEMBERS:
            raise ValueError("declared member count bound")
        for name in names:
            safe_name(name)
            if not name.endswith(".db3.zstd"):
                raise ValueError("declared sqlite3 zstd member required")
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("duplicate or case-colliding declared member")
        if "files" in info:
            files = info["files"]
            if (not isinstance(files, list) or len(files) != len(names)
                    or any(not isinstance(item, dict) or not isinstance(item.get("path"), str) for item in files)
                    or sorted(item["path"] for item in files) != sorted(names)):
                raise ValueError("metadata files must match declared members exactly")
        members, compressed_total, expanded_total = [], 0, 0
        with tempfile.TemporaryDirectory(prefix="musubi-ros-zstd-window-") as directory:
            target = Path(directory)
            for name in names:
                compressed = read_regular(root_fd, name, MAX_COMPRESSED_BYTES - compressed_total)
                compressed_total += len(compressed)
                parameters = ros.zstandard.get_frame_parameters(compressed)
                ros.check_single_zstd_frame(compressed, parameters.has_checksum)
                known = parameters.content_size not in (ros.zstandard.CONTENTSIZE_UNKNOWN,
                                                        ros.zstandard.CONTENTSIZE_ERROR)
                if (parameters.window_size > MAX_WINDOW_BYTES
                        or known and parameters.content_size > MAX_EXPANDED_BYTES - expanded_total):
                    raise ValueError("zstd expansion or window bound")
                destination = target / name[:-5]
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with destination.open("xb") as output, ros.zstandard.ZstdDecompressor().stream_reader(
                        io.BytesIO(compressed)) as decoder:
                    while True:
                        part = decoder.read(min(CHUNK_BYTES, MAX_EXPANDED_BYTES - expanded_total + 1))
                        if not part:
                            break
                        expanded_total += len(part)
                        if expanded_total > MAX_EXPANDED_BYTES:
                            raise ValueError("zstd expanded storage byte bound")
                        output.write(part)
                        digest.update(part)
                        size += len(part)
                if known and size != parameters.content_size:
                    raise ValueError("zstd content size mismatch")
                members.append(dict(source_path=name, compressed_bytes=len(compressed),
                    compressed_sha256=hashlib.sha256(compressed).hexdigest(),
                    expanded_path=name[:-5], expanded_bytes=size, expanded_sha256=digest.hexdigest()))
            rewritten = dict(info, relative_file_paths=[name[:-5] for name in names],
                             compression_mode="", compression_format="")
            if "files" in info:
                rewritten["files"] = [dict(item, path=item["path"][:-5]) for item in info["files"]]
            with (target / "metadata.yaml").open("w", encoding="utf-8") as output:
                ros.YAML(typ="safe").dump(dict(metadata, rosbag2_bagfile_information=rewritten), output)
            output, report = ros.convert(target, start_ns=start_ns, stop_ns=stop_ns, topics=topics,
                pointcloud_unit=pointcloud_unit, pointcloud_compact=pointcloud_compact,
                scan_compact=scan_compact, imu_reported_covariance=imu_reported_covariance,
                allow_missing_voltage=allow_missing_voltage, joint_kinds=joint_kinds,
                stamped_motion_role=stamped_motion_role, list_compact=list_compact,
                allow_missing_joint_values=allow_missing_joint_values,
                scan_unavailable_infinity=scan_unavailable_infinity,
                interface_kinds=interface_kinds, legacy_schema=legacy_schema,
                nav2_through_poses=nav2_through_poses,
                outdoornav_control_state=outdoornav_control_state, autoware_gear=autoware_gear,
                tf_relation=tf_relation, husarion_estop=husarion_estop,
                husarion_model=husarion_model)
    finally:
        os.close(root_fd)
    report["file_zstd_window"] = dict(
        source_metadata_sha256=hashlib.sha256(metadata_raw).hexdigest(),
        source_metadata_yaml_hex="hex:" + metadata_raw.hex(), members=members,
        compressed_bytes=compressed_total, expanded_bytes=expanded_total,
        output_sha256=hashlib.sha256(output.encode()).hexdigest(),
        undeclared_directory_entries="NOT_ENUMERATED_OR_READ",
        outside_selection="STORAGE_EXPANDED; CDR_NOT_DECODED_OR_SEMANTICALLY_VALIDATED",
        source_binding="paired original metadata/member digests and exact CSV; not authentication or a new recording",
        bounds=dict(metadata_bytes=MAX_METADATA_BYTES, members=MAX_MEMBERS,
            compressed_bytes=MAX_COMPRESSED_BYTES, expanded_bytes=MAX_EXPANDED_BYTES,
            window_bytes=MAX_WINDOW_BYTES, report_bytes=MAX_REPORT_BYTES))
    if len(json.dumps(report, indent=2).encode()) > MAX_REPORT_BYTES:
        raise ValueError("complete pretty JSON report byte bound")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--start-ns", type=int, required=True)
    parser.add_argument("--stop-ns", type=int, required=True)
    parser.add_argument("--topic", action="append")
    parser.add_argument("--pointcloud-unit", choices=("m",))
    parser.add_argument("--pointcloud-compact", action="store_true")
    parser.add_argument("--scan-compact", action="store_true")
    parser.add_argument("--imu-reported-covariance", action="store_true")
    parser.add_argument("--allow-missing-voltage", action="store_true")
    parser.add_argument("--joint-kind", action="append", default=[])
    parser.add_argument("--interface-kind", action="append", default=[])
    parser.add_argument("--stamped-motion-role", choices=("reported_estimate", "reported_reference"))
    parser.add_argument("--list-compact", choices=("actions", "parameters"))
    parser.add_argument("--legacy-schema", choices=tuple(sorted(ros.LEGACY_SCHEMA_CHOICES)))
    for option in ("allow-missing-joint-values", "scan-unavailable-infinity",
                   "nav2-through-poses", "outdoornav-control-state", "autoware-gear", "tf-relation",
                   "husarion-estop", "husarion-model"):
        parser.add_argument("--" + option, action="store_true")
    args = parser.parse_args()
    try:
        if args.output_directory.exists():
            raise ValueError("output directory already exists")
        mappings = []
        for entries in (args.joint_kind, args.interface_kind):
            mapping = {}
            for entry in entries:
                key, value = entry.rsplit("=", 1)
                if key in mapping:
                    raise ValueError("duplicate explicit meaning configuration")
                mapping[key] = value
            mappings.append(mapping)
        output, report = convert(args.input, start_ns=args.start_ns, stop_ns=args.stop_ns,
            topics=args.topic, pointcloud_unit=args.pointcloud_unit,
            pointcloud_compact=args.pointcloud_compact, scan_compact=args.scan_compact,
            imu_reported_covariance=args.imu_reported_covariance,
            allow_missing_voltage=args.allow_missing_voltage, joint_kinds=mappings[0],
            interface_kinds=mappings[1], stamped_motion_role=args.stamped_motion_role,
            list_compact=args.list_compact, legacy_schema=args.legacy_schema,
            allow_missing_joint_values=args.allow_missing_joint_values,
            scan_unavailable_infinity=args.scan_unavailable_infinity,
            nav2_through_poses=args.nav2_through_poses,
            outdoornav_control_state=args.outdoornav_control_state, autoware_gear=args.autoware_gear,
            tf_relation=args.tf_relation, husarion_estop=args.husarion_estop,
            husarion_model=args.husarion_model)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        profile = ros.qualified_ground_profile(report) if args.husarion_model else None
        if profile is not None:
            (args.output_directory / "ground.profile.toml").write_bytes(profile)
    except Exception:
        parser.exit(2, "Saved file-zstd conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
