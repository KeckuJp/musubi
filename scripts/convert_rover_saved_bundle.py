"""Finite Rover/Copter 4.5.5 saved-log composition; no vehicle, network or clock inference."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path

import export_ardupilot_records as exporter
import convert_ardupilot_mode_csv as mode
import convert_ardupilot_battery_csv as battery
import convert_recorded_angles_csv as angles
import convert_position_csv_batches as position

PIN = "9064e22695992593b12e71a2b2ad25faba2f7277"
COPTER_PIN = "142aece2da56146d94dcad0f6b539af6960538f6"
VEHICLES = {PIN: "rover-4.5.5", COPTER_PIN: "copter-4.5.5"}
ROOT = Path(__file__).resolve().parents[1]
LIMIT = 128 * 1024**2
LAYOUTS = {
    "GPS": ("QBBIHBcLLeffffB", "TimeUS,I,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,GCrs,VZ,Yaw,U"),
    "ATT": ("QccccCCCCB", "TimeUS,DesRoll,Roll,DesPitch,Pitch,DesYaw,Yaw,ErrRP,ErrYaw,AEKF"),
    "MODE": ("QMBB", "TimeUS,Mode,ModeNum,Rsn"),
    "ARM": ("QBIBB", "TimeUS,ArmState,ArmChecks,Forced,Method"),
    "PARM": ("QNff", "TimeUS,Name,Value,Default"),
    "VER": ("QBHBBBBIZHB", "TimeUS,BT,BST,Maj,Min,Pat,FWT,GH,FWS,APJ,BU"),
    "MSG": ("QZ", "TimeUS,Message"),
    "BAT": ("QBfffffcfBBB", "TimeUS,Inst,Volt,VoltR,Curr,CurrTot,EnrgTot,Temp,Res,RemPct,H,SH"),
    "BCL": ("QBfHHHHHHHHHHHH", "TimeUS,Instance,Volt,V1,V2,V3,V4,V5,V6,V7,V8,V9,V10,V11,V12"),
    "BCL2": ("QBHH", "TimeUS,Instance,V13,V14"),
}
PROFILES = {
    "MODE": "ardupilot-mode/vehicle-profile.toml", "ARM": "ardupilot-mode/vehicle-profile.toml",
    "PARM": "ardupilot-mode/parameter-profile.toml", "VER": "ardupilot-mode/vehicle-profile.toml",
    "BAT": "ardupilot-battery/unknown-profile.toml", "BCL": "ardupilot-battery/unknown-profile.toml",
    "BCL2": "ardupilot-battery/unknown-profile.toml", "ATT": "recorded-angles/profile.toml",
    "GPS": "rover-saved-bundle/profile.toml", "MSG": "rover-saved-bundle/profile.toml",
}


def table(rows, fields):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def convert(path, source_pin, *, record_window=None, allow_truncated_tail=False, qualify_surface=False,
            qualify_ground=False, qualify_air=False):
    if source_pin not in VEHICLES:
        raise ValueError("fixed Rover or Copter source declaration required")
    vehicle = VEHICLES[source_pin]
    if type(allow_truncated_tail) is not bool or (allow_truncated_tail and record_window is None):
        raise ValueError("tail policy requires explicit record window")
    exports, report = (exporter.export_records(path, LAYOUTS, include_source_indices=True)
        if record_window is None else exporter.export_window(path, LAYOUTS, *record_window,
                                                            allow_truncated_tail=allow_truncated_tail))
    flags = (qualify_surface, qualify_ground, qualify_air)
    if any(type(flag) is not bool for flag in flags) or sum(flags) > 1:
        raise ValueError("domain qualification must be one explicit boolean")
    if ((qualify_surface or qualify_ground) and vehicle != "rover-4.5.5") or (qualify_air and vehicle != "copter-4.5.5"):
        raise ValueError("domain qualification does not apply to the declared source")
    qualify = any(flags)
    domain_refs, frame_values, identity_refs, families = [], set(), [], set()
    if (qualify_surface or qualify_ground) and "PARM" in exports:
        if report["schemas"]["PARM"]["format"] != LAYOUTS["PARM"][0] or report["schemas"]["PARM"]["columns"] != LAYOUTS["PARM"][1].split(","):
            raise ValueError("record layout is not the fixed Rover writer")
        for row, ref in zip(csv.DictReader(io.StringIO(exports["PARM"])), report["selected_record_refs"]["PARM"]):
            if row["Name"] == "FRAME_CLASS":
                value = float(row["Value"])
                if not math.isfinite(value):
                    raise ValueError("nonfinite recorded FRAME_CLASS")
                frame_values.add(value)
                domain_refs.append(ref)
        if len(frame_values) > 1:
            raise ValueError("conflicting recorded FRAME_CLASS declarations")
    if (qualify_ground or qualify_air) and "VER" in exports:
        for row, ref in zip(csv.DictReader(io.StringIO(exports["VER"])), report["selected_record_refs"]["VER"]):
            match = mode.FIRMWARE_STRING.fullmatch(row["FWS"])
            families.add(match.group(1) if match else None)
            identity_refs.append(ref)
    if qualify_air and "MSG" in exports:
        for row in csv.DictReader(io.StringIO(exports["MSG"])):
            match = mode.FIRMWARE_STRING.match(row["Message"])
            if match and (match.group(1), match.group(2, 3, 4)) != ("ArduCopter", ("4", "5", "5")):
                raise ValueError("conflicting in-log firmware identity")
    surface = qualify_surface and frame_values == {2.0}
    ground = qualify_ground and frame_values == {1.0} and families == {"ArduRover"}
    air = qualify_air and families == {"ArduCopter"}
    basis = ("same_original_selected_FRAME_CLASS_2_not_authenticated" if surface
             else "same_original_selected_FRAME_CLASS_1_and_VER_ArduRover_not_authenticated" if ground
             else "same_original_selected_VER_ArduCopter_4.5.5_not_authenticated_not_airframe" if air
             else "no_selected_Rover_ground_evidence" if qualify_ground
             else "no_selected_ArduCopter_identity_evidence" if qualify_air else "no_selected_Boat_evidence")
    label = "surface" if surface else "ground" if ground else "air" if air else None
    files, outputs = {}, []
    for name, text in exports.items():
        schema = report["schemas"][name]
        fmt, fields = LAYOUTS[name]
        if schema["format"] != fmt or schema["columns"] != fields.split(","):
            raise ValueError("record layout is not the fixed Rover writer")
        if name == "VER":
            for row in csv.DictReader(io.StringIO(text)):
                if (row["Maj"], row["Min"], row["Pat"]) != ("4", "5", "5"):
                    raise ValueError("reported version contradicts fixed source declaration")
        if name in ("MODE", "ARM", "VER", "PARM"):
            result = mode.convert(text, vehicle,
                "parm-" + vehicle if name == "PARM" else name.lower(),
                preserve_nonfinite_default=name == "PARM")
        elif name in ("BAT", "BCL", "BCL2"):
            result = battery.convert(text, {"BAT": "ardupilot-bat-inst", "BCL": "ardupilot-bcl-4.3",
                "BCL2": "ardupilot-bcl2-4.3"}[name], bat_details=name == "BAT")
        elif name == "ATT":
            result = angles.convert(text, vehicle + "-decoded-att")
        else:
            result = text
        reader = csv.DictReader(io.StringIO(result, newline=""))
        header, rows = reader.fieldnames, list(reader)
        if name == "MSG":
            header = ["message_utf8_hex" if key == "Message" else key for key in header]
            for row in rows:
                row["message_utf8_hex"] = "hex:" + row.pop("Message").encode("utf-8").hex()
        refs = report["selected_record_refs"][name]
        if len(rows) != len(refs):
            raise ValueError("meaning converter changed record accounting")
        extra = ["original_sha256", "original_record_index", "original_offset", "original_length", "writer_source_pin"]
        if qualify:
            extra += ["recorded_domain_basis", "recorded_domain_parameter_indices"]
        for row, ref in zip(rows, refs):
            row.update(original_sha256=report["source_sha256"], original_record_index=ref["index"],
                       original_offset=ref["offset"], original_length=ref["length"], writer_source_pin=source_pin)
            if qualify:
                row.update(recorded_domain_basis=basis,
                    recorded_domain_parameter_indices=";".join(str(r["index"]) for r in domain_refs))
            if name == "GPS":
                for field in ("Spd", "GCrs", "VZ"):
                    if not math.isfinite(float(row[field])):
                        raise ValueError("nonfinite reported GPS motion")
        enriched = table(rows, header + extra)
        files[name + ".csv"] = enriched.encode()
        profile_path = "profiles/declared/" + (
            "copter-saved-bundle/profile.toml" if vehicle == "copter-4.5.5" and name in ("GPS", "MSG")
            else PROFILES[name])
        if label:
            profile = (ROOT / profile_path).read_text()
            if 'family = "unknown"' not in profile or "platform_domain" in profile:
                raise ValueError("unexpected original bundle domain profile")
            profile_path = name + ".profile.toml"
            files[profile_path] = (f'platform_domain = "{label}"\n' + profile).encode()
        outputs.append(dict(file=name + ".csv", records=len(rows), profile=profile_path))
        if name == "GPS":
            declared = json.loads((ROOT / "profiles/declared/mavlogdump-position-csv/profile.json").read_text())
            instances = {}
            for row in rows:
                if not row["I"].isdigit() or int(row["I"]) > 255:
                    raise ValueError("invalid reported GPS instance")
                instances.setdefault(int(row["I"]), []).append(row)
            for instance, selected in sorted(instances.items()):
                prefix = "GPS" if len(instances) == 1 else f"GPS-I{instance}"
                track = table(selected, header + extra).encode()
                for index, part in enumerate(position.convert_batches(track, declared)):
                    key = f"{prefix}-position-{index:06d}.jsonl"
                    files[key] = part
                    outputs.append(dict(file=key, records=part.count(b"\n"), gps_instance=instance,
                        position_adapter="profiles/declared/mavlogdump-position-csv/position.json",
                        duplicate_projection_of="GPS.csv"))
                    if label:
                        adapter = json.loads((ROOT / outputs[-1]["position_adapter"]).read_text())
                        if adapter["domain"] != "unknown":
                            raise ValueError("unexpected position adapter domain")
                        adapter["domain"] = label
                        files["GPS-position-adapter.json"] = (json.dumps(adapter, indent=2) + "\n").encode()
                        outputs[-1]["position_adapter"] = "GPS-position-adapter.json"
        if sum(map(len, files.values())) > LIMIT:
            raise ValueError("bundle output budget exceeded")
    required = {"position_motion": ["GPS"], "attitude": ["ATT"],
                "mode": ["MODE"], "arming": ["ARM"], "parameters": ["PARM"],
                "battery": ["BAT"], "reported_build_identity": ["VER"]}
    missing = [role for role, names in required.items() if not all(report["exported_counts"].get(n) for n in names)]
    for item in outputs:
        item["sha256"] = hashlib.sha256(files[item["file"]]).hexdigest()
    report.update(bundle_schema=vehicle.split("-")[0] + "-basic-saved/v1", declared_source_pin=source_pin, outputs=outputs,
        missing_required_roles=missing,
        disposition="INCOMPLETE_REQUIRED_INPUT" if missing else "REQUIRED_RECORD_FAMILIES_SUPPLIED_NOT_PHYSICAL_VALIDITY",
        association_basis="same original bytes; no independent identity or inferred clock synchronization",
        domain="UNKNOWN; source pin is caller applicability declaration, not authenticated hardware",
        source_retention="retain original binary and complete report; projections are not the original",
        repeated_projection_accounting="GPS position JSONL and GPS CSV project the same records; do not add their counts")
    if qualify:
        report.update(domain=label.capitalize() if label else "Unknown", domain_basis=basis,
            domain_parameter_refs=domain_refs, domain_parameter_values=sorted(frame_values),
            domain_scope="selected same-original declarations only; no outside-window or physical identity inference",
            generated_declarations={name: hashlib.sha256(data).hexdigest() for name, data in files.items()
                if name.endswith(".profile.toml") or name == "GPS-position-adapter.json"})
    if qualify_ground or qualify_air:
        report.update(domain_identity_refs=identity_refs,
                      domain_identity_families=sorted(families, key=lambda family: family or ""))
    return files, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-pin", required=True)
    parser.add_argument("--start-record", type=int)
    parser.add_argument("--stop-record", type=int)
    parser.add_argument("--allow-truncated-tail", action="store_true")
    parser.add_argument("--qualify-surface", action="store_true")
    parser.add_argument("--qualify-ground", action="store_true")
    parser.add_argument("--qualify-air", action="store_true")
    args = parser.parse_args()
    try:
        if (args.start_record is None) != (args.stop_record is None):
            raise ValueError("both interval bounds required")
        files, report = convert(args.input, args.source_pin, record_window=None if args.start_record is None
                               else (args.start_record, args.stop_record), allow_truncated_tail=args.allow_truncated_tail,
                               qualify_surface=args.qualify_surface, qualify_ground=args.qualify_ground,
                               qualify_air=args.qualify_air)
        args.output.mkdir()
        for name, data in files.items():
            with (args.output / name).open("xb") as stream:
                stream.write(data)
        with (args.output / "report.json").open("x") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (ValueError, OSError, TypeError, KeyError):
        parser.exit(1, "saved ArduPilot bundle refused; check fixed source, layout and bounds\n")


if __name__ == "__main__":
    main()
