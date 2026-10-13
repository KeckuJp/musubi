"""Bounded GPX1.1 declared points to shared CSV events; no navigation or fetching."""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET
import zipfile

try:
    from scripts.recorded_mission_parameters import FIELDS, DEFINITIONS, interpret
    from scripts.convert_ardupilot_battery_csv import unique_object
except ModuleNotFoundError:
    from recorded_mission_parameters import FIELDS, DEFINITIONS, interpret
    from convert_ardupilot_battery_csv import unique_object

DEERE_WGS84 = 'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'

NS = "{http://www.topografix.com/GPX/1/1}"
DEERE_NS = "{urn:musubi:deere-point-v1}"
DEERE_SECTION_BASIS = "SOURCE_DECLARED_SECTIONID_N9_0_NOT_GLOBAL_OR_PHYSICAL_IDENTITY"
INPUT_LIMIT = 64 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
META = {"name", "cmt", "desc", "src", "link", "number", "type", "extensions"}
POINT = {"ele", "time", "magvar", "geoidheight", "name", "cmt", "desc", "src", "link",
         "sym", "type", "fix", "sat", "hdop", "vdop", "pdop", "ageofdgpsdata", "dgpsid", "extensions"}


def decimal_value(text, low=None, high=None, exclusive_high=False, scientific=False):
    pattern = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)" + (r"(?:[Ee][+-]?[0-9]{1,3})?" if scientific else "")
    if not isinstance(text, str) or len(text) > 128 or not re.fullmatch(pattern, text.strip()):
        raise ValueError("finite decimal quantity required")
    value = Decimal(text)
    if ((low is not None and value < low) or (high is not None and
            (value >= high if exclusive_high else value > high)) or not math.isfinite(float(value))
            or (value != 0 and float(value) == 0)):
        raise ValueError("quantity outside selected range")
    return float(value)


def utc_microseconds(text):
    match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?)(Z|[+-][0-9]{2}:[0-9]{2})", text or "")
    if not match:
        raise ValueError("explicit UTC point time with at most microseconds required")
    offset = match[2]
    if offset != "Z" and (int(offset[1:3]) > 14 or int(offset[4:]) > 59
                           or (int(offset[1:3]) == 14 and int(offset[4:]) != 0)):
        raise ValueError("invalid UTC offset")
    # The accepted grammar above allows one to six fractional digits, but `fromisoformat` only
    # took exactly three or six before Python 3.11. Padding here keeps a legal `…:03.5Z` meaning
    # the same on every interpreter, instead of raising an opaque parser error on one of them.
    stamp = match[1]
    if "." in stamp:
        whole, fraction = stamp.split(".")
        stamp = whole + "." + fraction.ljust(6, "0")
    value = datetime.fromisoformat(stamp + ("+00:00" if offset == "Z" else offset))
    value = value.astimezone(timezone.utc)
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    if micros < 0:
        raise ValueError("point time before selected epoch")
    return micros


def children(node, allowed):
    seen = set()
    for child in node:
        name = child.tag.removeprefix(NS) if child.tag.startswith(NS) else ""
        if name not in allowed or (name in seen and name not in ("wpt", "rte", "trk", "rtept", "trkseg", "trkpt", "link")):
            raise ValueError("unknown layout or duplicate singleton; use extensions for custom data")
        seen.add(name)
    return list(node)


def convert(raw, capture_time_us, *, deere_projection=False):
    if not isinstance(raw, bytes) or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("bounded UTF8 GPX required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    text = raw.decode("utf-8-sig")
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("DTD/entity declarations unsupported")
    root = ET.fromstring(text)
    if root.tag != NS + "gpx" or root.get("version") != "1.1" or not root.get("creator"):
        raise ValueError("GPX1.1 namespace/version/creator required")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    extra_columns = ["deere_section_id", "deere_section_id_basis"] if deere_projection else []
    writer.writerow(["record_time_us", "point_index", "point_role", "series_index", "segment_index",
        "point_in_segment", "latitude_deg", "longitude_deg", "coordinate_basis", "elevation_m",
        "elevation_basis", "point_time_unix_us", "point_time_text_hex", "point_name_hex", "source_sha256"] + extra_columns)
    report = dict(points=0, timed_points=0, untimed_points=0, routes=0, tracks=0, segments=0,
        empty_routes=0, empty_tracks=0, empty_segments=0, waypoints=0,
        source_sha256=digest, source_retention="unchanged source.gpx, retained with CSV and report",
        unconverted_content="other metadata, point fields and extensions retained in source.gpx, not semantically decoded",
        numeric_precision="finite f64 output; original decimal text retained in source.gpx",
        clock="CALLER_CAPTURE_NOT_POINT_OR_DEVICE_TIME", evidence="declared-points-not-physical-certification")
    if deere_projection:
        report.update(deere_section_ids_present=0, deere_section_ids_missing=0,
            selected_extension="Deere Point v1 SECTIONID only; all other extensions remain unconverted")

    def emit(point, role, series, segment, number):
        children(point, POINT)
        lat = decimal_value(point.get("lat"), -90, 90)
        lon = decimal_value(point.get("lon"), -180, 180, True)
        ele, stamp, name = (point.find(NS + key) for key in ("ele", "time", "name"))
        for leaf in (ele, stamp, name):
            if leaf is not None and len(leaf):
                raise ValueError("nested scalar point field")
        elevation = "" if ele is None else decimal_value(ele.text)
        time = "" if stamp is None else utc_microseconds(stamp.text)
        section = []
        if deere_projection:
            extensions = point.find(NS + "extensions")
            if (extensions is None or len(extensions) != 1
                    or extensions[0].tag != DEERE_NS + "section_id"):
                raise ValueError("selected Deere projection requires one scoped SECTIONID declaration")
            declaration = extensions[0]
            basis = declaration.get("basis")
            value = declaration.text or ""
            if len(declaration) or set(declaration.attrib) != {"basis"}:
                raise ValueError("invalid projected SECTIONID declaration")
            if basis == DEERE_SECTION_BASIS:
                if not re.fullmatch(r"[+-]?[0-9]{1,9}", value) or len(value) > 9:
                    raise ValueError("exact projected N9/0 SECTIONID required")
                value = int(value)
            elif basis not in ("NOT_DECLARED", "MISSING_SOURCE_VALUE") or value:
                raise ValueError("invalid projected SECTIONID missing basis")
            section = [value, basis]
            report["deere_section_ids_missing" if value == "" else "deere_section_ids_present"] += 1
        writer.writerow([capture_time_us, report["points"], role, series, segment, number,
            lat, lon, "WGS84_DECIMAL_DEGREES", elevation, "UNRESOLVED_NOT_ASSUMED_MSL_OR_ELLIPSOID",
            time, "" if stamp is None else "hex:" + stamp.text.encode().hex(),
            "" if name is None else "hex:" + (name.text or "").encode().hex(), digest] + section)
        report["points"] += 1
        report["untimed_points" if stamp is None else "timed_points"] += 1
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("CSV exceeds bound")

    for node in children(root, {"metadata", "wpt", "rte", "trk", "extensions"}):
        if node.tag == NS + "wpt":
            emit(node, "WAYPOINT_DECLARED", -1, -1, report["waypoints"])
            report["waypoints"] += 1
        elif node.tag == NS + "rte":
            points = [x for x in children(node, META | {"rtept"}) if x.tag == NS + "rtept"]
            for number, point in enumerate(points):
                emit(point, "ROUTE_DECLARED_NOT_EXECUTED", report["routes"], -1, number)
            report["routes"] += 1
            report["empty_routes"] += not points
        elif node.tag == NS + "trk":
            segments = [x for x in children(node, META | {"trkseg"}) if x.tag == NS + "trkseg"]
            track_points = 0
            for segment_index, segment in enumerate(segments):
                points = [x for x in children(segment, {"trkpt", "extensions"}) if x.tag == NS + "trkpt"]
                for number, point in enumerate(points):
                    emit(point, "TRACK_REPORTED_NOT_PHYSICALLY_VERIFIED", report["tracks"], segment_index, number)
                track_points += len(points)
                report["segments"] += 1
                report["empty_segments"] += not points
            report["tracks"] += 1
            report["empty_tracks"] += not track_points
    if not report["points"]:
        raise ValueError("no declared points")
    return output.getvalue(), report


def convert_kmz(raw, capture_time_us, *, track_angles=False, feature_geometry=False):
    """Selected saved geometry declarations in a ZIP; never extract or fetch resources."""
    if not isinstance(raw, bytes) or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("bounded KMZ required")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".kml")]
        if len(members) != 1 or members[0].flag_bits & 1 or members[0].file_size > INPUT_LIMIT:
            raise ValueError("one bounded unencrypted KML member required")
        with archive.open(members[0]) as source:
            text = source.read(INPUT_LIMIT + 1).decode("utf-8-sig")
    return convert_kml_document(text, capture_time_us, raw, "source.kmz", track_angles=track_angles, feature_geometry=feature_geometry)


