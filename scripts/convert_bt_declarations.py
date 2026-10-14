#!/usr/bin/env python3
"""Saved BehaviorTree.CPP 4.x tree declarations -> unit-normalized CSV for TelemetryCsvReader.

Static structure only: no tree is run, no node is ticked, no plugin is loaded and no
task outcome is claimed.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

# BehaviorTree.CPP 4.6.2 (2a8a226fbbd99f524f0796e0d8a3145773c61c06).
PIN = "behaviortree-cpp-4.6.2"
LIMIT = 1024 * 1024
OUTPUT_LIMIT = 16 * 1024 * 1024
MAX_TREES = 64
MAX_NODES = 4096
MAX_DEPTH = 64
MAX_ROWS = 65536
BUNDLE_LIMIT = 4 * LIMIT
MAX_FILES = 16
BUNDLE_FIELDS = ["source_bundle_sha256", "bt_document_name_hex", "bt_document_disposition",
                 "bt_include_target_hex", "bt_subtree_document_hex", "bt_selected_tree",
                 "bt_selection_basis"]

# src/bt_factory.cpp registration, split by the NodeType each one registers with.
CONTROL_NODES = ("Fallback", "AsyncFallback", "Sequence", "AsyncSequence", "SequenceWithMemory",
                 "SequenceStar", "Parallel", "ParallelAll", "ReactiveSequence", "ReactiveFallback",
                 "IfThenElse", "WhileDoElse", "Switch2", "Switch3", "Switch4", "Switch5", "Switch6")
DECORATOR_NODES = ("Inverter", "RetryUntilSuccessful", "KeepRunningUntilFailure", "Repeat", "Timeout",
                   "Delay", "RunOnce", "ForceSuccess", "ForceFailure", "Precondition",
                   "SkipUnlessUpdated", "WaitValueUpdate", "LoopInt", "LoopBool", "LoopDouble",
                   "LoopString")
LEAF_NODES = ("AlwaysSuccess", "AlwaysFailure", "Script", "ScriptCondition", "SetBlackboard",
              "Sleep", "UnsetBlackboard", "WasEntryUpdated")
GENERIC_TAGS = ("Action", "Condition", "Control", "Decorator", "SubTree")
MODEL_PORTS = {"input_port": "INPUT", "output_port": "OUTPUT", "inout_port": "INOUT"}
FIELDS = ["bt_record_kind", "bt_element_index", "bt_tree_id_hex", "bt_main_tree_declared",
          "bt_node_uid", "bt_parent_uid", "bt_child_ordinal", "bt_child_count", "bt_tag_hex",
          "bt_node_category", "bt_category_basis", "bt_declared_id_hex", "bt_node_name_hex",
          "bt_subtree_ref_hex", "bt_attribute_name_hex", "bt_attribute_kind",
          "bt_blackboard_key_hex", "bt_attribute_value_hex", "bt_port_direction",
          "bt_declaration_basis"]
BASIS = "DECLARED_STRUCTURE_NOT_EXECUTED_OR_TICKED"


def text_hex(value):
    return "hex:" + value.encode("utf-8").hex()


def allowed_port_name(name):
    """src/basic_types.cpp IsAllowedPortName at the pin."""
    if name == "_autoremap":
        return True
    return bool(name) and name[0].isalpha() and name not in ("name", "ID")


def blackboard_key(value):
    """src/tree_node.cpp isBlackboardPointer: trimmed, >=3 chars, braces; inside is the key."""
    trimmed = value.strip(" ")
    if len(trimmed) >= 3 and trimmed.startswith("{") and trimmed.endswith("}"):
        return trimmed[1:-1]
    return None


def declared_models(root):
    """<TreeNodesModel> entries: the only place this file declares port direction.

    Each entry keeps its own element and each port keeps the exact port element, so the
    emitted rows carry the same document order index as every other declaration.
    """
    models = {}
    sections = [child for child in root if child.tag == "TreeNodesModel"]
    if len(sections) > 1:
        raise ValueError("at most one TreeNodesModel section")
    for section in sections[0] if sections else ():
        if section.tag not in ("Action", "Condition", "Control", "Decorator"):
            raise ValueError("unsupported node model category")
        identifier = section.get("ID")
        if not identifier or identifier in models:
            raise ValueError("missing or duplicate node model ID")
        ports = {}
        for port in section:
            direction = MODEL_PORTS.get(port.tag)
            name = port.get("name")
            if direction is None or not name or name in ports:
                raise ValueError("invalid or duplicate declared model port")
            ports[name] = (direction, port)
        models[identifier] = (section.tag.upper(), ports, section)
    return models


def builtin_category(name):
    """Category this document's pin establishes for a registered name, else None."""
    if name in CONTROL_NODES:
        return "CONTROL"
    if name in DECORATOR_NODES:
        return "DECORATOR"
    if name in LEAF_NODES:
        return "ACTION_OR_CONDITION"
    return None


