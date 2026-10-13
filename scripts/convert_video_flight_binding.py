"""Saved ffprobe8.1 frames to a caller-declared Plane4.3.1 flight clock.

Library entry: convert(export_bytes, flight_csv_bytes, declaration_bytes) returns
(frame_csv, mandatory_report, frame_profile, unchanged_flight_csv, flight_profile).
Retain both original inputs and declaration beside those outputs. Never opens
media, fetches data, estimates synchronization, or interpolates flight samples.
"""
import csv
from fractions import Fraction
import io
import json
from pathlib import Path

from scripts import convert_video_frames as video
from scripts.convert_image_flight_binding import RELATION, digest, integer, label
from scripts.convert_recorded_angles_csv import convert as angles

ROOT = Path(__file__).resolve().parents[1]
LABELS = ("session_id", "camera_id", "flight_entity_id", "media_clock_id", "flight_clock_id", "citation")
KEYS = {"schema", "export_sha256", "flight_sha256", "relation", "stream_index",
        "anchor_frame_index", "anchor_flight_index", "valid_start_pts", "valid_end_pts", "max_abs_delta_ticks", *LABELS}
UNITS = {
    "media_source_time_us": "floor_reported_PTS_times_timebase_us_not_exact_alignment",
    "flight_connection_status": "caller_declared_clock_connection_or_explicit_withheld",
    "derived_flight_time_us": "exact_us_on_declared_flight_clock_not_measured_sync",
    "flight_exact_sample_index": "one_based_unique_exact_flight_row_not_pose_at_frame",
    "flight_roll_rad": "rad_reported_at_matching_row_not_interpolated",
    "flight_pitch_rad": "rad_reported_at_matching_row_not_interpolated",
    "flight_yaw_rad": "rad_reported_at_matching_row_not_interpolated",
    "source_export_sha256": "sha256_supplied_export_bytes_not_media_authentication",
    "flight_source_sha256": "sha256_supplied_Plane_decoded_ATT_bytes",
    "clock_declaration_sha256": "sha256_supplied_clock_declaration_bytes",
    "flight_relation": "caller_assertion_not_verified",
    **{name + "_hex": "utf8_hex_caller_declared_" + name for name in LABELS},
}
# Opt-in only (rational_clock=True); the same exact-rational/floor pattern as convert_rtp_sender_clock.
RATIONAL_PRECISION = "EXACT_RATIONAL_AUTHORITATIVE_FLOOR_US_QUANTIZED_NOT_A_TIMESTAMP"
RATIONAL_UNITS = {
    "derived_flight_time_us_exact": "ratio_us_on_declared_flight_clock_authoritative_not_measured_sync",
    "derived_flight_time_us_floor": "floor_of_exact_ratio_quantized_not_timestamp_never_sample_matched",
    "derived_flight_time_precision": "constant_precision_basis_label",
}


