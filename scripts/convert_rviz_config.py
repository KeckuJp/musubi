#!/usr/bin/env python3
"""Saved RViz `.rviz` display configuration -> typed declaration CSV for TelemetryCsvReader.

Saved-configuration import only. No RViz process, no ROS graph, no node, no discovery, no subscription,
no device, no network operation, no transform computation and no UI reproduction. A configured topic is
not evidence that anything published, a declared fixed frame is not a coordinate transform, `Enabled` is
a saved UI setting and not device state, and a declared name or class identifier is not a device identity.

Schema is fixed against RViz2 11.2.3 (eedb2acca2b79cb8af75f22f9852692ee11f2b78, ros2/rviz, ROS 2 Humble),
writer/property side only: visualization_frame.cpp, visualization_manager.cpp, yaml_config_writer.cpp,
yaml_config_reader.cpp, config.cpp, display.cpp, display_group.cpp, properties/property.cpp,
properties/qos_profile_property.cpp, properties/ros_topic_property.cpp, ros_topic_display.hpp.

The YAML safety, retention and bounding rules are the ones the adopted saved-YAML declaration path
already established (`convert_parameter_yaml` / `convert_map_metadata` in convert_rosbag_observations.py,
read-only here): the pinned ruamel.yaml safe reader used through `compose`, the explicit token scan that
refuses aliases, anchors, tags and directives, the explicit document count, duplicate/invalid mapping key
refusal, a bounded read before allocation, and the reader's own exceptions never escaping.

Contract, refusals and what the pin does not settle:
docs/saved-inputs.md
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re

from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode

PIN = "rviz2-11.2.3-eedb2ac"
LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
REPORT_LIMIT = 32 * 1024 * 1024
MAX_ROWS = 200_000
MAX_DISPLAYS = 20_000
MAX_DEPTH = 32
INT_MAX = 2 ** 31 - 1

# `VisualizationFrame::save` / `VisualizationManager::save` / `DisplayGroup::save`.
MANAGER = "Visualization Manager"
DISPLAYS = "Displays"
GLOBAL_OPTIONS = "Global Options"
FIXED_FRAME = "Fixed Frame"
# `Display::save`: the class id, the name and the enabled flag; `Property::save` adds `Value`.
CLASS = "Class"
NAME = "Name"
ENABLED = "Enabled"
TOPIC = "Topic"
VALUE = "Value"
# `QosProfileProperty`: the child names and, per policy, the literal option spellings. Which option is
# each enumeration's initial value is not established by the pinned source, so an absent key stays
# undeclared here rather than being defaulted.
QOS_POLICIES = {
    "History Policy": ("HISTORY_POLICY", {"System Default": "SYSTEM_DEFAULT", "Keep Last": "KEEP_LAST",
                                          "Keep All": "KEEP_ALL"}),
    "Reliability Policy": ("RELIABILITY_POLICY", {"System Default": "SYSTEM_DEFAULT",
                                                  "Reliable": "RELIABLE", "Best Effort": "BEST_EFFORT"}),
    "Durability Policy": ("DURABILITY_POLICY", {"System Default": "SYSTEM_DEFAULT",
                                                "Transient Local": "TRANSIENT_LOCAL",
                                                "Volatile": "VOLATILE"}),
}
QOS_DEPTH = "Depth"
# The saved `Class` string is exactly the id these keys use: `Display::save` writes `getClassId()`;
# `ClassIdRecordingFactory::make` sets `setClassId(class_id)` from the string it created the object
# with; `DisplayFactory::makeRaw` forwards it unchanged to `PluginlibFactory::makeRaw`, which checks
# its built-ins (`package + "/" + name`) and otherwise calls
# `class_loader_->createUnmanagedInstance(class_id.toStdString())` with no rewriting; and pluginlib
# 5.1.5 (ros/pluginlib a3679216f798c83d67244b3fd741d1418a3faafc) resolves that argument verbatim in
# `classes_available_`, keyed by the `<class name="...">` attribute of a plugins_description.xml (the
# `type` attribute when `name` is absent), with no aliasing. So keying on the manifest name is exact.
# Only these declared classes are source qualified for the `Topic` + QoS layout at this pin:
# `rviz_default_plugins/plugins_description.xml` maps each to a C++ type that inherits
# `rviz_common::MessageFilterDisplay<...>`, a sibling of `RosTopicDisplay` under `_RosTopicDisplay`,
# which is where `RosTopicProperty("Topic", ...)` and the `QosProfileProperty` parented to it live.
# A property merely *named* `Topic` under any other class is retained unqualified: same name and shape
# is not the same meaning, and this pin does not settle a custom plugin's property set.
TOPIC_LAYOUT_CLASSES = {
    "rviz_default_plugins/LaserScan": "MessageFilterDisplay<sensor_msgs::msg::LaserScan>",
    "rviz_default_plugins/PointCloud2": "MessageFilterDisplay<sensor_msgs::msg::PointCloud2>",
    "rviz_default_plugins/Path": "MessageFilterDisplay<nav_msgs::msg::Path>",
}
# Read at the same pin and deliberately NOT qualified: `Grid` and `TF` derive straight from
# `rviz_common::Display` and declare no topic property. The class-binding pass established that TF's
# lookup is unambiguous on the display path. `ImageTransportDisplay` and the built-in Group's topic
# layout remain outside the bounded read; unqualified does not assert they have no topic.
TOPIC_LAYOUT_NOT_QUALIFIED = {
    "rviz_default_plugins/Grid": "derives from rviz_common::Display, no topic property",
    "rviz_default_plugins/TF": "derives from rviz_common::Display, no topic property; the same name "
                               "string is declared for a frame transformer too, but pluginlib keeps only "
                               "manifest entries whose base_class_type matches the loader's, so on the "
                               "display path the id is not ambiguous",
    "rviz_default_plugins/Image": "derives from ImageTransportDisplay, whose base was not established "
                                  "within the bounded read",
    # Established on the class-id binding pass: DisplayFactory registers this id as a built-in,
    # `addBuiltInClass("rviz_common", "Group", ..., &newDisplayGroup)`, so it is creatable without a
    # manifest entry. Its topic layout is still outside this unit's scope, so it stays unqualified.
    "rviz_common/Group": "a factory built-in rather than a manifest class; its topic layout is outside "
                         "the scope read for this unit",
}
# `BoolProperty::save` writes the QVariant bool, so the saved spellings are exactly these two.
BOOLEANS = {"true": 1, "false": 0}

DECLARATION_BASIS = "DECLARED_CONFIGURATION_NOT_OBSERVED_DATA_TOPIC_ACTIVITY_OR_DEVICE_STATE"
TOPIC_BASIS = "CONFIGURED_TOPIC_NOT_EVIDENCE_THAT_ANYTHING_PUBLISHED_OR_SUBSCRIBED"
FRAME_BASIS = "DECLARED_FRAME_NAME_ONLY_NOT_A_TRANSFORM_NOTHING_IS_PROJECTED_OR_REFRAMED"
DISPLAY_BASIS = ("DECLARED_DISPLAY_NAME_AND_CLASS_IDENTIFIER_NOT_A_DEVICE_IDENTITY_AND_ENABLED_IS_A_"
                 "SAVED_UI_SETTING_NOT_DEVICE_OR_SENSOR_STATE")
PROPERTY_BASIS = ("RETAINED_AS_DECLARED_WITH_ITS_KEY_PATH_WHETHER_THE_DISPLAY_CLASS_CONSUMES_IT_IS_NOT_"
                  "ESTABLISHED_BY_THIS_PIN")
SECTION_BASIS = "RETAINED_SAVED_SECTION_NOT_INTERPRETED_BY_THIS_IMPORTER"
TOPIC_LAYOUT_QUALIFIED = ("DECLARED_CLASS_INHERITS_THE_PINNED_RosTopicDisplay_TOPIC_AND_QOS_PROPERTY_"
                          "LAYOUT_SO_THOSE_KEYS_ARE_INTERPRETED_AS_THAT_LAYOUT")
TOPIC_LAYOUT_UNQUALIFIED = ("DECLARED_CLASS_IS_NOT_SOURCE_QUALIFIED_FOR_THE_TOPIC_AND_QOS_LAYOUT_AT_THIS_"
                            "PIN_SO_ANY_SUCH_KEY_IS_RETAINED_STRUCTURALLY_AND_NOT_READ_AS_TOPIC_OR_QOS")
LOOKALIKE_BASIS = ("PROPERTY_NAMED_TOPIC_UNDER_A_CLASS_THIS_PIN_DOES_NOT_QUALIFY_RETAINED_WITH_ITS_"
                   "STRUCTURE_VALUES_AND_SOURCE_REFERENCES_NOT_INTERPRETED_AS_A_TOPIC_OR_A_QOS_SETTING")
QOS_BASIS = ("DECLARED_QOS_SETTING_OF_THE_SAVED_DISPLAY_NOT_A_NEGOTIATED_ENDPOINT_AND_NOT_APPLIED_HERE")
KEY_ORDER_BASIS = ("MAPPING_KEY_ORDER_IS_THE_WRITERS_SORTED_ORDER_AND_CARRIES_NO_DECLARED_MEANING_ONLY_"
                   "LIST_ORDER_DOES_AND_IT_IS_PRESERVED_IN_display_index_path_AND_display_order")
CLOCK_BASIS = ("CALLER_DECLARED_CAPTURE_MICROSECONDS_NO_TIME_EXISTS_IN_THE_SAVED_CONFIGURATION_NOT_ROS_"
               "TIME_NOT_SIMULATION_TIME")
NOT_ESTABLISHED = [
    "each QoS enumeration's initial option, so an absent QoS key stays NOT_DECLARED",
    "the property set of any particular display class",
    "whether ImageTransportDisplay inherits the RosTopicDisplay layout, so rviz_default_plugins/Image "
    "stays unqualified",
    "what the root display group writes for its own Class, Name, Enabled and Value",
    "the behaviour of two identically named properties under one display",
]

# Free text travels hex encoded, the convention the other declaration importers here use: the admitted
# CSV reader rejects quoted cells, and a saved name, path or value legitimately contains a comma.
FIELDS = ["declaration_kind", "scope_kind", "display_index_path", "display_path_hex", "display_order",
          "display_depth", "display_class_hex", "display_class_state",
          "class_topic_layout_basis", "display_name_hex",
          "display_name_occurrence", "display_enabled", "display_enabled_state", "setting_key_hex",
          "setting_key_path_hex", "setting_value_hex", "value_kind", "value_number", "qos_setting",
          "qos_option", "topic_form", "row_basis", "source_line", "source_line_end",
          "interpretation_basis"]

class Refused(ValueError):
    """A malformed, ambiguous or unsupported construct. `code` is a fixed token."""

    def __init__(self, code, line=None):
        super().__init__(code if line is None else f"{code} at line {line}")
        self.code = code
        self.line = line


def read_bounded(path, limit=LIMIT):
    """Read at most one byte past the bound, so an oversized file is refused without reading it in."""
    with Path(path).open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Refused("INPUT_TOO_LARGE")
    return data


def text_hex(value):
    """Lossless, unquoted transport for authored text (same helper shape as the peer importers)."""
    return "hex:" + value.encode("utf-8").hex()


def _line(node):
    return node.start_mark.line + 1, node.end_mark.line + 1


def _pairs(node, where):
    """The adopted mapping rule: a duplicate, empty, non-scalar or merge key is ambiguous, not a hint."""
    if not isinstance(node, MappingNode):
        raise Refused(f"{where}_MUST_BE_A_MAPPING", _line(node)[0])
    seen = set()
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or not key.value:
            raise Refused("MAPPING_KEY_MUST_BE_A_NONEMPTY_SCALAR", _line(key)[0])
        if key.value == "<<":
            raise Refused("YAML_MERGE_KEY_UNSUPPORTED", _line(key)[0])
        if key.value in seen:
            raise Refused("DUPLICATE_MAPPING_KEY", _line(key)[0])
        seen.add(key.value)
        yield key.value, value


def _scalar(node, where):
    if not isinstance(node, ScalarNode):
        raise Refused(f"{where}_MUST_BE_A_SCALAR", _line(node)[0])
    return node.value


def _scalar_kind(node):
    """yaml-cpp emits a value plain, an empty string double-quoted and an empty node as Null; the pinned
    loader turns every scalar into a string and types it later, so nothing else is typed here."""
    if node.style in ("'", '"'):
        return "empty_string" if node.value == "" else "declared_scalar_untyped"
    if node.value == "" or node.value in ("~", "null", "Null", "NULL"):
        return "null"
    return "declared_scalar_untyped"


class _Rows:
    """Accumulates declaration rows; display names keep an occurrence index because the pin does not
    make them unique."""

    def __init__(self, capture_time_us, digest):
        self.capture_time_us = capture_time_us
        self.digest = digest
        self.rows = []
        self.counts = {}
        self.names = {}

    def occurrence(self, name):
        self.names[name] = self.names.get(name, 0) + 1
        return self.names[name]

    def add(self, kind, *, scope_kind, node, row_basis, display=None, key="", key_path=(), value=None,
            value_kind="", value_number="", qos_setting="", qos_option="", topic_form=""):
        if len(self.rows) >= MAX_ROWS:
            raise Refused("TOO_MANY_DECLARATION_ROWS", _line(node)[0])
        first, last = _line(node)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        display = display or {}
        self.rows.append({
            "record_time_us": self.capture_time_us,
            "source_document_sha256": "sha256:" + self.digest,
            "declaration_kind": kind,
            "scope_kind": scope_kind,
            "display_index_path": display.get("index_path", ""),
            "display_path_hex": text_hex(display["path"]) if display.get("path") else "",
            "display_order": display.get("order", ""),
            "display_depth": display.get("depth", ""),
            "display_class_hex": text_hex(display["class"]) if display.get("class") else "",
            "display_class_state": display.get("class_state", ""),
            "class_topic_layout_basis": display.get("class_topic_layout_basis", ""),
            "display_name_hex": text_hex(display["name"]) if display.get("name") is not None else "",
            "display_name_occurrence": display.get("name_occurrence", ""),
            "display_enabled": display.get("enabled", ""),
            "display_enabled_state": display.get("enabled_state", ""),
            "setting_key_hex": text_hex(key) if key else "",
            "setting_key_path_hex": text_hex("/".join(key_path)) if key_path else "",
            "setting_value_hex": "" if value is None else text_hex(value),
            "value_kind": value_kind,
            "value_number": value_number,
            "qos_setting": qos_setting,
            "qos_option": qos_option,
            "topic_form": topic_form,
            "row_basis": row_basis,
            "source_line": first,
            "source_line_end": last,
            "interpretation_basis": DECLARATION_BASIS,
        })


def _walk_setting(rows, node, *, scope_kind, display, key, key_path, depth, report,
                  row_basis=PROPERTY_BASIS):
    """Every saved leaf becomes a reference-linked row: nothing is dropped for being uninterpreted."""
    if depth > MAX_DEPTH:
        raise Refused("NESTING_EXCEEDS_BOUND", _line(node)[0])
    if isinstance(node, MappingNode):
        rows.add("rviz_display_property", scope_kind=scope_kind, node=node, row_basis=row_basis,
                 display=display, key=key, key_path=key_path, value_kind="mapping")
        for child, value in _pairs(node, "SAVED_SETTING"):
            _walk_setting(rows, value, scope_kind=scope_kind, display=display, key=child,
                          key_path=(*key_path, child), depth=depth + 1, report=report,
                          row_basis=row_basis)
        return
    if isinstance(node, SequenceNode):
        rows.add("rviz_display_property", scope_kind=scope_kind, node=node, row_basis=row_basis,
                 display=display, key=key, key_path=key_path, value_kind="sequence")
        for index, value in enumerate(node.value, 1):
            _walk_setting(rows, value, scope_kind=scope_kind, display=display, key=key,
                          key_path=(*key_path, str(index)), depth=depth + 1, report=report,
                          row_basis=row_basis)
        return
    report["retained_property_rows"] += 1
    rows.add("rviz_display_property", scope_kind=scope_kind, node=node, row_basis=row_basis,
             display=display, key=key, key_path=key_path, value=_scalar(node, "SAVED_SETTING"),
             value_kind=_scalar_kind(node))


def _walk_children(rows, node, *, scope_kind, key_path, report):
    """Walk the children of a container whose own row is already written."""
    if isinstance(node, MappingNode):
        for key, value in _pairs(node, "SAVED_SETTING"):
            _walk_setting(rows, value, scope_kind=scope_kind, display=None, key=key,
                          key_path=(*key_path, key), depth=2, report=report)
    elif isinstance(node, SequenceNode):
        for index, value in enumerate(node.value, 1):
            _walk_setting(rows, value, scope_kind=scope_kind, display=None, key=key_path[-1],
                          key_path=(*key_path, str(index)), depth=2, report=report)


def _walk_topic(rows, node, *, display, key_path, depth, report):
    """`RosTopicProperty("Topic", ...)` parents the QoS children, so the saved form is a nested map; the
    flat scalar the loader still accepts is retained with its own form marker."""
    if isinstance(node, ScalarNode):
        report["topic_rows"] += 1
        rows.add("rviz_display_topic", scope_kind="display", node=node, row_basis=TOPIC_BASIS,
                 display=display, key=TOPIC, key_path=key_path, value=node.value,
                 value_kind=_scalar_kind(node), topic_form="LEGACY_FLAT_TOPIC_SCALAR")
        return
    for key, value in _pairs(node, "TOPIC"):
        path = (*key_path, key)
        if key == VALUE:
            report["topic_rows"] += 1
            rows.add("rviz_display_topic", scope_kind="display", node=value, row_basis=TOPIC_BASIS,
                     display=display, key=key, key_path=path,
                     value=_scalar(value, "TOPIC_VALUE"), value_kind=_scalar_kind(value),
                     topic_form="NESTED_TOPIC_VALUE")
            continue
        if key == QOS_DEPTH:
            text = _scalar(value, "QOS_DEPTH")
            if not re.fullmatch(r"[0-9]{1,10}", text) or not 1 <= int(text) <= INT_MAX:
                raise Refused("QOS_DEPTH_OUTSIDE_THE_PINNED_INT_PROPERTY_RANGE", _line(value)[0])
            report["qos_rows"] += 1
            rows.add("rviz_display_qos", scope_kind="display", node=value, row_basis=QOS_BASIS,
                     display=display, key=key, key_path=path, value=text, value_kind="integer",
                     value_number=text, qos_setting="DEPTH")
            continue
        if key in QOS_POLICIES:
            setting, options = QOS_POLICIES[key]
            text = _scalar(value, "QOS_POLICY")
            if text not in options:
                raise Refused("QOS_POLICY_OUTSIDE_THE_PINNED_ENUMERATION", _line(value)[0])
            report["qos_rows"] += 1
            rows.add("rviz_display_qos", scope_kind="display", node=value, row_basis=QOS_BASIS,
                     display=display, key=key, key_path=path, value=text, value_kind="enumeration",
                     qos_setting=setting, qos_option=options[text])
            continue
        _walk_setting(rows, value, scope_kind="display", display=display, key=key, key_path=path,
                      depth=depth + 1, report=report)


def _walk_displays(rows, node, *, key_path, parent_index, parent_path, depth, report):
    """`Visualization Manager` -> `Displays` is a sequence; a group is a display with its own sequence."""
    if not isinstance(node, SequenceNode):
        raise Refused("DISPLAY_LIST_MUST_BE_A_SEQUENCE", _line(node)[0])
    if depth > MAX_DEPTH:
        raise Refused("NESTING_EXCEEDS_BOUND", _line(node)[0])
    for order, entry in enumerate(node.value, 1):
        if not isinstance(entry, MappingNode):
            raise Refused("DISPLAY_ENTRY_MUST_BE_A_MAPPING", _line(entry)[0])
        children = dict(_pairs(entry, "DISPLAY"))
        # Slash joined with a leading separator so the path is never a number: the common
        # reader types a numeric-looking cell, and a dotted "2.10" would collide with "2.1".
        index_path = f"{parent_index}/{order}"
        name_node = children.get(NAME)
        name = _scalar(name_node, "DISPLAY_NAME") if name_node is not None else ""
        class_node = children.get(CLASS)
        report["displays"] += 1
        if report["displays"] > MAX_DISPLAYS:
            raise Refused("TOO_MANY_DISPLAYS", _line(entry)[0])
        display = {
            "index_path": index_path,
            "path": f"{parent_path}/{order}:{name}" if parent_path else f"{order}:{name}",
            "order": order,
            "depth": depth,
            # A declared class identifier, not a device identity; absence is tolerated upstream
            # (`mapGetString("Class")` falls back and the display fails), so it is reported, not invented.
            "class": _scalar(class_node, "DISPLAY_CLASS") if class_node is not None else "",
            "class_state": "DECLARED" if class_node is not None else "NOT_DECLARED_IN_THIS_FILE",
            "name": name,
            "name_occurrence": rows.occurrence(name),
        }
        qualified = display["class"] in TOPIC_LAYOUT_CLASSES
        display["class_topic_layout_basis"] = (TOPIC_LAYOUT_QUALIFIED if qualified
                                               else TOPIC_LAYOUT_UNQUALIFIED)
        enabled_node = children.get(ENABLED)
        if enabled_node is None:
            display["enabled_state"] = "NOT_DECLARED_IN_THIS_FILE"
        else:
            text = _scalar(enabled_node, "DISPLAY_ENABLED")
            if text not in BOOLEANS:
                raise Refused("DISPLAY_ENABLED_MUST_BE_THE_WRITERS_TRUE_OR_FALSE", _line(enabled_node)[0])
            display["enabled"] = BOOLEANS[text]
            display["enabled_state"] = "DECLARED"
        nested = children.get(DISPLAYS)
        kind = "rviz_display_group" if nested is not None else "rviz_display"
        if nested is not None:
            report["groups"] += 1
        report["max_depth"] = max(report["max_depth"], depth)
        rows.add(kind, scope_kind="display_group" if nested is not None else "display", node=entry,
                 row_basis=DISPLAY_BASIS, display=display, key_path=(*key_path, str(order)))
        for key, value in children.items():
            path = (*key_path, str(order), key)
            if key in (CLASS, NAME, ENABLED):
                continue  # already carried, typed, on the display row itself
            if key == DISPLAYS:
                _walk_displays(rows, value, key_path=path, parent_index=index_path,
                               parent_path=display["path"], depth=depth + 1, report=report)
                continue
            if key == TOPIC and qualified:
                _walk_topic(rows, value, display=display, key_path=path, depth=depth, report=report)
                continue
            if key == TOPIC:
                # Same name, unqualified class: keep the structure, the values and the source
                # references, and say why they are not read as a topic or a QoS setting.
                report["unqualified_topic_lookalike_properties"] += 1
                _walk_setting(rows, value, scope_kind="display", display=display, key=key,
                              key_path=path, depth=depth + 1, report=report,
                              row_basis=LOOKALIKE_BASIS)
                continue
            _walk_setting(rows, value, scope_kind="display", display=display, key=key, key_path=path,
                          depth=depth + 1, report=report)


def convert(data, capture_time_us):
    """Saved `.rviz` bytes -> (CSV for the common reader, JSON-ready accounting)."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2 ** 63:
        raise Refused("EXPLICIT_CAPTURE_TIME_REQUIRED")
    if not isinstance(data, bytes) or not data or len(data) > LIMIT:
        raise Refused("BOUNDED_CONFIGURATION_BYTES_REQUIRED")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Refused("INVALID_UTF8_CONFIGURATION_FILE") from error
    yaml = YAML(typ="safe")
    # The adopted rule: a reading failure of the pinned reader is a rejection of this input, so the
    # reader's own exception type never escapes, and documents are counted explicitly.
    try:
        tokens = list(yaml.scan(text))
    except Exception as error:
        raise Refused("UNREADABLE_CONFIGURATION_YAML") from error
    if any(type(token).__name__ in ("AliasToken", "AnchorToken", "TagToken", "DirectiveToken")
           for token in tokens):
        raise Refused("YAML_ALIASES_ANCHORS_TAGS_AND_DIRECTIVES_UNSUPPORTED")
    try:
        documents = list(yaml.compose_all(text))
    except Exception as error:
        raise Refused("UNREADABLE_CONFIGURATION_YAML") from error
    if len(documents) != 1:
        raise Refused("ONE_CONFIGURATION_DOCUMENT_REQUIRED")
    digest = hashlib.sha256(data).hexdigest()
    rows = _Rows(capture_time_us, digest)
    report = dict(displays=0, groups=0, max_depth=0, topic_rows=0, qos_rows=0,
                  unqualified_topic_lookalike_properties=0, retained_property_rows=0, root_sections=0,
                  global_options=0, fixed_frame_state="NOT_DECLARED_IN_THIS_FILE")
    root = documents[0]
    sections = dict(_pairs(root, "CONFIGURATION_ROOT"))
    if MANAGER not in sections:
        raise Refused("SAVED_CONFIGURATION_HAS_NO_VISUALIZATION_MANAGER")
    for key, value in sections.items():
        report["root_sections"] += 1
        rows.add("rviz_root_section", scope_kind="root_section", node=value,
                 row_basis=SECTION_BASIS, key=key, key_path=(key,),
                 value_kind="mapping" if isinstance(value, MappingNode)
                 else "sequence" if isinstance(value, SequenceNode) else _scalar_kind(value),
                 value=None if not isinstance(value, ScalarNode) else value.value)
    manager = dict(_pairs(sections[MANAGER], "VISUALIZATION_MANAGER"))
    if DISPLAYS not in manager:
        raise Refused("VISUALIZATION_MANAGER_HAS_NO_DISPLAY_LIST")
    for key, value in manager.items():
        path = (MANAGER, key)
        if key == DISPLAYS:
            _walk_displays(rows, value, key_path=path, parent_index="", parent_path="", depth=1,
                           report=report)
            continue
        if key == GLOBAL_OPTIONS:
            for option, option_value in _pairs(value, "GLOBAL_OPTIONS"):
                if option != FIXED_FRAME and not isinstance(option_value, ScalarNode):
                    # An option this importer does not select on keeps its saved structure: the same
                    # retention walker every other uninterpreted setting uses, not a new one.
                    _walk_setting(rows, option_value, scope_kind="global_options", display=None,
                                  key=option, key_path=(*path, option), depth=2, report=report)
                    continue
                text_value = _scalar(option_value, "GLOBAL_OPTION")
                report["global_options"] += 1
                if option == FIXED_FRAME:
                    report["fixed_frame_state"] = "DECLARED"
                rows.add("rviz_global_option", scope_kind="global_options", node=option_value,
                         row_basis=FRAME_BASIS if option == FIXED_FRAME else PROPERTY_BASIS,
                         key=option, key_path=(*path, option), value=text_value,
                         value_kind=_scalar_kind(option_value))
            continue
        _walk_setting(rows, value, scope_kind="visualization_manager", display=None, key=key,
                      key_path=path, depth=2, report=report)
    for key, value in sections.items():
        # `Panels`, `Window Geometry` and anything else this pin writes beside the manager: retained as
        # reference-linked rows, never interpreted and never dropped. The section row above already
        # carries the container itself, so only its children are walked here.
        if key != MANAGER:
            _walk_children(rows, value, scope_kind="root_section", key_path=(key,), report=report)
    if not report["displays"]:
        # An empty display list is a configuration with nothing to associate: say so rather than
        # returning a file-shaped row set that declares no display at all.
        raise Refused("NO_DECLARED_DISPLAY_IN_THE_SELECTED_FILE")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, ["record_time_us", "source_document_sha256", *FIELDS],
                            lineterminator="\n")
    writer.writeheader()
    for row in rows.rows:
        writer.writerow(row)
        if output.tell() > OUTPUT_LIMIT:
            raise Refused("CONFIGURATION_CSV_EXCEEDS_BOUND")
    accounting = dict(
        pin=PIN, source_sha256="sha256:" + digest, source_bytes=len(data),
        declaration_rows=len(rows.rows), rows_by_kind=dict(sorted(rows.counts.items())), **report,
        capture_time_us=capture_time_us, clock="Unknown", clock_basis=CLOCK_BASIS,
        interpretation_basis=DECLARATION_BASIS, topic_basis=TOPIC_BASIS, frame_basis=FRAME_BASIS,
        display_basis=DISPLAY_BASIS, property_basis=PROPERTY_BASIS, qos_basis=QOS_BASIS,
        key_order_basis=KEY_ORDER_BASIS,
        topic_layout_qualified_classes=dict(sorted(TOPIC_LAYOUT_CLASSES.items())),
        topic_layout_not_qualified_classes=dict(sorted(TOPIC_LAYOUT_NOT_QUALIFIED.items())),
        class_qualification_basis=TOPIC_LAYOUT_QUALIFIED, not_established_by_this_pin=NOT_ESTABLISHED,
        source_retention="unchanged source file retained beside the CSV and this report")
    if len(json.dumps(accounting)) > REPORT_LIMIT:
        raise Refused("REPORT_EXCEEDS_BOUND")
    return output.getvalue(), accounting


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved .rviz configuration file")
    parser.add_argument("new_output_directory", type=Path,
                        help="directory to create; existing inputs and evidence are never overwritten")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="the operator's capture clock; nothing in the saved file is a timestamp")
    args = parser.parse_args()
    created = False
    try:
        data = read_bounded(args.input)
        converted, report = convert(data, args.capture_time_us)
        args.new_output_directory.mkdir()  # Preserve existing inputs and evidence.
        created = True
        (args.new_output_directory / "observations.csv").write_text(converted, encoding="utf-8")
        (args.new_output_directory / "accounting.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (args.new_output_directory / "source.rviz").write_bytes(data)
    except Refused as refusal:
        parser.exit(2, f"saved RViz configuration import refused: {refusal.code}\n")
    except (ValueError, OSError):
        # Only claim nothing was written when nothing could have been.
        if created:
            parser.exit(2, "saved RViz configuration import failed after creating "
                           f"{args.new_output_directory}; its contents are incomplete\n")
        parser.exit(2, "saved RViz configuration import failed; nothing was written\n")
    print(f"imported {report['declaration_rows']} declaration rows from "
          f"{report['displays']} declared displays (pin {report['pin']})")


if __name__ == "__main__":
    main()
