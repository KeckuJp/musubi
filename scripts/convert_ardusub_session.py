"""Compose fixed saved Sub reports; never infer a vehicle or synchronize clocks."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_ardusub_vertical_reports as vertical
from scripts import convert_ardupilot_mode_csv as mode
from scripts import convert_ardupilot_battery_csv as battery

PIN = "30257f01185471ab4c1ac544e47d1b4437e44c98"
SOURCE_PINS = {PIN: "4.5.7", "80e8c23a7a07d0653fc1ce6b6a55ee17889c77af": "4.5.3"}
LIMIT = 16 * 1024**2
FIELDS = {
    "MODE": "TimeUS,Mode,ModeNum,Rsn".split(","),
    "BAT": "TimeUS,Inst,Volt,Curr,CurrTot,EnrgTot,RemPct,VoltR,Temp,Res,H,SH".split(","),
    "BCL": ["TimeUS", "Instance", "Volt", *[f"V{i}" for i in range(1, 13)]],
    "BCL2": ["TimeUS", "Instance", "V13", "V14"],
    "IMU": "TimeUS,I,GyrX,GyrY,GyrZ,AccX,AccY,AccZ".split(","),
}
PROFILES = {"CTUN": "ardusub-vertical/underwater-profile.toml",
    "MODE": "ardupilot-mode/vehicle-profile.toml", "IMU": "ardusub-session/imu-profile.toml",
    **{kind: "ardupilot-battery/unknown-profile.toml" for kind in ("BAT", "BCL", "BCL2")}}


def table(rows, fields):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def convert(raw, *, source_pin, session_id, single_boot_session, surface_anchor=None, qualify_session_domain=False):
    if type(qualify_session_domain) is not bool:
        raise ValueError("session domain option must be boolean")
    if source_pin not in SOURCE_PINS or single_boot_session is not True:
        raise ValueError("fixed Sub source and explicit single boot declaration required")
    version = SOURCE_PINS[source_pin]
    version_tag = version.replace(".", "")
    domain_basis = f"FIXED_SUB{version_tag}_SAME_REPORT_BUILD_NOT_PHYSICAL_IDENTITY"
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id.encode()) > 256:
        raise ValueError("bounded session label required")
    ctun, vertical_report = vertical.convert(raw, source_version=f"ardusub-{version}-ctun",
        underwater_position=True, surface_anchor=surface_anchor)
    document = json.loads(raw, object_pairs_hook=vertical.unique_object,
                          parse_constant=vertical.reject_constant)
    records = document["source_records"]
    groups = {kind: [] for kind in ("CTUN", *FIELDS)}
    unknown = {}
    previous = None
    for index, record in enumerate(records):
        kind, data = record["meta"]["type"], record["data"]
        if kind not in groups:
            unknown[kind] = unknown.get(kind, 0) + 1
            continue
        stamp = vertical.integer(data.get("TimeUS"), "TimeUS", 2**63 - 1)
        if previous is not None and stamp < previous:
            raise ValueError("selected records cross a boot regression; split explicitly")
        previous = stamp
        groups[kind].append((index, record))
    files, outputs = {}, []
    digest = vertical_report["source_document_sha256"]
    for kind, selected in groups.items():
        if not selected:
            continue
        if kind == "CTUN":
            text = ctun
        else:
            rows = []
            for _, record in selected:
                data = record["data"]
                row = {}
                for name in FIELDS[kind]:
                    value = data.get(name)
                    if (kind == "BAT" and name in ("Curr", "CurrTot", "EnrgTot", "VoltR", "Res")
                            and isinstance(value, str) and value.lower() == "nan"):
                        row[name] = value  # Existing BAT missing-value policy, not numeric zero.
                        continue
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise ValueError("selected numeric field missing or nonfinite")
                    row[name] = value
                if kind == "IMU":
                    vertical.integer(data["I"], "IMU.I", 255)
                rows.append(row)
            text = table(rows, FIELDS[kind])
            if kind == "MODE":
                text = mode.convert(text, "sub-" + version)
            elif kind != "IMU":
                text = battery.convert(text, {"BAT": "ardupilot-bat-inst",
                    "BCL": "ardupilot-bcl-4.3", "BCL2": "ardupilot-bcl2-4.3"}[kind],
                    bat_details=kind == "BAT")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if len(rows) != len(selected):
            raise ValueError("meaning output changed source record count")
        extra = ["session_report_sha256", "session_label_hex", "session_source_record_index",
                 "session_record_hex", "session_association_basis"]
        profile_path = "profiles/declared/" + PROFILES[kind]
        original_profile = profile_path
        if qualify_session_domain:
            original_domain = "Subsurface" if kind == "CTUN" else "Unknown"
            extra += ["session_original_domain", "session_original_profile", "session_domain_basis"]
            profile = (Path(__file__).resolve().parents[1] / profile_path).read_text()
            if kind != "CTUN":
                if "platform_domain" in profile:
                    raise ValueError("unexpected existing domain profile")
                profile = 'platform_domain = "subsurface"\n' + profile
            profile_path = kind + ".profile.toml"
            files[profile_path] = profile.encode()
        for row, (index, record) in zip(rows, selected):
            row.update(session_report_sha256=digest, session_label_hex="hex:" + session_id.encode().hex(),
                session_source_record_index=index, session_record_hex=vertical.retained_hex(record),
                session_association_basis="SAME_REPORT_FIXED_BUILD_CALLER_SINGLE_BOOT_NOT_AUTHENTICATED_OR_SYNCHRONIZED")
            if qualify_session_domain:
                row.update(session_original_domain=original_domain,
                    session_original_profile=original_profile,
                    session_domain_basis=domain_basis)
        encoded = table(rows, reader.fieldnames + extra).encode()
        if len(encoded) > LIMIT:
            raise ValueError("per-route output limit exceeded")
        filename = kind + ".csv"
        files[filename] = encoded
        outputs.append(dict(file=filename, profile=profile_path,
            reader_options=["--allow-equal-time"] + (["--preserve-nonfinite-as-text"] if kind == "BAT" else []),
            records=len(rows), sha256="sha256:" + hashlib.sha256(encoded).hexdigest()))
    if sum(map(len, files.values())) > 64 * 1024**2:
        raise ValueError("session output budget exceeded")
    missing = [kind for kind in ("CTUN", "MODE", "BAT", "BCL", "IMU") if not groups[kind]]
    report = dict(schema=f"saved-sub{version_tag}-session/v1", source_report_sha256=digest, source_pin=source_pin,
        session_id=session_id, source_records=len(records), selected_records=sum(map(len, groups.values())),
        selected_counts={key: len(value) for key, value in groups.items()}, unselected_types=unknown,
        not_selected_records=sum(unknown.values()), missing_required_families=missing, outputs=outputs,
        disposition="INCOMPLETE_REQUIRED_INPUT" if missing else "FINITE_FAMILIES_SUPPLIED_NOT_PHYSICAL_VALIDITY",
        vertical_report=vertical_report, domain_basis="CTUN_SUBSURFACE_OTHER_ROUTES_UNKNOWN_UNCHANGED",
        source_retention="retain unchanged source-report.json; session indices refer to it, not native BIN offsets",
        scope="No installed BlueROV2 proof; no carryforward, clock alignment or inferred water anchor")
    if qualify_session_domain:
        report["domain_basis"] = domain_basis
        report["generated_profiles"] = {name: "sha256:" + hashlib.sha256(data).hexdigest()
            for name, data in files.items() if name.endswith(".profile.toml")}
    return files, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-pin", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--single-boot-session", action="store_true")
    parser.add_argument("--surface-anchor", type=Path)
    parser.add_argument("--qualify-session-domain", action="store_true")
    args = parser.parse_args()
    try:
        with args.input.open("rb") as source:
            raw = source.read(LIMIT + 1)
        anchor = None
        if args.surface_anchor:
            with args.surface_anchor.open("rb") as source:
                anchor = source.read(65537)
        files, report = convert(raw, source_pin=args.source_pin, session_id=args.session_id,
            single_boot_session=args.single_boot_session, surface_anchor=anchor,
            qualify_session_domain=args.qualify_session_domain)
        args.output.mkdir()
        (args.output / "source-report.json").write_bytes(raw)
        if anchor is not None:
            (args.output / "surface-anchor.json").write_bytes(anchor)
        for name, data in files.items():
            (args.output / name).write_bytes(data)
        (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    except (ValueError, OSError, TypeError, KeyError):
        parser.exit(2, "saved Sub session refused; check source, selected fields and single-boot bounds\n")


if __name__ == "__main__":
    main()