def convert(export, flight, declaration, *, rational_clock=False):
    """No implicit read/write. Caller must save mandatory report and original bytes.

    rational_clock=True connects submicrosecond mappings by exact ratio instead of withholding
    them; such a frame never receives a flight sample, since flight rows are whole microseconds.
    """
    if type(rational_clock) is not bool:
        raise ValueError("explicit rational_clock boolean required")
    if any(type(raw) is not bytes for raw in (export, flight, declaration)):
        raise ValueError("exact input bytes required")
    if len(export) > 32*1024**2 or len(flight) > 16*1024**2 or len(declaration) > 16*1024:
        raise ValueError("input exceeds finite bound")
    declared = json.loads(declaration.decode(), object_pairs_hook=video.unique_object,
                          parse_constant=video.invalid_constant)
    if not isinstance(declared,dict) or set(declared) != KEYS or declared["schema"] != "video-flight-clock-v1" or declared["relation"] != RELATION:
        raise ValueError("closed explicit video-flight declaration required")
    if declared["export_sha256"] != digest(export) or declared["flight_sha256"] != digest(flight):
        raise ValueError("original digest mismatch")
    labels = {name+"_hex": label(declared[name]) for name in LABELS}
    for key in ("stream_index", "anchor_frame_index", "valid_start_pts", "valid_end_pts"):
        integer(declared[key])
    integer(declared["anchor_flight_index"],1)
    integer(declared["max_abs_delta_ticks"],1)
    start, stop = declared["valid_start_pts"], declared["valid_end_pts"]
    if start > stop:
        raise ValueError("invalid validity interval")
    text, report = video.convert(export.decode(), declared["stream_index"], compact=True)
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    flight_csv = angles(flight.decode(), "plane-4.3.1-decoded-att")
    states = list(csv.DictReader(io.StringIO(flight_csv)))
    anchors = [r for r in rows if int(r["source_frame_index"]) == declared["anchor_frame_index"]]
    af = declared["anchor_flight_index"]
    if len(anchors) != 1 or af > len(states):
        raise ValueError("selected timed frame/flight anchor absent")
    anchor = anchors[0]
    anchor_pts = int(anchor["presentation_timestamp_ticks"])
    if not start <= anchor_pts <= stop:
        raise ValueError("anchor outside validity")
    flight_anchor = int(states[af-1]["record_time_us"])
    samples = {}
    for index,state in enumerate(states,1):
        samples.setdefault(int(state["record_time_us"]),[]).append((index,state))
    lower,upper = min(samples),max(samples)
    common = dict(source_export_sha256=digest(export),flight_source_sha256=digest(flight),
        clock_declaration_sha256=digest(declaration),flight_relation=RELATION,**labels)
    counts = dict(connected_frames=0,withheld_frames=report["untimed_video_frames"],exact_sample_frames=0,
        statuses={"WITHHELD_MISSING_PTS": report["untimed_video_frames"]},declaration=declared,
        anchor_pts=anchor_pts,anchor_flight_time_us=flight_anchor,flight_rows=len(states),
        unselected_frames=report["unsupported_frames"],basis=RELATION)
    for row in rows:
        row.update(common)
        row["media_source_time_us"] = row["record_time_us"]
        row["record_time_us"] = "0"  # association order, original clock remains explicit
        pts = int(row["presentation_timestamp_ticks"])
        delta = pts-anchor_pts
        mapped = Fraction(flight_anchor) + Fraction(delta*int(row["timebase_numerator"])*1000000,int(row["timebase_denominator"]))
        if not start <= pts <= stop or abs(delta) > declared["max_abs_delta_ticks"]:
            status = "WITHHELD_DECLARED_INTERVAL"
        elif mapped.denominator != 1 and not rational_clock:
            status = "WITHHELD_SUBMICROSECOND_MAPPING"
        elif not 0 <= mapped < 2**63 or not lower <= mapped <= upper:
            status = "WITHHELD_FLIGHT_RANGE"
        elif mapped.denominator != 1:
            # Whole-microsecond flight rows cannot equal a fractional time; the floor is not matched.
            status = "CLOCK_CONNECTED_SUBMICROSECOND_NO_EXACT_SAMPLE"
        else:
            row["derived_flight_time_us"] = int(mapped)
            exact = samples.get(int(mapped),[])
            status = "CLOCK_CONNECTED_NO_EXACT_SAMPLE"
            if len(exact) > 1:
                status = "CLOCK_CONNECTED_AMBIGUOUS_SAMPLE"
            elif len(exact) == 1:
                status = "CLOCK_CONNECTED_EXACT_SAMPLE"
                index,state = exact[0]
                row["flight_exact_sample_index"] = index
                for axis in ("roll","pitch","yaw"):
                    row["flight_"+axis+"_rad"] = state["reported_"+axis+"_rad"]
                counts["exact_sample_frames"] += 1
        if rational_clock and status.startswith("CLOCK_CONNECTED"):
            row.update(derived_flight_time_us_exact=f"ratio:{mapped.numerator}/{mapped.denominator}",
                       derived_flight_time_us_floor=mapped.numerator // mapped.denominator,
                       derived_flight_time_precision=RATIONAL_PRECISION)
        row["flight_connection_status"] = status
        counts["withheld_frames" if status.startswith("WITHHELD") else "connected_frames"] += 1
        counts["statuses"][status] = counts["statuses"].get(status,0)+1
    counts["all_selected_frames_connected"] = counts["withheld_frames"] == 0
    assert counts["connected_frames"]+counts["withheld_frames"] == report["selected_video_frames"]
    output = io.StringIO()
    units = {**UNITS, **RATIONAL_UNITS} if rational_clock else UNITS
    if rational_clock:
        counts.update(rational_clock=True, precision=RATIONAL_PRECISION)
    writer = csv.DictWriter(output,fieldnames=reader.fieldnames+list(units))
    writer.writeheader()
    writer.writerows(rows)
    text = output.getvalue()
    if len(text.encode()) > 128*1024**2:
        raise ValueError("output exceeds bound")
    report.update(flight_binding=counts,source_export_sha256=digest(export),flight_source_sha256=digest(flight),
        csv_sha256=digest(text.encode()),flight_csv_sha256=digest(flight_csv.encode()),
        clock_declaration_sha256=digest(declaration),primary_clock="Unknown; association order only")
    profile = (ROOT/"profiles/declared/video-frame-export/profile.toml").read_text()
    profile = profile.replace('decoded_video_frame_export_v1',
        'video_flight_declared_rational_v1' if rational_clock else 'video_flight_declared_v1').replace(
        'floor_PTS_times_timebase_in_microseconds_not_UTC','constant_zero_association_order_not_capture_or_media_time')
    profile += "\n"+"\n".join(f'{key} = "{unit}"' for key,unit in units.items())+"\n"
    return text,report,profile,flight_csv,(ROOT/"profiles/declared/recorded-angles/profile.toml").read_text()