def convert_kml(raw, capture_time_us, *, track_angles=False, feature_geometry=False):
    """The same selected geometry declarations supplied as a bare saved KML2.2 document."""
    if not isinstance(raw, bytes) or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("bounded KML required")
    return convert_kml_document(raw.decode("utf-8-sig"), capture_time_us, raw, "source.kml", track_angles=track_angles, feature_geometry=feature_geometry)


def convert_kml_document(text, capture_time_us, raw, retained_name, *, track_angles=False, feature_geometry=False):
    """One KML2.2 semantic implementation shared by both containers; no container is synthesised."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    if len(text.encode("utf-8")) > INPUT_LIMIT or "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("bounded XML without DTD required")
    ns = "{http://www.opengis.net/kml/2.2}"
    gx = "{http://www.google.com/kml/ext/2.2}"
    root = ET.fromstring(text)
    if root.tag != ns + "kml":
        raise ValueError("KML2.2 namespace required")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    track_angles = track_angles or feature_geometry
    context_fields = ["feature_path_hex", "feature_id_hex", "feature_name_hex", "geometry_id_hex",
        "polygon_path_hex", "ring_index", "ring_role", "multitrack_path_hex", "multitrack_part_index",
        "multitrack_interpolate_reported", "feature_time_precision", "feature_time_year",
        "feature_time_month", "feature_time_day"]
    context_fields += [f"feature_{end}_{field}" for end in ("begin", "end")
                      for field in ("text_hex", "precision", "unix_us", "year", "month", "day")]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "geometry_index", "point_index", "geometry_role",
        "latitude_deg", "longitude_deg", "coordinate_basis", "declared_altitude_m",
        "altitude_basis", "coordinate_text_hex", "source_sha256",
        "feature_time_unix_us", "feature_time_text_hex", "feature_time_scope",
        "feature_time_basis", "point_time_unix_us", "point_time_text_hex",
        "point_time_basis", "position_status"] + (["kml_track_heading_deg", "kml_track_tilt_deg",
        "kml_track_roll_deg", "kml_track_angles_status", "kml_track_angles_text_hex",
        "kml_track_angles_basis"] if track_angles else []) + (context_fields if feature_geometry else []))
    report = dict(points=0, selected_geometries=0, ignored_altitudes=0, unselected_subtrees=0,
        source_sha256=digest, source_retention=f"unchanged {retained_name} required",
        clock="CALLER_CAPTURE_WITH_SEPARATE_DECLARED_FEATURE_AND_TRACK_TIMES",
        timed_features=0, timed_points=0, untimed_points=0, timespan_declarations=0,
        gx_track_declarations=0, track_points=0, missing_track_positions=0,
        unconverted_content=f"other geometry, metadata and resources retained only in {retained_name}",
        evidence="declared-geometry-not-physical-trajectory-or-height-certification")
    if track_angles:
        report.update(track_angles_declared=0, track_angles_missing=0, track_angles_not_declared=0)
    if feature_geometry:
        report.update(polygon_declarations=0, polygon_rings=0, multitrack_declarations=0,
                      feature_geometry_basis="DECLARED_BOUNDARIES_AND_TIME_EXTENTS_NOT_RENDERED_OR_INTERPOLATED")
    # `<TimeStamp><when>` belongs to a FEATURE - Document, Folder or Placemark - and never to a
    # geometry or a coordinate. The nearest enclosing feature that declares one supplies the
    # instant for its geometries, every point of that feature carries the SAME value, and the
    # scope that produced it is named. Nothing is interpolated and no per-point time is invented.
    features = {ns + k for k in ("Document", "Folder", "Placemark")}

    scopes = {ns + "Placemark": "PLACEMARK_TIMESTAMP_DECLARED",
              ns + "Folder": "FOLDER_TIMESTAMP_DECLARED",
              ns + "Document": "DOCUMENT_TIMESTAMP_DECLARED"}
    time_basis = ("THE_FEATURES_OWN_DECLARED_INSTANT_SHARED_BY_EVERY_POINT_OF_THAT_FEATURE_"
                  "NEVER_A_PER_POINT_TIME_NEVER_INTERPOLATED_AND_NEVER_A_MEASURED_FIX_TIME")

    def feature_instant(node):
        """`(text, microseconds, scope)` the feature declares, or None.

        A `TimeSpan` is an interval rather than an instant: it is counted and never converted.
        The pinned reference allows a date-only `<when>` and a `Z` or numeric offset; a date
        alone is not an instant on a resolved clock, so the shared GPX rule refuses it instead of
        inventing midnight in a time zone nobody wrote.
        """
        primitives = [c for c in node if c.tag in (ns + "TimeStamp", ns + "TimeSpan")]
        if len(primitives) > 1:
            raise ValueError("one TimeStamp or TimeSpan per Feature required")
        if not primitives:
            return None
        stamp = primitives[0]
        if stamp.tag == ns + "TimeSpan":
            report["timespan_declarations"] += 1
            return "", "", node.tag.removeprefix(ns).upper() + ("_TIMESPAN_DECLARED" if feature_geometry else "_TIMESPAN_UNCONVERTED")
        when = stamp.find(ns + "when")
        if when is None or not (when.text or "").strip():
            raise ValueError("declared TimeStamp without a when value")
        if len(stamp) != 1 or len(when):
            raise ValueError("one scalar when per TimeStamp required")
        text = when.text
        report["timed_features"] += 1
        if feature_geometry and re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", text.strip()):
            calendar_value(text)  # validate without inventing a timezone or instant
            return text, "", scopes[node.tag] + "_CALENDAR_PRECISION"
        return text, utc_microseconds(text.strip()), scopes[node.tag]

    def hex_text(text):
        return "hex:" + text.encode().hex()

    def calendar_value(text):
        stripped = text.strip()
        match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", stripped)
        if match:
            year, month, day = match.groups()
            datetime(int(year), int(month or 1), int(day or 1))
            return dict(text_hex=hex_text(text), precision="DAY" if day else "MONTH" if month else "YEAR",
                        unix_us="", year=int(year), month=int(month) if month else "", day=int(day) if day else "")
        return dict(text_hex=hex_text(text), precision="INSTANT", unix_us=utc_microseconds(stripped),
                    year="", month="", day="")

    def feature_context(node, inherited, path):
        context = dict(inherited)
        names = node.findall(ns + "name")
        if len(names) > 1 or any(len(n) for n in names):
            raise ValueError("single scalar feature name required")
        context.update(feature_path_hex=hex_text(path), feature_id_hex=hex_text(node.get("id", "")),
                       feature_name_hex=hex_text((names[0].text or "") if names else ""))
        primitives = [c for c in node if c.tag in (ns + "TimeStamp", ns + "TimeSpan")]
        if not primitives:
            return context
        if len(primitives) != 1:
            raise ValueError("single feature TimePrimitive required")
        for key in context_fields:
            if key.startswith(("feature_begin_", "feature_end_", "feature_time_")):
                context[key] = ""
        primitive = primitives[0]
        if primitive.tag == ns + "TimeStamp":
            when = primitive.find(ns + "when")
            if when is None:
                raise ValueError("TimeStamp when required")
            value = calendar_value(when.text or "")
            context.update({"feature_time_"+key: value[key] for key in ("precision", "year", "month", "day")})
        else:
            if any(c.tag not in (ns+"begin", ns+"end") or len(c) for c in primitive):
                raise ValueError("scalar TimeSpan endpoints required")
            values = []
            for end in ("begin", "end"):
                nodes = primitive.findall(ns+end)
                if len(nodes) > 1:
                    raise ValueError("duplicate TimeSpan endpoint")
                value = calendar_value(nodes[0].text or "") if nodes else {"precision": "UNBOUNDED"}
                context.update({f"feature_{end}_{key}": value.get(key, "") for key in ("text_hex", "precision", "unix_us", "year", "month", "day")})
                values.append(value)
            a, b = values
            if a["precision"] == b["precision"] != "UNBOUNDED":
                keys = ("unix_us",) if a["precision"] == "INSTANT" else ("year", "month", "day")
                if tuple(a[k] for k in keys) > tuple(b[k] for k in keys):
                    raise ValueError("reversed declared TimeSpan")
        return context

    def geometries(parent, inherited, context=None, path=""):
        context = {} if context is None else context
        for ordinal, child in enumerate(parent):
            child_path = path + "/" + str(ordinal)
            local = dict(context, geometry_id_hex=hex_text(child.get("id", ""))) if feature_geometry else context
            if child.tag in (ns + "Point", ns + "LineString", gx + "Track"):
                if child.tag == gx + "Track":
                    report["gx_track_declarations"] += 1
                yield child, inherited, local
            elif child.tag in features:
                updated = feature_context(child, context, child_path) if feature_geometry else context
                yield from geometries(child, feature_instant(child) or inherited, updated, child_path)
            elif child.tag == ns + "MultiGeometry":
                yield from geometries(child, inherited, context, child_path)
            elif feature_geometry and child.tag == ns + "Polygon":
                allowed = {ns+k for k in ("outerBoundaryIs", "innerBoundaryIs", "altitudeMode", "extrude", "tessellate")}
                if any(c.tag not in allowed for c in child) or len(child.findall(ns+"outerBoundaryIs")) != 1:
                    raise ValueError("Polygon requires one outer boundary and supported modifiers")
                modifiers = [c for c in child if c.tag not in (ns+"outerBoundaryIs", ns+"innerBoundaryIs")]
                report["polygon_declarations"] += 1
                ring_index = 0
                for boundary in child:
                    if boundary.tag not in (ns+"outerBoundaryIs", ns+"innerBoundaryIs"):
                        continue
                    if len(boundary) != 1 or boundary[0].tag != ns+"LinearRing":
                        raise ValueError("boundary needs one LinearRing")
                    ring = boundary[0]
                    if len(ring) != 1 or ring[0].tag != ns+"coordinates" or len(ring[0]):
                        raise ValueError("selected ring needs only scalar coordinates")
                    points = (ring[0].text or "").split()
                    if len(points) < 4:
                        raise ValueError("closed ring with at least four coordinate tuples required")
                    for token in points[0].split(",") + points[-1].split(","):
                        decimal_value(token, scientific=True)
                    if tuple(Decimal(v) for v in points[0].split(",")) != tuple(Decimal(v) for v in points[-1].split(",")):
                        raise ValueError("ring endpoint coordinates must match exactly")
                    projected = ET.Element(ns+"LineString")
                    projected.extend([*modifiers, ring[0]])
                    yield projected, inherited, dict(local, polygon_path_hex=hex_text(child_path),
                        ring_index=ring_index, ring_role="OUTER" if boundary.tag == ns+"outerBoundaryIs" else "INNER")
                    ring_index += 1
                    report["polygon_rings"] += 1
            elif feature_geometry and child.tag == gx + "MultiTrack":
                allowed = {ns+"altitudeMode", gx+"interpolate", gx+"Track"}
                tracks = child.findall(gx+"Track")
                modes, interpolations = child.findall(ns+"altitudeMode"), child.findall(gx+"interpolate")
                if not tracks or any(c.tag not in allowed for c in child) or len(modes)>1 or len(interpolations)>1:
                    raise ValueError("bounded supported MultiTrack declarations required")
                if any(len(c) for c in [*modes, *interpolations]):
                    raise ValueError("scalar MultiTrack modifier required")
                mode = (modes[0].text or "").strip() if modes else "clampToGround"
                interpolation = (interpolations[0].text or "").strip() if interpolations else "0"
                if interpolation not in ("0", "1", "true", "false"):
                    raise ValueError("boolean MultiTrack interpolation required")
                report["multitrack_declarations"] += 1
                for index, track in enumerate(tracks):
                    projected = ET.fromstring(ET.tostring(track))
                    own_modes = projected.findall(ns+"altitudeMode")
                    if len(own_modes)>1 or (own_modes and (own_modes[0].text or "").strip()!=mode):
                        raise ValueError("ambiguous MultiTrack and Track altitude modes")
                    if not own_modes:
                        ET.SubElement(projected, ns+"altitudeMode").text=mode
                    report["gx_track_declarations"] += 1
                    yield projected, inherited, dict(local, geometry_id_hex=hex_text(track.get("id", "")),
                        multitrack_path_hex=hex_text(child_path), multitrack_part_index=index,
                        multitrack_interpolate_reported="true" if interpolation in ("1","true") else "false")
            elif child.tag in (ns + "TimeStamp", ns + "TimeSpan"):
                if parent.tag not in features:
                    raise ValueError("TimePrimitive must belong to a Feature")
                # Examined by `feature_instant`, so already accounted under its own count rather
                # than a second time as something nobody looked at.
                continue
            elif feature_geometry and parent.tag in features and child.tag == ns + "name":
                continue  # interpreted by feature_context, not an unselected subtree
            else:
                report["unselected_subtrees"] += 1

    for node, instant, context in geometries(root, None):
        is_track = node.tag == gx + "Track"
        allowed = {ns + k for k in ("coordinates", "altitudeMode", "extrude", "tessellate")}
        repeated = {ns + "when", gx + "coord", gx + "angles"} if is_track else set()
        retained = {ns + "ExtendedData", ns + "Model"} if is_track else set()
        if is_track:
            allowed = {ns + "altitudeMode"} | repeated | retained
        seen = set()
        for child in node:
            if (child.tag not in allowed or (child.tag in seen and child.tag not in repeated)
                    or (len(child) and child.tag not in retained)):
                raise ValueError("unsupported selected geometry modifier")
            seen.add(child.tag)
            if child.tag in retained or (child.tag == gx + "angles" and not track_angles):
                report["unselected_subtrees"] += 1
        mode_node = node.find(ns + "altitudeMode")
        mode = "clampToGround" if mode_node is None else (mode_node.text or "").strip()
        bases = {"clampToGround": "GROUND_CLAMPED_NO_NUMERIC_HEIGHT",
                 "absolute": "SEA_LEVEL_DECLARED", "relativeToGround": "GROUND_RELATIVE_DECLARED"}
        if mode not in bases:
            raise ValueError("unsupported altitude mode")
        coordinates = node.find(ns + "coordinates")
        tuples = [] if coordinates is None else (coordinates.text or "").split()
        track_times = []
        angles = []
        if is_track:
            tuples = [(c.text or "") for c in node.findall(gx + "coord")]
            whens = node.findall(ns + "when")
            angles = node.findall(gx + "angles")
            if len(whens) != len(tuples) or (angles and len(angles) != len(tuples)):
                raise ValueError("equal track when coordinate and optional angle counts required")
            track_times = [(w.text or "", utc_microseconds((w.text or "").strip())) for w in whens]
        if not tuples or (node.tag == ns + "Point" and len(tuples) != 1):
            raise ValueError("selected geometry coordinates required")
        for number, point in enumerate(tuples):
            selected_angles = []
            if track_angles:
                angle_text = (angles[number].text or "") if angles else ""
                cells = angle_text.split()
                if cells and len(cells) != 3:
                    raise ValueError("three scalar KML track angles required")
                values = [decimal_value(v, scientific=True) for v in cells] if cells else ["", "", ""]
                status = ("NOT_A_TRACK" if not is_track else "NOT_DECLARED_NOT_INFERRED" if not angles
                          else "MISSING_DECLARATION_NOT_INTERPOLATED" if not cells else "DECLARED")
                if is_track:
                    report["track_angles_declared" if cells else "track_angles_missing" if angles
                           else "track_angles_not_declared"] += 1
                selected_angles = values + [status, "hex:" + angle_text.encode().hex() if angles else "",
                    "KML_GX_TRACK_HEADING_TILT_ROLL_DEGREES_DECLARATION_NOT_AIRCRAFT_EULER_OR_COMPOSED_MODEL_ROTATION"]
            missing = is_track and not point.strip()
            cells = point.split() if is_track else point.split(",")
            if not missing and len(cells) not in ((3,) if is_track else (2, 3)):
                raise ValueError("longitude latitude and optional altitude required")
            lon = "" if missing else decimal_value(cells[0], -180, 180, scientific=True)
            lat = "" if missing else decimal_value(cells[1], -90, 90, scientific=True)
            z = decimal_value(cells[2], scientific=True) if len(cells) == 3 else None
            if mode != "clampToGround" and z is None and not missing:
                raise ValueError("explicit altitude required for selected nonclamped mode")
            if mode == "clampToGround":
                report["ignored_altitudes"] += z is not None
                z = ""
            if missing:
                z = ""
                report["missing_track_positions"] += 1
            report["track_points"] += is_track
            writer.writerow([capture_time_us, report["selected_geometries"], number,
                "POLYGON_RING_DECLARED" if context.get("ring_role") else "GX_TRACK_DECLARED" if is_track else node.tag.removeprefix(ns) + "_DECLARED",
                lat, lon, "WGS84_DECIMAL_DEGREES",
                z, bases[mode], "hex:" + point.encode().hex(), digest,
                "" if instant is None else instant[1],
                "" if instant is None or not instant[0] else "hex:" + instant[0].encode().hex(),
                "NO_FEATURE_TIME_DECLARED" if instant is None else instant[2],
                "DECLARED_FEATURE_TIME_EXTENT_OR_CALENDAR_PRECISION_NOT_A_POINT_SAMPLE_OR_INTERPOLATION" if feature_geometry else time_basis,
                track_times[number][1] if is_track else "",
                "hex:" + track_times[number][0].encode().hex() if is_track else "",
                "GX_TRACK_PAIRED_WHEN_DECLARED_NOT_VERIFIED_FIX_TIME" if is_track else "NO_POINT_TIME_DECLARED",
                "MISSING_COORDINATE_NOT_INTERPOLATED" if missing else "COORDINATE_DECLARED"] + selected_angles +
                ([context.get(key, "") for key in context_fields] if feature_geometry else []))
            report["timed_points" if is_track or (instant and instant[1] != "") else "untimed_points"] += 1
            report["points"] += 1
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("CSV exceeds bound")
        report["selected_geometries"] += 1
    if not report["points"]:
        raise ValueError("no selected geometry")
    return output.getvalue(), report


def convert_wpl(raw, capture_time_us, *, parameter_meanings=False):
    """Read selected declarations only; never construct or send a mission."""
    if type(parameter_meanings) is not bool:
        raise ValueError('explicit boolean selector required')
    if not isinstance(raw, bytes) or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("bounded WPL input required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    lines = raw.decode("utf-8-sig").splitlines()
    if not lines or lines[0] != "QGC WPL 110" or b"\x00" in raw:
        raise ValueError("exact WPL110 header required")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "source_line", "point_role", "declared_sequence",
        "current_declared", "autocontinue_declared", "frame_code", "command_code",
        "latitude_deg", "longitude_deg", "coordinate_basis", "declared_altitude_m",
        "altitude_basis", "source_sha256"] + (FIELDS if parameter_meanings else []))
    report = dict(source_rows=0, decoded_points=0, unselected_rows=0, comments=0,
        source_sha256=digest, clock="CALLER_CAPTURE_NOT_ARRIVAL_OR_DEVICE_TIME",
        source_retention="unchanged source.waypoints required with CSV/report",
        unconverted_content="parameters1..4 and unselected commands/frames remain source-only",
        evidence="declared-points-not-execution-or-physical-certification")
    bases = {0: "MSL_DECLARED", 3: "HOME_RELATIVE_DECLARED", 10: "TERRAIN_RELATIVE_DECLARED"}
    if parameter_meanings:
        report['unconverted_content'] = 'unqualified commands/frames and original values retained; not execution'
        report['parameter_uninterpreted_rows'] = 0
        report['parameter_qualified_rows'] = 0
        report['output_rows'] = 0
    for number, line in enumerate(lines[1:], 2):
        if line.startswith("#"):
            report["comments"] += 1
            continue
        if not line.strip():
            continue
        cells = line.split()
        if len(cells) != 12:
            raise ValueError("exact twelve WPL fields required")
        codes = []
        for index, maximum in ((0, 65535), (1, 1), (2, 255), (3, 65535), (11, 1)):
            if not re.fullmatch(r"[0-9]{1,5}", cells[index]) or int(cells[index]) > maximum:
                raise ValueError("invalid WPL integer field")
            codes.append(int(cells[index]))
        # Unselected parameters may use protocol NaN. They are never normalized.
        for value in cells[4:11]:
            if value.lower() != "nan":
                decimal_value(value, scientific=True)
        seq, current, frame, command, auto = codes
        report["source_rows"] += 1
        if not parameter_meanings and (command != 16 or frame not in bases):
            report["unselected_rows"] += 1
            continue
        meanings = interpret(command, frame, cells[4:11], definition_pin='1c5b39f6709166596341b855d92331c1c0885b5c') if parameter_meanings else {}
        if parameter_meanings:
            if command not in DEFINITIONS:
                report['parameter_uninterpreted_rows'] += 1
                report['unselected_rows'] += 1
            else:
                report['parameter_qualified_rows'] += 1
            lat, lon, altitude = (meanings[f'declared_p{i}_value']
                                  if meanings[f'declared_p{i}_role'] == role else ''
                                  for i, role in ((5, 'latitude'), (6, 'longitude'), (7, 'altitude')))
        else:
            lat = decimal_value(cells[8], -90, 90, scientific=True)
            lon = decimal_value(cells[9], -180, 180, scientific=True)
            altitude = decimal_value(cells[10], scientific=True)
        role = 'MISSION_ITEM_DECLARED_NOT_EXECUTED' if parameter_meanings else 'MISSION_POINT_DECLARED_NOT_EXECUTED'
        output_row = [capture_time_us, number, role, seq, current, auto, frame, command,
                      lat, lon, 'WGS84_DECIMAL_DEGREES' if lat != '' or lon != '' else '',
                      altitude, bases.get(frame, '') if altitude != '' else '', digest]
        if parameter_meanings:
            output_row += [meanings[field] for field in FIELDS]
        writer.writerow(output_row)
        if not parameter_meanings or lat != '' and lon != '' and altitude != '':
            report["decoded_points"] += 1
        if parameter_meanings:
            report['output_rows'] += 1
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("CSV exceeds bound")
    if not (report['parameter_qualified_rows'] if parameter_meanings else report["decoded_points"]):
        raise ValueError("no selected declared mission points")
    return output.getvalue(), report


def convert_boundary(raw, capture_time_us, *, allow_empty=False):
    """Fixed BoundaryFiles.Save local declarations, never a navigation trajectory."""
    if not isinstance(raw, bytes) or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("bounded UTF8 boundary required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    lines = raw.decode("utf-8-sig").splitlines()
    if not lines or lines[0] != "$Boundary":
        raise ValueError("explicit BoundaryFiles header required")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "point_index", "ring_index", "point_in_ring", "ring_role",
        "drive_through_reported", "easting_m", "northing_m", "heading_rad", "coordinate_basis", "source_sha256"])
    report = dict(points=0, rings=0, empty_rings=0, empty_ring_declarations=[], source_sha256=digest,
        source_retention="unchanged source.boundary.txt, retained with CSV and report",
        clock="CALLER_CAPTURE_NOT_POINT_OR_DEVICE_TIME",
        evidence="declared-boundary-not-physical-certification",
        numeric_precision="finite f64; original rounded writer text retained",
        scope="BoundaryFiles.Save at 21be26aa; no field origin, drift correction or path execution")
    cursor = 1
    while cursor < len(lines):
        if cursor + 1 >= len(lines) or lines[cursor] not in ("True", "False"):
            raise ValueError("one declared drive-through flag per ring required")
        flag = int(lines[cursor] == "True")
        count_text = lines[cursor + 1]
        if not re.fullmatch(r"[0-9]{1,9}", count_text):
            raise ValueError("bounded point count required")
        count = int(count_text)
        cursor += 2
        if count > len(lines) - cursor:
            raise ValueError("truncated declared ring")
        for number in range(count):
            cells = lines[cursor + number].split(",")
            if len(cells) != 3:
                raise ValueError("exact ENH triple required; grouped numeric formats not inferred")
            east, north, heading = [decimal_value(cell, scientific=True) for cell in cells]
            writer.writerow([capture_time_us, report["points"], report["rings"], number,
                "OUTER_DECLARED" if report["rings"] == 0 else "INNER_DECLARED", flag,
                east, north, heading, "LOCAL_FIELD_ORIGIN_UNRESOLVED_NOT_WGS84", digest])
            report["points"] += 1
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("CSV exceeds bound")
        cursor += count
        if count == 0:
            report["empty_ring_declarations"].append(dict(ring_index=report["rings"], drive_through_reported=flag))
        report["rings"] += 1
        report["empty_rings"] += count == 0
    if not report["points"] and not allow_empty:
        raise ValueError("no declared boundary points")
    return output.getvalue(), report


# TrackFiles.Save/Load at 21be26aa writes one TrackLines.txt block per track: name, heading, point
# A, point B, nudge, mode, visibility, curve count, then that many easting,northing,heading lines.
# Every mode stores both the A/B reference line and the curve list; the mode says which one is the
# guidance geometry, so the layout never changes and no geometry is guessed from the shape.
TRACK_MODES = {0: ("None", "NO_DESIGNATED_GEOMETRY_DECLARED"),
               2: ("AB", "REFERENCE_LINE_A_TO_B_DECLARED"),
               4: ("Curve", "CURVE_POINT_LIST_DECLARED"),
               8: ("bndTrackOuter", "CURVE_POINT_LIST_DECLARED_FROM_OUTER_BOUNDARY"),
               16: ("bndTrackInner", "CURVE_POINT_LIST_DECLARED_FROM_INNER_BOUNDARY"),
               32: ("bndCurve", "CURVE_POINT_LIST_DECLARED_FROM_BOUNDARY_CURVE"),
               64: ("waterPivot", "CURVE_POINT_LIST_DECLARED_AS_PIVOT_TRACK")}
TRACK_FIELDS = ["record_time_us", "point_index", "track_index", "point_in_track", "point_role",
                "track_name_hex", "track_mode_code", "track_mode_reported", "track_geometry_basis",
                "track_heading_rad", "track_nudge_distance_m", "track_visible_reported",
                "track_curve_point_count", "easting_m", "northing_m", "heading_rad",
                "coordinate_basis", "source_sha256"]


def track_pair(line):
    """One declared easting,northing reference point; the writer rounds both to three decimals."""
    cells = line.split(",")
    if len(cells) != 2:
        raise ValueError("exact easting,northing pair required; grouped formats not inferred")
    return [decimal_value(cell, scientific=True) for cell in cells]


def convert_recpath(raw, capture_time_us):
    """Fixed saved recording-callsite path; never execute or certify a driven trajectory."""
    if not isinstance(raw, bytes) or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("bounded UTF8 RecPath file required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    lines = raw.decode("utf-8-sig").splitlines()
    if len(lines) < 2 or lines[0] != "$RecPath" or not re.fullmatch(r"[0-9]{1,9}", lines[1]):
        raise ValueError("explicit RecPath Save header and count required")
    count = int(lines[1])
    if count != len(lines) - 2:
        raise ValueError("RecPath count does not match saved point records")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "point_index", "easting_m", "northing_m", "heading_rad",
                     "reported_path_speed_m_s", "section_master_auto_reported", "coordinate_basis",
                     "speed_basis", "source_sha256"])
    for index, line in enumerate(lines[2:]):
        cells = line.split(",")
        if len(cells) != 5 or cells[4] not in ("True", "False"):
            raise ValueError("exact RecPath point fields and Boolean required")
        east, north, heading, speed = [decimal_value(cell, scientific=True) for cell in cells[:4]]
        if speed < 1:
            raise ValueError("RecPath speed outside qualified recording-callsite minimum")
        writer.writerow([capture_time_us, index, east, north, heading, speed / 3.6,
                         "TRUE" if cells[4] == "True" else "FALSE",
                         "LOCAL_FIELD_ORIGIN_UNRESOLVED_NOT_WGS84",
                         "SAVED_REPLAY_SPEED_KMH_CLAMPED_AT_ONE_NOT_MEASURED_SPEED", digest])
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("CSV exceeds bound")
    return output.getvalue(), dict(points=count, source_sha256=digest,
        source_retention="unchanged source.recpath.txt required",
        clock="CALLER_CAPTURE_NOT_POINT_OR_DEVICE_TIME",
        evidence="saved-recording-callsite-values-not-physical-trajectory-certification",
        numeric_precision="finite f64; writer rounds XY/heading to 3 and replay speed to 1 decimal",
        scope="RecPathFiles.Save and Position recording callsite at 21be26aa; no origin or execution")


def convert_track(raw, capture_time_us, *, allow_empty=False):
    """Fixed TrackFiles.Save guidance declarations: a saved plan, never a driven or executed path."""
    if not isinstance(raw, bytes) or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("bounded UTF8 track file required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    lines = raw.decode("utf-8-sig").splitlines()
    if not lines or lines[0] != "$TrackLines":
        # The pinned loader also accepts $TwolTracks and then reads and discards two extra lines per
        # track; accepting it here would silently drop declared data, so it is named and refused.
        raise ValueError("explicit $TrackLines header required; the $TwolTracks variant of this "
                         "writer carries two further declared lines per track and is not qualified")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(TRACK_FIELDS)
    report = dict(tracks=0, points=0, curve_points=0, reference_points=0, unrecognised_modes=[],
                  empty_curve_tracks=0, source_sha256=digest,
                  source_retention="unchanged source.tracklines.txt, retained with CSV and report",
                  clock="CALLER_CAPTURE_NOT_TRACK_OR_DEVICE_TIME",
                  evidence="declared-guidance-plan-not-driven-path-or-actuation",
                  numeric_precision="finite f64; writer rounds coordinates to 3 and point heading to 5",
                  scope="TrackFiles.Save at 21be26aa; no field origin, world transform or execution")
    cursor = 1
    while cursor < len(lines):
        block = lines[cursor:cursor + 8]
        if len(block) < 8:
            raise ValueError("truncated declared track block")
        name, heading, a_line, b_line, nudge, mode_text, visible, count_text = block
        if not name.strip():
            # The writer can emit an empty name while its own loader skips that line, which
            # desynchronises every following field. Refused rather than silently mirrored.
            raise ValueError("declared track name required; the pinned loader skips an empty name "
                             "line and would misread the rest of the block")
        if not re.fullmatch(r"[0-9]{1,9}", count_text) or visible not in ("True", "False"):
            raise ValueError("declared visibility flag and bounded curve count required")
        if not re.fullmatch(r"-?[0-9]{1,9}", mode_text):
            raise ValueError("declared integer track mode required")
        mode = int(mode_text)
        named, basis = TRACK_MODES.get(mode, (f"UNKNOWN_{mode}",
                                              "GEOMETRY_ROLE_UNQUALIFIED_UNRECOGNISED_MODE"))
        if mode not in TRACK_MODES:
            report["unrecognised_modes"].append(dict(track_index=report["tracks"], mode_code=mode))
        count = int(count_text)
        cursor += 8
        if count > len(lines) - cursor:
            raise ValueError("truncated declared curve point list")
        track_index = report["tracks"]
        # The per-track declarations repeat on every row of that track, so a single point carries
        # its own identity without a join. The name travels as hex: a numeric-looking track name
        # would otherwise reach the common reader as a number.
        declarations = ["hex:" + name.encode("utf-8").hex(), mode, named, basis,
                        decimal_value(heading, scientific=True),
                        decimal_value(nudge, scientific=True), int(visible == "True"), count]
        points = [("REFERENCE_A_DECLARED", *track_pair(a_line), None),
                  ("REFERENCE_B_DECLARED", *track_pair(b_line), None)]
        for number in range(count):
            cells = lines[cursor + number].split(",")
            if len(cells) != 3:
                raise ValueError("exact easting,northing,heading triple required")
            east, north, point_heading = [decimal_value(cell, scientific=True) for cell in cells]
            points.append(("CURVE_POINT_DECLARED", east, north, point_heading))
        for number, (role, east, north, point_heading) in enumerate(points):
            writer.writerow([capture_time_us, report["points"], track_index, number, role,
                             *declarations, east, north,
                             # Only a curve point carries its own heading; A and B never borrow
                             # the track heading, which travels separately on every row.
                             "" if point_heading is None else point_heading,
                             "LOCAL_FIELD_ORIGIN_UNRESOLVED_NOT_WGS84", digest])
            report["points"] += 1
            report["curve_points"] += role == "CURVE_POINT_DECLARED"
            report["reference_points"] += role != "CURVE_POINT_DECLARED"
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("CSV exceeds bound")
        cursor += count
        report["empty_curve_tracks"] += count == 0
        report["tracks"] += 1
    if not report["tracks"] and not allow_empty:
        raise ValueError("no declared guidance track")
    return output.getvalue(), report


def deere_points_to_gpx(raw):
    """Pinned decoder -> explicit derived standard export; original ZIP stays authoritative."""
    try:
        import shapefile
    except ImportError as exc:
        raise ValueError("install pinned optional shapefile decoder") from exc
    if shapefile.__version__ != "2.3.1" or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("pinned decoder and bounded input required")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if (len(names) != len(set(names)) or len(names) != 5
                or any("/" in n or "\\" in n or "\x00" in n for n in names)
                or sum(info.file_size for info in archive.infolist()) > INPUT_LIMIT):
            raise ValueError("bounded single flat dataset required")
        shapes = [n for n in names if n.endswith(".shp")]
        if len(shapes) != 1: raise ValueError("one point dataset required")
        stem = shapes[0][:-4]
        expected = {stem + suffix for suffix in (".shp", ".shx", ".dbf", ".prj", "-Deere-Metadata.json")}
        if set(names) != expected: raise ValueError("matching dataset members required")
        parts = {name[len(stem):]: archive.read(name) for name in names}
    for suffix in (".shp", ".shx"):
        block = parts[suffix]
        if (len(block) < 100 or struct.unpack_from(">i", block)[0] != 9994
                or struct.unpack_from(">i", block, 24)[0] * 2 != len(block)
                or struct.unpack_from("<ii", block, 28) != (1000, 1)):
            raise ValueError("exact Point container header and length required")
    if parts[".prj"].decode("utf-8-sig").strip() != DEERE_WGS84:
        raise ValueError("unqualified CRS; do not infer WGS84 from coordinate values")
    metadata = json.loads(parts["-Deere-Metadata.json"], object_pairs_hook=unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid metadata number")))
    attributes = metadata["DataAttributes"]
    if metadata["Version"] != "1.0" or not isinstance(attributes, list):
        raise ValueError("qualified metadata version and attributes required")
    declared = [item["Name"] for item in attributes]
    if len(declared) != len(set(declared)) or "IsoTime" not in declared:
        raise ValueError("unambiguous declared IsoTime required")
    root = ET.Element("gpx", xmlns=NS[1:-1], version="1.1", creator="Musubi passive point projection")
    count = 0
    try:
        with shapefile.Reader(**{key: io.BytesIO(parts["." + key]) for key in ("shp", "shx", "dbf")},
                              encoding="utf-8", encodingErrors="strict") as reader:
            fields = [f[0] for f in reader.fields[1:]]
            if reader.shapeType != 1 or len(fields) != len(set(fields)) or "IsoTime" not in fields:
                raise ValueError("Point geometry and unique timestamp column required")
            has_section = "SECTIONID" in declared
            if has_section != ("SECTIONID" in fields):
                raise ValueError("SECTIONID metadata and DBF declaration must agree")
            if has_section:
                section_index = fields.index("SECTIONID")
                if list(reader.fields[section_index + 1][1:]) != ["N", 9, 0]:
                    raise ValueError("selected SECTIONID requires exact DBF N9/0 representation")
                section_offset = 1 + sum(f[2] for f in reader.fields[1:section_index + 1])
                header_length, record_length = struct.unpack_from("<HH", parts[".dbf"], 8)
            for index, shape in enumerate(reader.iterShapes()):
                section, section_basis = "", "NOT_DECLARED"
                if has_section:
                    start = header_length + index * record_length + section_offset
                    cell = parts[".dbf"][start:start + 9]
                    token = cell.strip(b" ")
                    if len(cell) != 9:
                        raise ValueError("truncated SECTIONID cell")
                    if token in (b"", b"*********"):
                        section_basis = "MISSING_SOURCE_VALUE"
                    elif re.fullmatch(rb"[+-]?[0-9]+", token):
                        section, section_basis = str(int(token)), DEERE_SECTION_BASIS
                    else:
                        raise ValueError("SECTIONID must be an exact integer or explicit numeric null")
                record = reader.record(index)
                if record is None or shape.shapeType != 1 or len(shape.points) != 1:
                    raise ValueError("deleted/null/nonpoint records need a separate profile")
                timestamp = record.as_dict()["IsoTime"]
                utc_microseconds(timestamp)
                longitude, latitude = shape.points[0]
                # Decimal spelling without exponent preserves finite double values for GPX.
                lon, lat = (format(Decimal(str(value)), "f") for value in (longitude, latitude))
                point = ET.SubElement(root, "wpt", lat=lat, lon=lon)
                ET.SubElement(point, "time").text = timestamp
                extension = ET.SubElement(point, "extensions")
                ET.SubElement(extension, DEERE_NS + "section_id", basis=section_basis).text = section
                count += 1
            if not count or count != reader.numRecords:
                raise ValueError("geometry/attribute count mismatch or zero points")
    except (shapefile.ShapefileException, IndexError, struct.error) as exc:
        raise ValueError("invalid shapefile dataset") from exc
    derived = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return derived, dict(original_zip_sha256=hashlib.sha256(raw).hexdigest(),
        source_records=count, projected_points=count, decoder="pyshp2.3.1",
        metadata_version="1.0", derivation="WGS84 Point, IsoTime and declared SECTIONID -> source.gpx; original source.zip retained",
        section_id_declared=has_section, section_id_basis=DEERE_SECTION_BASIS,
        unconverted_fields=[name for name in fields if name not in ("IsoTime", "SECTIONID")],
        nonclaim="attributes including rate/gallon basis, elevation datum and heading not normalized")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path, help="new directory only")
    parser.add_argument("--capture-time-us", type=int, required=True)
    parser.add_argument("--kml-track-angles", action="store_true", help="interpret explicit gx:Track heading/tilt/roll degree declarations only")
    parser.add_argument("--kml-feature-geometry", action="store_true", help="connect declared feature identity, polygon rings, time extents and MultiTrack including reported angles")
    parser.add_argument("--source-format", choices=("gpx11", "agopen-boundary-21be26aa", "agopen-track-21be26aa", "agopen-recpath-21be26aa", "deere-point-v1", "qgc-wpl110", "kmz-kml22", "kml22"), default="gpx11")
    parser.add_argument('--mission-parameter-meanings', action='store_true')
    args = parser.parse_args()
    try:
        with args.input.open("rb") as source:
            raw = source.read(INPUT_LIMIT + 1)
        boundary = args.source_format == "agopen-boundary-21be26aa"
        track = args.source_format == "agopen-track-21be26aa"
        recpath = args.source_format == "agopen-recpath-21be26aa"
        wpl = args.source_format == "qgc-wpl110"
        if args.mission_parameter_meanings and not wpl:
            raise ValueError('mission parameter meanings require WPL')
        kmz = args.source_format == "kmz-kml22"
        kml = args.source_format == "kml22"
        projection = args.source_format == "deere-point-v1"
        if (args.kml_track_angles or args.kml_feature_geometry) and not (kml or kmz):
            raise ValueError("track angle option requires KML or KMZ")
        source, basis = deere_points_to_gpx(raw) if projection else (raw, None)
        output, report = (convert_wpl(source, args.capture_time_us, parameter_meanings=True) if args.mission_parameter_meanings else
                         (convert_kmz if kmz else convert_kml)(source, args.capture_time_us, track_angles=args.kml_track_angles,
                          feature_geometry=args.kml_feature_geometry) if args.kml_track_angles or args.kml_feature_geometry else
                         convert(source, args.capture_time_us, deere_projection=True) if projection else
                         (convert_kmz if kmz else convert_kml if kml else convert_wpl if wpl
                          else convert_recpath if recpath else convert_track if track
                          else convert_boundary if boundary else convert)(source, args.capture_time_us))
        if projection: report["input_projection"] = basis
        args.output.mkdir()
        if projection: (args.output / "source.zip").write_bytes(raw)
        (args.output / ("source.kmz" if kmz else "source.kml" if kml
                            else "source.waypoints" if wpl else "source.recpath.txt" if recpath else "source.tracklines.txt" if track
                            else "source.boundary.txt" if boundary else "source.gpx")).write_bytes(source)
        (args.output / "observations.csv").write_text(output, encoding="utf-8")
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, ET.ParseError, OverflowError, zipfile.BadZipFile,
            RuntimeError, KeyError, TypeError, IndexError, struct.error):
        parser.exit(2, "GPX conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