def same_family(left, right):
    """Leaf categories are one family; control and decorator are each their own."""
    leaves = {"ACTION", "CONDITION", "ACTION_OR_CONDITION"}
    return (left in leaves and right in leaves) or left == right


def categorize(tag, declared_id, models):
    """Tag -> category, per the pinned registry; never guessed from a name."""
    if tag in GENERIC_TAGS:
        if not declared_id:
            raise ValueError("generic node tag requires ID")
        established = builtin_category(declared_id) or (
            models[declared_id][0] if declared_id in models else None)
        if tag == "SubTree":
            # The pin refuses a subtree whose ID is the name of a registered node.
            if established is not None:
                raise ValueError("subtree ID must not name a registered or modelled node")
            return "SUBTREE", "GENERIC_TAG_WITH_ID"
        # A generic tag must not assert a category its own ID contradicts.
        if established is not None and not same_family(tag.upper(), established):
            raise ValueError("generic tag contradicts the established category of its ID")
        basis = "GENERIC_TAG_WITH_ID" + ("_AND_DECLARED_MODEL" if declared_id in models else "")
        return tag.upper(), basis
    if declared_id is not None:
        raise ValueError("registered node tag must not carry ID")
    established = builtin_category(tag)
    if established is not None:
        return established, "BUILT_IN_REGISTRY_" + PIN
    if tag in models:
        return models[tag][0], "DECLARED_MODEL_IN_THIS_DOCUMENT"
    return "UNKNOWN_NOT_REGISTERED", "UNQUALIFIED_PLUGIN_NOT_RESOLVABLE_OFFLINE"


def check_children(category, count):
    """VerifyXML counts at the pin, applied by the **established** category, so a named
    built-in leaf and a modelled Action/Condition are held to the same rule as a generic
    one. An unknown custom tag has no established category and is never constrained."""
    if category in ("SUBTREE", "ACTION", "CONDITION", "ACTION_OR_CONDITION"):
        if count:
            raise ValueError("leaf declaration must not have children")
    elif category == "DECORATOR" and count != 1:
        raise ValueError("decorator declaration needs exactly one child")
    elif category == "CONTROL" and count == 0:
        raise ValueError("control declaration needs at least one child")


def _parse_document(data):
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized tree declaration")
    text = data.decode("utf-8-sig")
    # Ordinary saved trees carry XML comments, so `<!--` stays readable. Every other `<!`
    # construct - DOCTYPE, ENTITY, CDATA - is still refused before the reader sees it, so
    # no DTD, entity expansion or external reference is ever resolved. Comments are not
    # declarations: the reader drops them and they take no element index.
    position = text.find("<!")
    while position != -1:
        if not text.startswith("<!--", position):
            raise ValueError("tree XML declarations, DTD and entities unsupported")
        closing = text.find("-->", position + 4)
        if closing == -1:
            raise ValueError("unterminated XML comment")
        position = text.find("<!", closing + 3)
    if "\x00" in text:
        raise ValueError("tree declaration must not contain NUL")
    # One rejection contract: a reader failure is this converter's ValueError, not a
    # parser exception type a caller would have to know about separately.
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError("unreadable tree declaration XML") from error
    if root.tag != "root" or root.get("BTCPP_format") != "4":
        raise ValueError("BTCPP_format 4 root element required")
    return root, text


def convert(data, capture_time_us):
    return _convert_document(data, capture_time_us)


def _convert_document(data, capture_time_us, *, bundle_tree_ids=None):
    """Same declaration decoder; bundle IDs extend reference resolution, not execution."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture time required")
    root, text = _parse_document(data)
    in_bundle = bundle_tree_ids is not None
    if not in_bundle and any(child.tag == "include" for child in root):
        raise ValueError("include references another file and is not resolved here")
    models = declared_models(root)
    trees = [child for child in root if child.tag == "BehaviorTree"]
    if (not trees and not in_bundle) or len(trees) > MAX_TREES:
        raise ValueError("one to sixty-four declared trees required")
    identifiers = []
    for tree in trees:
        identifier = tree.get("ID")
        if not identifier or identifier in identifiers:
            raise ValueError("missing or duplicate tree ID")
        identifiers.append(identifier)
    main = root.get("main_tree_to_execute")
    valid_ids = bundle_tree_ids if in_bundle else identifiers
    if main is not None and main not in valid_ids:
        raise ValueError("declared main tree is not in this document")
    allowed = ("BehaviorTree", "TreeNodesModel", "include") if in_bundle else ("BehaviorTree", "TreeNodesModel")
    if any(child.tag not in allowed for child in root):
        raise ValueError("unsupported root child element")

    order = {element: index for index, element in enumerate(root.iter())}
    rows, references = [], {}

    def append(row):
        """Every emitted declaration counts against the declared row bound."""
        if len(rows) >= MAX_ROWS:
            raise ValueError("declared row bound")
        rows.append(row)

    def attribute_rows(element, uid, category, declared_id):
        model = models.get(declared_id or element.tag, (None, {}, None))[1]
        for name, value in element.attrib.items():
            if name in ("ID", "name"):
                continue
            if not name:
                raise ValueError("empty attribute name")
            if not allowed_port_name(name):
                if not name.startswith("_"):
                    raise ValueError("attribute name is not an allowed port name")
                append(dict(bt_record_kind="RESERVED", bt_element_index=uid, bt_node_uid=uid,
                            bt_attribute_name_hex=text_hex(name),
                            bt_attribute_kind="FRAMEWORK_RESERVED_NOT_PORT",
                            bt_attribute_value_hex=text_hex(value),
                            bt_port_direction="NOT_A_PORT"))
                continue
            if name == "_autoremap":
                append(dict(bt_record_kind="RESERVED", bt_element_index=uid, bt_node_uid=uid,
                            bt_attribute_name_hex=text_hex(name),
                            bt_attribute_kind="SUBTREE_DECLARED_AUTOREMAP",
                            bt_attribute_value_hex=text_hex(value),
                            bt_port_direction="NOT_A_PORT"))
                continue
            key = blackboard_key(value)
            kind = "STATIC_DECLARATION_NOT_RESOLVED_VALUE"
            if key == "=":
                key, kind = name, "BLACKBOARD_SAME_NAME_REMAP"
            elif key is not None:
                kind = "BLACKBOARD_KEY_REFERENCE"
            append(dict(bt_record_kind="PORT", bt_element_index=uid, bt_node_uid=uid,
                        bt_attribute_name_hex=text_hex(name), bt_attribute_kind=kind,
                        bt_blackboard_key_hex=None if key is None else text_hex(key),
                        bt_attribute_value_hex=text_hex(value),
                        bt_port_direction=model.get(name, ("UNKNOWN_WITHOUT_DECLARED_MODEL",))[0]))

    def walk(element, tree_id, parent_uid, ordinal, depth):
        if depth > MAX_DEPTH:
            raise ValueError("tree nesting bound")
        uid = order[element]
        if len(rows) > MAX_ROWS or uid > MAX_NODES:
            raise ValueError("declared element bound")
        declared_id = element.get("ID")
        category, basis = categorize(element.tag, declared_id, models)
        children = list(element)
        check_children(category, len(children))
        subtree_ref = declared_id if category == "SUBTREE" else None
        if subtree_ref is not None:
            references.setdefault(tree_id, []).append(subtree_ref)
        append(dict(bt_record_kind="NODE", bt_element_index=uid, bt_tree_id_hex=text_hex(tree_id),
                         bt_node_uid=uid, bt_parent_uid=parent_uid, bt_child_ordinal=ordinal,
                         bt_child_count=len(children), bt_tag_hex=text_hex(element.tag),
                         bt_node_category=category, bt_category_basis=basis,
                         bt_declared_id_hex=None if declared_id is None else text_hex(declared_id),
                         bt_node_name_hex=None if element.get("name") is None else text_hex(element.get("name")),
                         bt_subtree_ref_hex=None if subtree_ref is None else text_hex(subtree_ref)))
        attribute_rows(element, uid, category, declared_id)
        for index, child in enumerate(children):
            walk(child, tree_id, uid, index, depth + 1)

    for tree in trees:
        identifier = tree.get("ID")
        children = list(tree)
        if len(children) != 1:
            raise ValueError("a declared tree needs exactly one root node")
        append(dict(bt_record_kind="TREE", bt_element_index=order[tree],
                         bt_tree_id_hex=text_hex(identifier), bt_node_uid=order[tree],
                         bt_main_tree_declared=int(identifier == main), bt_child_count=1))
        walk(children[0], identifier, order[tree], 0, 1)

    for identifier, targets in references.items():
        for target in targets:
            if target not in valid_ids:
                raise ValueError("subtree reference names no tree in this document")
    seen, stack = set(), []

    def visit(identifier):
        if identifier in stack:
            raise ValueError("cyclic subtree reference")
        if identifier in seen:
            return
        stack.append(identifier)
        for target in references.get(identifier, ()):
            visit(target)
        stack.pop()
        seen.add(identifier)

    for identifier in identifiers:
        visit(identifier)
    for identifier, (category, ports, section) in models.items():
        for name, (direction, port) in ports.items():
            append(dict(bt_record_kind="MODEL_PORT", bt_element_index=order[port],
                        bt_node_uid=order[section], bt_declared_id_hex=text_hex(identifier),
                        bt_node_category=category, bt_category_basis="DECLARED_MODEL_IN_THIS_DOCUMENT",
                        bt_attribute_name_hex=text_hex(name), bt_attribute_kind="DECLARED_MODEL_PORT",
                        bt_port_direction=direction))
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, ["record_time_us", "source_document_sha256", *FIELDS],
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(record_time_us=capture_time_us, source_document_sha256=digest,
                             bt_declaration_basis=BASIS,
                             **{key: "" if value is None else value for key, value in row.items()}))
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("declared output exceeds bound")
    report = dict(output_records=len(rows), declared_trees=len(identifiers),
                  declared_nodes=sum(1 for row in rows if row["bt_record_kind"] == "NODE"),
                  declared_models=len(models), main_tree_declared=main is not None,
                  source_sha256=digest, source_xml_hex=text_hex(text), clock="Unknown",
                  pin=PIN, scope="declared tree structure only; no execution, tick, plugin load "
                                "or task outcome; static port substitutions are declarations")
    return output.getvalue(), report


def convert_bundle(data, capture_time_us, *, main_tree_id=None):
    """Closed saved name -> XML map. Never opens an include path or evaluates a tree."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture time required")
    if not data or len(data) > BUNDLE_LIMIT:
        raise ValueError("empty or oversized saved bundle")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def name(value):
        if not isinstance(value, str) or not value or any(
                part in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                for part in value.split("/")):
            raise ValueError("unsupported noncanonical bundle name")
        return value

    manifest = json.loads(data, object_pairs_hook=unique_object)
    if not isinstance(manifest, dict) or set(manifest) != {"format", "entry", "files"} or \
            manifest["format"] != "btcpp4-saved-bundle-v1":
        raise ValueError("saved bundle manifest required")
    files = manifest["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("saved bundle file bound")
    encoded = {}
    for filename, xml in files.items():
        name(filename)
        if not isinstance(xml, str) or not xml or len(xml.encode("utf-8")) > LIMIT:
            raise ValueError("empty or oversized bundle XML")
        encoded[filename] = xml.encode("utf-8")
    entry = name(manifest["entry"])
    if entry not in files:
        raise ValueError("entry document missing")
    loaded, roots, edges, active = [], {}, [], set()

    def load(filename, depth):
        if filename in active:
            raise ValueError("include cycle")
        if filename in roots:
            raise ValueError("repeated inclusion unsupported")
        if filename not in encoded or depth > MAX_DEPTH:
            raise ValueError("missing include or include depth bound")
        root, _ = _parse_document(encoded[filename])
        roots[filename] = root
        active.add(filename)
        indexes = {element: index for index, element in enumerate(root.iter())}
        for include in root:
            if include.tag != "include":
                continue
            if set(include.attrib) != {"path"} or len(include) or (include.text or "").strip():
                raise ValueError("unsupported include declaration")
            relative = name(include.get("path"))
            parent = filename.rpartition("/")[0]
            target = parent + "/" + relative if parent else relative
            edges.append((filename, indexes[include], relative, target))
            load(target, depth + 1)
        active.remove(filename)
        loaded.append(filename)

    load(entry, 1)
    if sum(sum(1 for _ in root.iter()) for root in roots.values()) > MAX_NODES:
        raise ValueError("bundle element bound")
    tree_documents, models, references = {}, set(), {}
    for filename in loaded:
        root = roots[filename]
        for identifier in declared_models(root):
            if identifier in models:
                raise ValueError("duplicate bundle model ID")
            models.add(identifier)
        for tree in (child for child in root if child.tag == "BehaviorTree"):
            identifier = tree.get("ID")
            if not identifier or identifier in tree_documents or builtin_category(identifier):
                raise ValueError("missing, duplicate or registered bundle tree ID")
            tree_documents[identifier] = filename
            references[identifier] = [node.get("ID") for node in tree.iter() if node.tag == "SubTree"]
    if not 1 <= len(tree_documents) <= MAX_TREES or set(tree_documents) & models:
        raise ValueError("bundle tree bound or conflicting model/tree ID")
    seen, active = set(), set()

    def visit(identifier):
        if identifier not in tree_documents:
            raise ValueError("unresolved bundle subtree reference")
        if identifier in active:
            raise ValueError("cyclic bundle subtree reference")
        if identifier in seen:
            return
        active.add(identifier)
        for target in references[identifier]:
            visit(target)
        active.remove(identifier)
        seen.add(identifier)

    for identifier in tree_documents:
        visit(identifier)
    if main_tree_id is not None:
        selected, selection = main_tree_id, "CALLER_ARGUMENT"
    elif roots[entry].get("main_tree_to_execute") is not None:
        selected, selection = roots[entry].get("main_tree_to_execute"), "ENTRY_DECLARATION"
    elif len(tree_documents) == 1:
        selected, selection = next(iter(tree_documents)), "SOLE_LOADED_TREE"
    else:
        raise ValueError("ambiguous bundle root selection")
    if not isinstance(selected, str) or selected not in tree_documents:
        raise ValueError("selected tree is not loaded")

    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, ["record_time_us", "source_document_sha256", *FIELDS, *BUNDLE_FIELDS],
                            lineterminator="\n")
    writer.writeheader()
    documents, count, node_count = [], 0, 0

    def emit(filename, row):
        nonlocal count
        if count >= MAX_ROWS:
            raise ValueError("bundle row bound")
        writer.writerow(dict(row, record_time_us=capture_time_us,
            source_document_sha256="sha256:" + hashlib.sha256(encoded[filename]).hexdigest(),
            source_bundle_sha256=digest, bt_document_name_hex=text_hex(filename),
            bt_document_disposition="LOADED" if filename in roots else "UNREFERENCED_NOT_INTERPRETED",
            bt_declaration_basis=BASIS, bt_selection_basis=selection))
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("bundle output bound")
        count += 1

    for filename, raw in encoded.items():
        disposition = "LOADED" if filename in roots else "UNREFERENCED_NOT_INTERPRETED"
        documents.append(dict(name=filename, disposition=disposition,
            source_sha256="sha256:" + hashlib.sha256(raw).hexdigest(), source_xml_hex="hex:" + raw.hex()))
        emit(filename, dict(bt_record_kind="DOCUMENT"))
    for filename, index, relative, target in edges:
        emit(filename, dict(bt_record_kind="INCLUDE", bt_element_index=index,
            bt_attribute_value_hex=text_hex(relative), bt_include_target_hex=text_hex(target)))
    for filename in loaded:
        csv_text, report = _convert_document(encoded[filename], capture_time_us,
                                            bundle_tree_ids=tree_documents)
        node_count += report["declared_nodes"]
        for row in csv.DictReader(io.StringIO(csv_text)):
            if row["bt_record_kind"] == "TREE":
                row["bt_selected_tree"] = int(row["bt_tree_id_hex"] == text_hex(selected))
            if row["bt_subtree_ref_hex"]:
                identifier = bytes.fromhex(row["bt_subtree_ref_hex"][4:]).decode("utf-8")
                row["bt_subtree_document_hex"] = text_hex(tree_documents[identifier])
            emit(filename, row)
    return output.getvalue(), dict(source_sha256=digest, source_bundle_sha256=digest,
        source_bundle_hex="hex:" + data.hex(),
        entry=entry, selected_tree_id=selected, selection_basis=selection,
        input_files=len(files), loaded_files=len(loaded), unreferenced_files=len(files) - len(loaded),
        include_edges=len(edges), output_records=count, declared_trees=len(tree_documents),
        declared_nodes=node_count, declared_models=len(models), documents=documents,
        clock="Unknown", pin=PIN, scope=BASIS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller capture clock; the common clock basis stays Unknown")
    parser.add_argument("--bundle-json", action="store_true", help="closed saved XML include bundle")
    parser.add_argument("--main-tree-id", help="bundle-only declared root selection; never executes")
    args = parser.parse_args()
    try:
        if args.main_tree_id is not None and not args.bundle_json:
            raise ValueError("root override requires explicit bundle mode")
        with args.input.open("rb") as stream:
            data = stream.read((BUNDLE_LIMIT if args.bundle_json else LIMIT) + 1)
        if args.bundle_json:
            output, report = convert_bundle(data, args.capture_time_us, main_tree_id=args.main_tree_id)
        else:
            output, report = convert(data, args.capture_time_us)
        args.output_directory.mkdir()  # Refuse an existing output/evidence directory.
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError, ET.ParseError, RecursionError):
        parser.exit(2, "tree declaration conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
