#!/usr/bin/env python3
"""Passive, bounded ISOXML dynamic timelog -> reported actual-rate changes."""
import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET

LIMIT = 16 * 1024 * 1024
GRID_LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
PTN = {"A": "i", "B": "i", "C": "i", "D": "B", "E": "H", "F": "H", "G": "B", "H": "I", "I": "H"}
ACTUAL_RATES = {2: ("actual_volume_rate_l_ha", .0001),
                7: ("actual_mass_rate_kg_m2", .000001),
                12: ("actual_count_per_m2", .001)}
HARVEST = {84: ("reported_yield_mass_kg_m2", .000001),
           87: ("reported_yield_mass_kg_s", .000001),
           90: ("reported_yield_total_mass_kg", 1)}
HARVEST_FIELDS = [value[0] for value in HARVEST.values()] + [
    "reported_average_crop_moisture_ppm", "reported_average_crop_moisture_fraction",
    "reported_moisture_status"]
STATE_NAMES = ("DISABLED", "ENABLED", "ERROR", "UNDEFINED_OR_NOT_INSTALLED")
STATE_FIELDS = ["reported_work_state", "first_child_ordinal"] + [f"child_state_{i}" for i in range(1, 17)]
# The converter that produces these rows names itself, in the `musubi.<name>/vN` form this
# repository already uses for schema tags. The revision is a reported implementation and
# output-schema revision maintained here by this file's owner: revision 1 is the first revision in
# which this converter reports its own identity, and the number advances when the output schema or
# the conversion behaviour changes. It is not a build identifier, not a source-byte or installed
# package attestation, not a signature, and not a statement about the writer of the saved data.
APPLIED_CONVERTER_ID = "musubi.isoxml-rate/v1"
APPLIED_CONVERTER_REVISION = "1"
APPLIED_CONVERTER_FIELDS = ["applied_converter_id", "applied_converter_revision"]
APPLIED_CONVERTER_VALUES = [APPLIED_CONVERTER_ID, APPLIED_CONVERTER_REVISION]
APPLIED_CONVERTER_BASIS = (
    "THE_CONVERTER_THAT_PRODUCED_THESE_ROWS_NAMING_ITSELF_A_REPORTED_IDENTIFIER_AND_IMPLEMENTATION_"
    "REVISION_EMITTED_BY_THE_CODE_THAT_RAN_IT_IS_NOT_A_BUILD_IDENTIFIER_NOT_A_SOURCE_BYTE_OR_"
    "INSTALLED_PACKAGE_ATTESTATION_NOT_A_SIGNATURE_OR_RECEIPT_NOT_A_STATEMENT_THAT_ANY_PARTICULAR_"
    "FILE_ON_DISK_EXECUTED_AND_NOT_A_STATEMENT_ABOUT_THE_ORIGINAL_WRITER_OF_THE_SAVED_DATA_OR_ABOUT_"
    "THE_REFERENCE_SPECIFICATION_THIS_ADAPTATION_WAS_WRITTEN_AGAINST")
DESCRIPTION_LIMIT = 2 * 1024 * 1024
# Declared material attribution, from the task's own product allocation. Attribute letters are the
# pinned source's own, at the commit this case already pins -- AgGateway ISOv4Plugin
# 5bcf596c60fa11bcf638b308df478df906b965a5:
#   ISOModels/ISOProductAllocation.cs  PAN  A=ProductIdRef (required), B=QuantityDDI, C=QuantityValue,
#                                           D=TransferMode, E=DeviceElementIdRef (OPTIONAL),
#                                           F=ValuePresentationIdRef, child ASP=AllocationStamp
#   ISOModels/ISOProduct.cs            PDT  A=ProductId, B=ProductDesignator, C=ProductGroupRef,
#                                           D=ValuePresentationIdRef, E=QuantityDDI, F=ProductType
# A declared product is what the task document says was allocated. It is never chemical
# authentication, never proof that anything was physically applied, and is never inferred from a
# measured rate, a designator's wording, a prescription's intent or a file name.
PRODUCT_FIELDS = ["declared_product_id_hex", "declared_product_designator_hex",
                  "declared_product_group_ref_hex", "declared_product_type_code",
                  "declared_product_quantity_ddi", "declared_allocation_element_ref_hex",
                  "declared_allocation_transfer_mode_code", "declared_allocation_source_hex",
                  "product_allocation_basis"]
BOUND = ("ONE_PRODUCT_ALLOCATION_IN_THE_DECLARED_TASK_NAMES_THIS_DEVICE_ELEMENT_AND_ITS_PRODUCT_"
         "REFERENCE_RESOLVES_IN_THE_SAME_DOCUMENT_THE_PRODUCT_IS_WHAT_THAT_DOCUMENT_DECLARES_AND_IS_"
         "NEVER_CHEMICAL_AUTHENTICATION_NOR_PROOF_OF_PHYSICAL_APPLICATION")
NO_ALLOCATION = ("NO_PRODUCT_ALLOCATION_IN_THE_DECLARED_TASK_NAMES_THIS_DEVICE_ELEMENT_SO_NO_"
                 "MATERIAL_IS_ATTRIBUTED_AND_NONE_IS_INFERRED_FROM_THE_MEASURED_RATE")
TASK_SCOPED = ("A_PRODUCT_ALLOCATION_EXISTS_BUT_DECLARES_NO_DEVICE_ELEMENT_REFERENCE_WHICH_THE_"
               "PINNED_DEFINITION_MAKES_OPTIONAL_SO_ITS_SCOPE_IS_THE_TASK_AND_NOT_THIS_ELEMENT_AND_"
               "IT_IS_NOT_BOUND_HERE")
TIME_SCOPED = ("A_PRODUCT_ALLOCATION_NAMES_THIS_DEVICE_ELEMENT_BUT_CARRIES_AN_ALLOCATION_STAMP_"
               "WHOSE_TIME_SCOPE_THIS_CONVERTER_DOES_NOT_EVALUATE_SO_NOTHING_IS_BOUND_RATHER_THAN_"
               "BOUND_UNDER_A_SCOPE_THAT_WAS_NEVER_CHECKED")
AMBIGUOUS = ("MORE_THAN_ONE_PRODUCT_ALLOCATION_NAMES_THIS_DEVICE_ELEMENT_AND_NOTHING_IN_THE_"
             "DOCUMENT_CHOOSES_BETWEEN_THEM_SO_NO_PRODUCT_IS_SELECTED_AND_THE_FIRST_IS_NEVER_TAKEN")
DANGLING = ("THE_PRODUCT_ALLOCATION_NAMES_A_PRODUCT_REFERENCE_THAT_NO_PDT_IN_THE_SAME_DOCUMENT_"
            "DECLARES_THE_REFERENCE_IS_CARRIED_AS_REPORTED_AND_NO_NAME_TYPE_OR_GROUP_IS_INVENTED")
AMBIGUOUS_TASK = ("THE_DOCUMENT_DECLARES_MORE_THAN_ONE_TASK_AND_THE_TIMELOG_DOES_NOT_SAY_WHICH_ONE_"
                  "IT_BELONGS_TO_SO_NO_ALLOCATION_IS_READ_UNTIL_THE_CALLER_NAMES_THE_TASK")
NO_TASK = "THE_DOCUMENT_DECLARES_NO_TASK_SO_THERE_IS_NO_PRODUCT_ALLOCATION_TO_READ"
COEXISTENCE = ("AN_ALLOCATION_THAT_DECLARES_NO_DEVICE_ELEMENT_REFERENCE_COEXISTS_IN_THIS_TASK_WITH_"
               "ONE_THAT_NAMES_THIS_ELEMENT_AND_THE_PINNED_DEFINITIONS_STATE_NO_PRECEDENCE_BETWEEN_"
               "A_TASK_SCOPED_AND_AN_ELEMENT_SCOPED_ALLOCATION_SO_WHICH_ONE_APPLIES_HERE_IS_"
               "UNRESOLVED_AND_NOTHING_IS_BOUND_RATHER_THAN_THE_ELEMENT_SCOPED_ONE_BEING_ASSUMED")
# An enumeration code the pinned source writes as an integer (`((int)ProductType).ToString()`, and
# `WriteXmlAttribute<ISOTransferMode>`), and a DDI in the four-hex-digit form this converter already
# requires elsewhere. Checking the lexical form is also what keeps a comma or a newline out of a
# passthrough cell: the common reader refuses quoted input, so one stray delimiter would make the
# whole emitted file unreadable.
ENUM_CODE = re.compile(r"-?[0-9]{1,9}")


def declared_reference(value, what, *, required):
    """One declared ID reference, or None when the document did not state it at all.

    An attribute that is present but empty is NOT a reference. Left alone it would match another
    empty one and manufacture a resolvable product out of two blanks, so it is refused.
    """
    if value is None:
        if required:
            raise ValueError(f"{what} is required by the pinned definition and is absent")
        return None
    if value == "":
        raise ValueError(f"{what} is present but empty; an empty attribute is not an identifier "
                         "and is never matched against another empty one")
    return value


def declared_code(value, what):
    """A raw enumeration code, carried as its own number and never resolved to a meaning."""
    if value is None:
        return ""
    if not ENUM_CODE.fullmatch(value):
        raise ValueError(f"{what} is not the integer enumeration code the pinned source writes")
    return value


def declared_quantity_ddi(value, what):
    if value is None:
        return ""
    if not re.fullmatch(r"[0-9a-fA-F]{4}", value):
        raise ValueError(f"{what} is not a four-hex-digit DDI")
    return value


def product_allocations(root, declared_task=None):
    """Declared material attribution per device element, or an explicit reason there is none.

    Nothing is chosen when the document is ambiguous, nothing is filled in when a reference does
    not resolve, and no material is ever inferred from a measured quantity.
    """
    blank = [""] * (len(PRODUCT_FIELDS) - 1)
    tasks = root.findall("TSK")
    if declared_task is not None:
        tasks = [task for task in tasks if task.get("A") == declared_task]
        if not tasks:
            raise ValueError("the declared task is not in the saved TASKDATA")
    if not tasks:
        return {}, blank + [NO_TASK]
    if len(tasks) > 1:
        return {}, blank + [AMBIGUOUS_TASK]
    task = tasks[0]
    task_id = declared_reference(task.get("A"), "the task identifier TSK.A", required=True)
    products = {}
    for product in root.findall("PDT"):
        identifier = declared_reference(product.get("A"), "a product identifier PDT.A",
                                        required=True)
        if identifier in products:
            raise ValueError("duplicate product identifier")
        products[identifier] = product
    by_element, unscoped = {}, False
    for index, allocation in enumerate(task.findall("PAN")):
        # The pinned definition requires ProductIdRef and makes DeviceElementIdRef optional; an
        # empty spelling of either is malformed, not a reference.
        declared_reference(allocation.get("A"), "a product allocation reference PAN.A",
                           required=True)
        element = declared_reference(allocation.get("E"),
                                     "a product allocation element reference PAN.E", required=False)
        if element is None:
            unscoped = True  # Task-scoped by the pinned definition, never bound to one element.
            continue
        by_element.setdefault(element, []).append((index, allocation))
    resolved = {}
    for element, entries in by_element.items():
        if len(entries) > 1:
            resolved[element] = blank + [AMBIGUOUS + "_COUNT_%d" % len(entries)]
            continue
        index, allocation = entries[0]
        # The exact links this attribution rests on, so a reader can reopen every one of them.
        source = "hex:" + json.dumps(
            [["task", task_id], ["allocation_index", index],
             ["product_ref", allocation.get("A")]],
            separators=(",", ":"), ensure_ascii=False).encode("utf-8").hex()
        element_hex = "hex:" + element.encode().hex()
        mode = declared_code(allocation.get("D"), "the transfer mode PAN.D")
        if unscoped:
            # A task-scoped allocation may also cover this element, and nothing in the pinned
            # definitions ranks them, so uniqueness is not claimed.
            resolved[element] = ["", "", "", "", "", element_hex, mode, source, COEXISTENCE]
            continue
        if allocation.find("ASP") is not None:
            resolved[element] = ["", "", "", "", "", element_hex, mode, source, TIME_SCOPED]
            continue
        product = products.get(allocation.get("A"))
        common = [element_hex, mode, source]
        if product is None:
            resolved[element] = ["hex:" + allocation.get("A").encode().hex(),
                                 "", "", "", ""] + common + [DANGLING]
            continue
        designator = product.get("B")
        group = declared_reference(product.get("C"), "a product group reference PDT.C",
                                   required=False)
        resolved[element] = [
            "hex:" + product.get("A").encode().hex(),
            "hex:" + designator.encode().hex() if designator else "",
            "hex:" + group.encode().hex() if group else "",
            declared_code(product.get("F"), "the product type PDT.F"),
            declared_quantity_ddi(product.get("E"), "the product quantity DDI PDT.E"),
        ] + common + [BOUND]
    return resolved, blank + [TASK_SCOPED if unscoped else NO_ALLOCATION]


DEVICE_FIELDS = ["declared_device_id_hex", "declared_element_object_id", "declared_parent_object_id",
                 "declared_element_type", "declared_element_type_raw", "declared_element_number",
                 "declared_element_designator_hex", "device_description_sha256"]
# Declared geometry, read from DPT only. ISOv4Plugin 5bcf596c60fa11bcf638b308df478df906b965a5
# resolves a DET's properties as Device.DeviceProperties.Where(dpt => DOR.DeviceObjectId contains
# dpt.ObjectID) (ISOModels/ISODeviceElement.cs), and ObjectModel/DeviceElementHierarchy.cs takes
# widths from "0046"/"0044"/"0043" and offsets from "0086"/"0087"/"0088". Both
# Representation/UnitFactory.cs (intervals 51-70 and 134-136 -> ISOUnit("m", 0.001, 0)) and
# Resources/ddiExport.txt (entities 67/68/70/134/135/136: "Unit: mm - Length", "Resolution: 1")
# make the raw integer millimetres. The plugin's width priority (0046 over 0044 over 0043) is NOT
# reproduced: three differently defined declarations must not collapse into one "the width".
GEOMETRY = {67: ("declared_actual_working_width_m", 0, 2**31 - 1),
            68: ("declared_default_working_width_m", 0, 2**31 - 1),
            70: ("declared_maximum_working_width_m", 0, 2**31 - 1),
            134: ("declared_element_offset_x_m", -2**31, 2**31 - 1),
            135: ("declared_element_offset_y_m", -2**31, 2**31 - 1),
            136: ("declared_element_offset_z_m", -2**31, 2**31 - 1)}
MILLIMETRE = Decimal("0.001")
# The only frame statement in the pinned material is dictionary text, ddiExport.txt DD Entity 180:
# "All device element offsets refer to the DRP which is the centre of the device coordinate system
# and usually not at the ground surface." That names the reference point and says nothing else - no
# axis sign convention, no rotation, no ground datum - and the plugin itself applies no transform.
GEOMETRY_BASIS = ("DECLARED_MILLIMETRE_WIDTHS_AND_OFFSETS_CONVERTED_AT_THE_DICTIONARY_RESOLUTION_OF_"
                  "ONE_MILLIMETRE_REFERRED_TO_THE_DEVICE_REFERENCE_POINT_NAMED_BY_DDI180_WHICH_"
                  "STATES_ONLY_THAT_IT_IS_THE_CENTRE_OF_THE_DEVICE_COORDINATE_SYSTEM_AND_USUALLY_"
                  "NOT_AT_THE_GROUND_SURFACE_SO_NO_AXIS_SIGN_CONVENTION_NO_ROTATION_NO_GROUND_"
                  "DATUM_AND_NO_ABSOLUTE_OR_PHYSICAL_SECTION_POSITION_IS_ESTABLISHED_HERE")
# ISODeviceProperty declares E and the plugin never dereferences it (unlike ISODeviceProcessData,
# which resolves its own F on read); scaling comes from the fixed DDI table above.
PRESENTATION_RETAINED = ("DECLARED_DEVICE_VALUE_PRESENTATION_REFERENCE_RETAINED_NOT_APPLIED_BECAUSE_"
                         "THE_PINNED_DECODER_NEVER_DEREFERENCES_A_DEVICE_PROPERTYS_PRESENTATION_AND_"
                         "SCALES_THESE_DDIS_BY_THE_FIXED_MILLIMETRE_TABLE_INSTEAD")
GEOMETRY_FIELDS = [name for name, _, _ in GEOMETRY.values()] + [
    "declared_geometry_raw_mm", "declared_geometry_property_ids", "declared_geometry_reference_basis",
    "declared_geometry_value_presentation", "declared_geometry_value_presentation_disposition",
    "declared_referenced_property_count", "declared_unselected_property_ddis",
    "declared_unresolved_reference_ids", "declared_referenced_process_data_count"]
# The root element carries what the file declares about its own transfer, which is the only
# in-file evidence of which side wrote it. ISOv4Plugin 5bcf596c60fa11bcf638b308df478df906b965a5
# declares these attributes in ISOModels/ISO11783_TaskData.cs, the origin enumeration in
# ISOEnumerations/ISOTaskDataTransferOrigin.cs (FMIS = 1, MICS = 2), and Mappers/TaskDataMapper.cs
# writes VersionMajor/Minor 4/2 with DataTransferOrigin defaulted to FMIS on export. These are the
# file's own declarations: they authenticate nothing and certify no conformance role.
TRANSFER_ORIGINS = {1: "FMIS", 2: "MICS"}
TRANSFER_FIELDS = ["declared_transfer_origin_code", "declared_transfer_origin",
                   "declared_management_software_hex", "declared_management_software_version_hex",
                   "declared_task_controller_hex", "declared_task_controller_version_hex",
                   "declared_transfer_provenance_basis"]
TRANSFER_BASIS = ("DECLARED_BY_THE_TASKDATA_ROOT_ITSELF_NOT_AUTHENTICATED_NOT_A_CONFORMANCE_ROLE_"
                  "AND_NOT_EVIDENCE_THAT_ANY_TASK_CONTROLLER_WAS_CERTIFIED")
TRANSFER_ATTRIBUTES = (("ManagementSoftwareManufacturer", "ManagementSoftwareVersion"),
                       ("TaskControllerManufacturer", "TaskControllerVersion"))


def transfer_provenance(root):
    """What the root declares about its own transfer; an absent attribute stays blank."""
    code = root.get("DataTransferOrigin")
    if code is None:
        origin_code, origin = "", ""
    else:
        if not re.fullmatch(r"[0-9]{1,2}", code):
            raise ValueError("invalid declared transfer origin")
        # An undocumented code keeps its number rather than being guessed into FMIS or MICS.
        origin_code, origin = int(code), TRANSFER_ORIGINS.get(int(code), "UNKNOWN_CODE_RETAINED")
    values = [origin_code, origin]
    for pair in TRANSFER_ATTRIBUTES:
        for name in pair:
            declared = root.get(name)
            values.append("" if declared is None else "hex:" + declared.encode("utf-8").hex())
    return values + [TRANSFER_BASIS]
# Setpoint counterparts of the adopted actual DDIs, ISOBUS dictionary revision1, range
# 0..2147483647: DDI1 0,01 mm3/m2 (entity14), DDI6 1 mg/m2 (entity21), DDI11 0,001 /m2 (entity24).
SETPOINT_RATES = {1: ("setpoint_volume_rate_l_ha", .0001),
                  6: ("setpoint_mass_rate_kg_m2", .000001),
                  11: ("setpoint_count_per_m2", .001)}
# The pinned decoder converts grid values with BitConverter, i.e. host order; the byte order is not
# stated as a rule anywhere it can be read, so the caller declares where the order comes from.
GRID_BYTE_ORDER = "little-endian-pinned-decoder"
# The pinned decoder's own Examples/RasterPrescriptions.cs states and enacts the cell order:
# "Rates ... are in a simple list, starting at the SW cell and going row-by-row left to right",
# with the row loop outside the column loop and Origin commented "SW Corner". That is the decoder
# family's documented convention, not a normative clause and not an enforced test.
CELL_ORDER_BASIS = "ROW_MAJOR_NORTHWARD_FROM_MINIMUM_CORNER_PER_DECODER_EXAMPLE_NOT_NORMATIVE"
COORDINATE_BASIS = "DECLARED_DEGREES_NO_DATUM_IN_SOURCE_NOT_PROJECTED"
POSITION_BASIS = "RELATIVE_TO_DECLARED_GRID_ORIGIN_NOT_AN_ABSOLUTE_FIX"
PRESCRIPTION_FIELDS = [
    "record_time_us", "task_id_hex", "task_designator_hex", "grid_type", "grid_cell_index",
    "grid_cell_count", "declared_grid_file_ref_hex", "grid_cell_row", "grid_cell_column",
    "declared_cell_north_offset_deg",
    "declared_cell_east_offset_deg", "cell_position_basis", "treatment_zone_code",
    "treatment_zone_designator_hex", "treatment_zone_role", "process_data_index", "source_ddi",
    "source_raw_value", "source_cell_hex",
    "source_value_origin"] + [name for name, _ in SETPOINT_RATES.values()] + [
    "prescription_value_status", "declared_product_ref_hex", "declared_device_element_ref_hex",
    "declared_value_presentation_ref_hex", "value_presentation_disposition",
    "declared_grid_minimum_north_deg", "declared_grid_minimum_east_deg",
    "declared_grid_cell_north_size_deg", "declared_grid_cell_east_size_deg",
    "declared_grid_columns", "declared_grid_rows", "grid_cell_order_basis",
    "grid_coordinate_basis", "grid_value_byte_order", "device_description_sha256", "grid_sha256",
    "applied_converter_id", "applied_converter_revision"]


def declared_identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 14:
        raise ValueError("invalid declared device/element ID")
    return value


def declared_integer(value, low, high):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value) or len(value) > 5:
        raise ValueError("invalid description integer")
    number = int(value)
    if not low <= number <= high:
        raise ValueError("description integer out of range")
    return number


def task_data_root(data):
    """The single qualification and guard both TASKDATA readers use."""
    if not data or len(data) > DESCRIPTION_LIMIT:
        raise ValueError("empty or oversized device description")
    text = data.decode("utf-8-sig")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper() or "\x00" in text:
        raise ValueError("device description DTD/entities not accepted")
    root = ET.fromstring(text)
    if root.tag != "ISO11783_TaskData" or (root.get("VersionMajor"), root.get("VersionMinor")) != ("4", "2"):
        raise ValueError("qualified ISOXML4.2 description required")
    return root


def declared_property(node):
    """One DPT exactly as written: A object id, B DDI, C value, D designator, E presentation."""
    if not set(node.attrib) <= {"A", "B", "C", "D", "E"}:
        raise ValueError("unsupported device property attribute")
    ddi = node.get("B")
    if not isinstance(ddi, str) or not re.fullmatch(r"[0-9a-fA-F]{4}", ddi):
        raise ValueError("invalid declared property DDI")
    value = node.get("C")
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]{1,10}", value):
        raise ValueError("invalid declared property value")
    value = int(value)
    if not -2**31 <= value <= 2**31 - 2:  # The pinned validator's own range for C.
        raise ValueError("declared property value out of range")
    designator = node.get("D")
    if designator is not None and len(designator) > 32:
        raise ValueError("oversized property designator")
    presentation = node.get("E")
    return ddi, value, None if presentation is None else declared_integer(presentation, 1, 65534)


def object_references(element):
    """A DET's only child elements are DOR, each carrying exactly one attribute."""
    references = []
    for child in element:
        if child.tag != "DOR":
            raise ValueError("unsupported device element child")
        if set(child.attrib) != {"A"}:
            raise ValueError("unsupported device object reference attribute")
        reference = declared_integer(child.get("A"), 1, 65534)
        if reference in references:  # The pinned decoder dedupes silently; a repeat is malformed.
            raise ValueError("duplicate device object reference")
        references.append(reference)
    return references


def declared_geometry(references, properties, process_data, report):
    """Declared widths/offsets for one element; a declaration, never a physical position."""
    selected, unselected, unresolved, process_count = {}, [], [], 0
    for reference in references:
        if reference in properties:
            ddi, value, presentation = properties[reference]
            number = int(ddi, 16)
            if number not in GEOMETRY:
                unselected.append(ddi)
                continue
            if number in selected:
                # The pinned decoder groups by DDI and keeps the first; silently choosing between
                # two declarations of the same property is not something this converter will do.
                raise ValueError("duplicate declared geometry property")
            _, low, high = GEOMETRY[number]
            if not low <= value <= high:
                raise ValueError("declared geometry outside the published DDI range")
            selected[number] = (ddi, value, presentation, reference)
        elif reference in process_data:
            process_count += 1  # The reported side of the same element, deliberately not merged.
        else:
            unresolved.append(reference)
    report["declared_geometry_properties"] += len(selected)
    report["declared_geometry_elements"] += bool(selected)
    report["declared_unselected_properties"] += len(unselected)
    report["declared_unresolved_references"] += len(unresolved)
    report["declared_geometry_value_presentations"] += sum(
        item[2] is not None for item in selected.values())
    cells = ["" if number not in selected else Decimal(selected[number][1]) * MILLIMETRE
             for number in GEOMETRY]
    pairs = sorted(selected.items())
    return cells + [
        ";".join(f"{ddi}={value}" for _, (ddi, value, _, _) in pairs),
        ";".join(f"{ddi}={object_id}" for _, (ddi, _, _, object_id) in pairs),
        GEOMETRY_BASIS if selected else "",
        ";".join(f"{ddi}={item}" for _, (ddi, _, item, _) in pairs if item is not None),
        PRESENTATION_RETAINED if any(item[2] is not None for item in selected.values())
        else "NOT_DECLARED",
        len(selected) + len(unselected), ";".join(unselected),
        ";".join(str(item) for item in unresolved), process_count]


def device_references(data, *, geometry=False, report=None):
    root = task_data_root(data)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    result, device_ids = {}, set()
    kinds = {1: "DEVICE", 2: "FUNCTION", 3: "BIN", 4: "SECTION", 5: "UNIT", 6: "CONNECTOR", 7: "NAVIGATION"}
    identifier, integer = declared_identifier, declared_integer
    for device in root.findall("DVC"):
        device_id = identifier(device.get("A"))
        if device_id in device_ids:
            raise ValueError("duplicate device ID")
        device_ids.add(device_id)
        objects, parents = set(), {}
        properties, process_data, pending = {}, set(), []
        for node in device:
            if node.tag not in ("DET", "DPD", "DPT", "DVP"):
                continue  # Unselected descriptions remain in original bytes.
            object_id = integer(node.get("B" if node.tag == "DET" else "A"), 1, 65534)
            if object_id in objects:
                raise ValueError("duplicate local device object ID")
            objects.add(object_id)
            if geometry and node.tag == "DPT":
                properties[object_id] = declared_property(node)
            if geometry and node.tag == "DPD":
                process_data.add(object_id)
            if node.tag != "DET":
                continue
            element_id = identifier(node.get("A"))
            if element_id in result:
                raise ValueError("duplicate element ID")
            parent = integer(node.get("F"), 0, 65534)
            kind = integer(node.get("C"), 0, 65534)
            number = integer(node.get("E"), 0, 4095)
            designator = node.get("D")
            if designator is not None and len(designator) > 32:
                raise ValueError("oversized element designator")
            parents[object_id] = parent
            result[element_id] = ["hex:" + device_id.encode().hex(), object_id, parent,
                kinds.get(kind, "UNKNOWN_RETAINED"), kind, number,
                "" if designator is None else "hex:" + designator.encode().hex(), digest]
            if geometry:
                pending.append((element_id, object_references(node)))
        # Object IDs are scoped to their DVC, and DPD/DPT may be written after the DET that refers
        # to them, so references resolve once the whole device has been read.
        for element_id, item in pending:
            result[element_id] += declared_geometry(item, properties, process_data, report)
        if any(parent != 0 and parent not in parents for parent in parents.values()):
            raise ValueError("missing local parent element")
        done = {0}
        for start in parents:
            current, path = start, set()
            while current not in done:
                if current in path:
                    raise ValueError("cyclic device element hierarchy")
                path.add(current)
                current = parents[current]
            done.update(path)
    if not result:
        raise ValueError("no device elements in description")
    return result, digest


def declared_decimal(value, low, high):
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?[0-9]{1,12}(\.[0-9]{1,12})?", value):
        raise ValueError("invalid declared grid decimal")
    number = float(value)
    if not low <= number <= high:
        raise ValueError("declared grid decimal out of range")
    return value, number


def grid_dimension(value):
    # The pinned decoder's own range; the exact size match against the bounded binary is what
    # actually limits the work, so no lower cell cap is invented here.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
        raise ValueError("invalid declared grid dimension")
    number = int(value)
    if not 1 <= number <= 4294967294:
        raise ValueError("declared grid dimension out of range")
    return number


def treatment_zones(task, report):
    """TZN A/B/C with their direct-child PDV, in document order, keyed by declared zone code."""
    zones = {}
    for node in task.findall("TZN"):
        if not set(node.attrib) <= {"A", "B", "C"}:
            raise ValueError("unsupported treatment zone attribute")
        code = declared_integer(node.get("A"), 0, 254)
        if code in zones:
            raise ValueError("duplicate treatment zone code")
        designator = node.get("B")
        if designator is not None and len(designator) > 32:
            raise ValueError("oversized treatment zone designator")
        if node.get("C") is not None:
            declared_integer(node.get("C"), 0, 254)
        variables = []
        for child in node:
            if child.tag == "PLN":
                report["unread_polygon_declarations"] += 1  # Vector zones stay unread, not silent.
                continue
            if child.tag != "PDV":
                raise ValueError("unsupported treatment zone child")
            if not set(child.attrib) <= {"A", "B", "C", "D", "E", "F", "G"}:
                raise ValueError("unsupported process data attribute")
            ddi = child.get("A")
            if not isinstance(ddi, str) or not re.fullmatch(r"[0-9a-fA-F]{4}", ddi):
                raise ValueError("invalid DDI")
            value = child.get("B")
            if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]{1,10}", value):
                raise ValueError("invalid declared process data value")
            value = int(value)
            if not -2**31 <= value <= 2**31 - 2:
                raise ValueError("declared process data value out of range")
            references = [declared_identifier(child.get(key)) if child.get(key) is not None else None
                          for key in ("C", "D", "E")]
            report["unread_pdv_attributes"] += sum(child.get(key) is not None for key in ("F", "G"))
            report["nested_process_data_variables"] += len(child.findall("PDV"))
            if references[2] is not None:
                report["value_presentation_declarations"] += 1
            variables.append((int(ddi, 16), value, references))
        zones[code] = (designator, variables)
    if not zones:
        raise ValueError("no declared treatment zone")
    report["zones_declared"] = len(zones)
    return zones


def prescriptions(description, grid, *, capture_time_us, byte_order, grid_name):
    """Passive ingestion of declared per-cell setpoints; an intended rate is not an application."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("invalid caller capture clock")
    if byte_order != GRID_BYTE_ORDER:
        raise ValueError("grid cell value byte order must be declared")
    if not grid or len(grid) > GRID_LIMIT:
        raise ValueError("empty or oversized grid binary")
    root = task_data_root(description)
    report = {"grid_type": 0, "declared_cells": 0, "cells_read": 0, "prescription_values": 0,
              "unsupported_values": 0, "zones_declared": 0, "zones_used": 0,
              "unused_zone_codes": [], "zone_codes_without_process_data": [],
              "unread_polygon_declarations": 0, "nested_process_data_variables": 0,
              "unread_pdv_attributes": 0, "value_presentation_declarations": 0,
              "device_description_hex": description.hex(), "grid_bytes": len(grid),
              "clock": "Unknown", "actual_application_records": "NOT_OBSERVED",
              "applied_converter_id": APPLIED_CONVERTER_ID,
              "applied_converter_revision": APPLIED_CONVERTER_REVISION,
              "applied_converter_basis": APPLIED_CONVERTER_BASIS}
    digest = "sha256:" + hashlib.sha256(description).hexdigest()
    grid_digest = "sha256:" + hashlib.sha256(grid).hexdigest()
    report.update(device_description_sha256=digest, grid_sha256=grid_digest)
    tasks = [task for task in root.findall("TSK") if task.find("GRD") is not None]
    if len(tasks) != 1:
        raise ValueError("requires exactly one gridded task")
    task = tasks[0]
    nodes = task.findall("GRD")
    if len(nodes) != 1:  # The decoder takes the first and drops the rest without a word.
        raise ValueError("duplicate grid in task")
    node = nodes[0]
    if not {"A", "B", "C", "D", "E", "F", "G", "I"} <= set(node.attrib) <= set("ABCDEFGHIJ"):
        raise ValueError("unsupported grid attribute set")
    north, _ = declared_decimal(node.get("A"), -90, 90)
    east, _ = declared_decimal(node.get("B"), -180, 180)
    cell_north, north_size = declared_decimal(node.get("C"), 0, 1)
    cell_east, east_size = declared_decimal(node.get("D"), 0, 1)
    if north_size <= 0 or east_size <= 0:
        raise ValueError("declared grid cell size is not positive")
    columns, rows = grid_dimension(node.get("E")), grid_dimension(node.get("F"))
    name = node.get("G")
    # Never a path from the document: a plain name is compared with the supplied file, never opened.
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", name):
        raise ValueError("invalid declared grid file name")
    if not isinstance(grid_name, str) or grid_name.lower() != name.lower():
        raise ValueError("supplied grid binary does not match the declared grid file name")
    if node.get("H") is not None:
        if not re.fullmatch(r"[0-9]{1,10}", node.get("H")) or int(node.get("H")) != len(grid):
            raise ValueError("declared grid file length does not match the supplied binary")
    kind = declared_integer(node.get("I"), 1, 2)
    zone_reference = None if node.get("J") is None else declared_integer(node.get("J"), 0, 254)
    task_id = declared_identifier(task.get("A"))
    task_designator = task.get("B")
    if task_designator is not None and len(task_designator) > 32:
        raise ValueError("oversized task designator")
    roles = {}
    for key, role in (("H", "DECLARED_DEFAULT_ZONE"), ("I", "DECLARED_POSITION_LOST_ZONE"),
                      ("J", "DECLARED_OUT_OF_FIELD_ZONE")):
        if task.get(key) is not None:
            roles.setdefault(declared_integer(task.get(key), 0, 254), []).append(role)
    zones = treatment_zones(task, report)
    cells = columns * rows
    report.update(grid_type=kind, declared_cells=cells)
    if kind == 1:
        expected, template_zone = cells, None
    else:
        if zone_reference is None:
            raise ValueError("a type2 grid requires its treatment zone reference")
        if zone_reference not in zones:
            raise ValueError("grid references an undeclared treatment zone")
        template_zone = zones[zone_reference]
        if not template_zone[1]:
            raise ValueError("a type2 treatment zone requires process data variables")
        expected = cells * 4 * len(template_zone[1])
    if len(grid) != expected:
        raise ValueError("grid binary size does not match the declared dimensions")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")

    def write(row):
        # Every emitted row is bounded, including an accounted cell that carries no value.
        writer.writerow(row)
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("converted CSV exceeds bound")

    write(PRESCRIPTION_FIELDS)
    # The trailing group every cell row shares, including this converter naming itself.
    constant = ([CELL_ORDER_BASIS, COORDINATE_BASIS, byte_order, digest, grid_digest]
                + APPLIED_CONVERTER_VALUES)
    geometry = [north, east, cell_north, cell_east, columns, rows]
    used = set()
    for index in range(cells):
        if kind == 1:
            code = grid[index]
            if code not in zones:
                raise ValueError("grid cell references an undeclared treatment zone")
            designator, variables = zones[code]
            span = grid[index:index + 1]
        else:
            code, (designator, variables) = zone_reference, template_zone
            span = grid[index * 4 * len(variables):(index + 1) * 4 * len(variables)]
        used.add(code)
        report["cells_read"] += 1
        # Row-major from the minimum corner, per the decoder's own documented cell order. The
        # offsets are exact decimal multiples of the declared cell size and stay relative to the
        # declared origin: grid geometry, never an absolute fix.
        row, column = divmod(index, columns)
        zone_row = ["hex:" + task_id.encode().hex(),
                    "" if task_designator is None else "hex:" + task_designator.encode().hex(), kind,
                    index, cells, "hex:" + name.encode().hex(), row, column,
                    Decimal(cell_north) * row, Decimal(cell_east) * column,
                    POSITION_BASIS, code,
                    "" if designator is None else "hex:" + designator.encode().hex(),
                    "|".join(roles.get(code, ["DECLARED_ZONE"]))]
        if not variables:
            # A declared zone without process data still accounts for its cell.
            write([capture_time_us] + zone_row + ["", "", "", "hex:" + span.hex(),
                  "NOT_PROVIDED", "", "", "", "ZONE_DECLARES_NO_PROCESS_DATA", "", "", "",
                  "NOT_PROVIDED"] + geometry + constant)
            continue
        for ordinal, (ddi, declared, references) in enumerate(variables):
            if kind == 1:
                value, origin, raw = declared, "TREATMENT_ZONE_DECLARATION", span
            else:
                raw = span[ordinal * 4:(ordinal + 1) * 4]
                value, origin = struct.unpack("<i", raw)[0], "GRID_CELL_BINARY"
            if ddi in SETPOINT_RATES:
                if value < 0:
                    raise ValueError("selected DDI outside published range; no sentinel assumed")
                status = "SETPOINT_DECLARED"
                report["prescription_values"] += 1
            else:
                status = "UNKNOWN_DDI_NO_UNIT_ASSUMED"  # Never guess a unit for an unknown DDI.
                report["unsupported_values"] += 1
            write([capture_time_us] + zone_row + [ordinal, ddi, value, "hex:" + raw.hex(), origin]
                  + [value * scale if ddi == key and status == "SETPOINT_DECLARED" else ""
                     for key, (_, scale) in SETPOINT_RATES.items()]
                  + [status] + ["" if reference is None else "hex:" + reference.encode().hex()
                                for reference in references]
                  + ["NOT_PROVIDED" if references[2] is None else
                     "DECLARED_VALUE_PRESENTATION_NOT_APPLIED"] + geometry + constant)
    if report["cells_read"] != report["declared_cells"]:
        raise ValueError("declared and read cell counts disagree")
    if not report["prescription_values"] and not report["unsupported_values"]:
        raise ValueError("no declared prescription value")
    # A declared zone the grid never selects, and one that declares no process data, are named
    # rather than left to a difference of counts. Nothing declared disappears quietly.
    report["zones_used"] = len(used)
    report["unused_zone_codes"] = sorted(set(zones) - used)
    report["zone_codes_without_process_data"] = sorted(code for code, (_, items) in zones.items()
                                                       if not items)
    return output.getvalue(), report


def convert(template, data, *, harvest=False, located=False, work_state=False, taskdata=None,
            product_allocation=False, declared_task=None,
            section_geometry=False):
    if (product_allocation or declared_task is not None) and taskdata is None:
        raise ValueError("declared product allocation is read from the saved TASKDATA; "
                         "--product-allocation requires --taskdata")
    if section_geometry and taskdata is None:
        raise ValueError("declared geometry requires a saved device description")
    if not template or len(template) > 65536 or not data or len(data) > LIMIT:
        raise ValueError("empty or oversized timelog")
    # No XML DTD/entity resolution, alternate encodings or external resource references.
    text = template.decode("utf-8-sig")
    if "<!" in text or "\x00" in text:
        raise ValueError("unsupported XML declaration/entity")
    root = ET.fromstring(text)
    if root.tag != "TIM" or root.attrib != {"A": "", "D": "4"}:
        raise ValueError("requires dynamic effective TIM template")
    children = list(root)
    positions = [node for node in children if node.tag == "PTN"]
    columns = [node for node in children if node.tag == "DLV"]
    if len(positions) > 1 or not 1 <= len(columns) <= 255 or len(positions) + len(columns) != len(children):
        raise ValueError("unsupported timelog child layout")
    position = positions[0].attrib if positions else {}
    if type(located) is not bool or (located and not {"A", "B"}.issubset(position)):
        raise ValueError("located updates require dynamic north/east PTN")
    if any(key not in PTN or value != "" for key, value in position.items()):
        raise ValueError("requires dynamic supported PTN fields")
    position_size = sum(struct.calcsize(code) for key, code in PTN.items() if key in position)
    metadata = []
    for node in columns:
        if set(node.attrib) != {"A", "B", "C"} or node.attrib["B"] != "" or not node.attrib["C"]:
            raise ValueError("requires dynamic DLV with a device reference")
        if not re.fullmatch(r"[0-9a-fA-F]{4}", node.attrib["A"]):
            raise ValueError("invalid DDI")
        metadata.append((int(node.attrib["A"], 16), node.attrib["C"]))
    geometry_report = {name: 0 for name in (
        "declared_geometry_elements", "declared_geometry_properties", "declared_unselected_properties",
        "declared_unresolved_references", "declared_geometry_value_presentations",
        "reported_geometry_updates")}
    references, description_hash = device_references(
        taskdata, geometry=section_geometry, report=geometry_report) if taskdata is not None else ({}, None)
    provenance = transfer_provenance(task_data_root(taskdata)) if taskdata is not None else []
    allocations, allocation_absent = (
        product_allocations(task_data_root(taskdata), declared_task)
        if product_allocation else ({}, []))
    if taskdata is not None and any(device not in references for _, device in metadata):
        raise ValueError("timelog element is missing from device description")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "source_row", "source_update", "source_column", "device_element_hex",
                     "source_raw_value", "actual_volume_rate_l_ha", "source_record_hex",
                     "source_ddi", "actual_mass_rate_kg_m2", "actual_count_per_m2"] +
                    (HARVEST_FIELDS if harvest else []) +
                    (STATE_FIELDS if work_state else []) +
                    (["reported_latitude_deg", "reported_longitude_deg", "position_status_raw",
                      "position_disposition"] if located else [])
                    + ((DEVICE_FIELDS + (GEOMETRY_FIELDS if section_geometry else []) + TRANSFER_FIELDS)
                       if taskdata is not None else [])
                    + (PRODUCT_FIELDS if product_allocation else [])
                    + APPLIED_CONVERTER_FIELDS)
    report = {"source_records": 0, "source_updates": 0, "decoded_updates": 0,
              "unsupported_updates": 0, "records_without_supported_update": 0,
              "template_hex": template.hex(), "source_records_hex": [], "clock": "Unknown",
              "applied_converter_id": APPLIED_CONVERTER_ID,
              "applied_converter_revision": APPLIED_CONVERTER_REVISION,
              "applied_converter_basis": APPLIED_CONVERTER_BASIS}
    if taskdata is not None:
        report.update(device_description_hex=taskdata.hex(), device_description_sha256=description_hash,
                      declared_element_count=len(references))
    if section_geometry:
        report.update(geometry_report)
    offset, previous, report_bytes = 0, None, len(template) * 2
    while offset < len(data):
        start = offset
        if len(data) - offset < 7 + position_size:
            raise ValueError("truncated time/position/count")
        milliseconds, days = struct.unpack_from("<IH", data, offset)
        if milliseconds >= 86400000 or days == 65535:
            raise ValueError("invalid or unavailable local calendar time")
        timestamp = (days * 86400000 + milliseconds) * 1000
        if previous is not None and timestamp < previous:
            raise ValueError("decreasing local calendar time")
        previous = timestamp
        position_values = []
        if located:
            point, cursor = {}, offset + 6
            for key, code in PTN.items():
                if key in position:
                    point[key] = struct.unpack_from("<" + code, data, cursor)[0]
                    cursor += struct.calcsize("<" + code)
            missing = point["A"] == 2**31 - 1 or point["B"] == 2**31 - 1
            for key, limit in (("A", 900000000), ("B", 1800000000)):
                if point[key] != 2**31 - 1 and abs(point[key]) > limit:
                    raise ValueError("reported geographic coordinate outside range")
            position_values = ["" if missing else point["A"] / 10000000,
                               "" if missing else point["B"] / 10000000, point.get("D", ""),
                               "NOT_PROVIDED" if missing else "REPORTED_NOT_FIX_QUALIFIED"]
        offset += 6 + position_size
        count = data[offset]
        offset += 1
        if len(data) - offset < count * 5:
            raise ValueError("truncated DLV changes")
        changes = []
        for index in range(count):
            column, value = struct.unpack_from("<Bi", data, offset)
            offset += 5
            if column >= len(metadata):
                raise ValueError("DLV column has no template definition")
            changes.append((index, column, value))
        frame = data[start:offset].hex()
        report_bytes += len(frame) + 16
        if report_bytes > LIMIT:
            raise ValueError("source report exceeds bound")
        report["source_records_hex"].append(frame)
        report["source_records"] += 1
        selected = 0
        for index, column, value in changes:
            report["source_updates"] += 1
            ddi, device = metadata[column]
            state_selected = work_state and ddi in (141, 161, 162, 163)
            if ddi not in ACTUAL_RATES and not (harvest and (ddi in HARVEST or ddi == 262)) and not state_selected:
                report["unsupported_updates"] += 1
                if section_geometry and ddi in GEOMETRY:
                    # The pinned decoder fills missing geometry from timelog values. A logged value
                    # is a report, so it is counted here and never merged into the declaration.
                    report["reported_geometry_updates"] += 1
                continue
            if not state_selected and value < 0:
                raise ValueError("selected DDI outside published range; no sentinel assumed")
            state_values = [""] * len(STATE_FIELDS) if work_state else []
            if state_selected:
                if ddi == 141:
                    if not 0 <= value <= 3:
                        raise ValueError("actual work state outside published range")
                    state_values[0] = STATE_NAMES[value]
                else:
                    # TIM carries a signed Int32; these DDIs define its full bit pattern.
                    bits = value & 0xffffffff
                    state_values[1] = (ddi - 161) * 16 + 1
                    state_values[2:] = [STATE_NAMES[(bits >> (2 * i)) & 3] for i in range(16)]
            harvest_values = []
            if harvest:
                harvest_values = [value * scale if ddi == key else ""
                                  for key, (_, scale) in HARVEST.items()]
                # The published wire range exceeds a physical moisture fraction.
                # Preserve the report, but never clamp it into a plausible value.
                harvest_values += [value if ddi == 262 else "",
                    value / 1000000 if ddi == 262 and value <= 1000000 else "",
                    ("REPORTED" if value <= 1000000 else "OUTSIDE_FRACTION_RANGE") if ddi == 262 else ""]
            writer.writerow([timestamp, report["source_records"], index, column,
                             "hex:" + device.encode().hex(), value,
                             value * ACTUAL_RATES[2][1] if ddi == 2 else "", "hex:" + frame,
                             ddi, value * ACTUAL_RATES[7][1] if ddi == 7 else "",
                             value * ACTUAL_RATES[12][1] if ddi == 12 else ""] + harvest_values + state_values + position_values
                            + ((references[device] + provenance) if taskdata is not None else [])
                            + (allocations.get(device, allocation_absent)
                               if product_allocation else [])
                            + APPLIED_CONVERTER_VALUES)
            selected += 1
            report["decoded_updates"] += 1
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("converted CSV exceeds bound")
        if not selected:
            report["records_without_supported_update"] += 1
    if not report["decoded_updates"]:
        raise ValueError("no selected actual updates; setpoint is not actual")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path, help="dynamic TIM template, or ISOXML4.2 TASKDATA with --prescription")
    parser.add_argument("data", type=Path, help="binary timelog, or the declared grid binary with --prescription")
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--prescription", action="store_true",
                        help="read declared per-cell setpoints from TASKDATA TSK/GRD/TZN/PDV plus one grid binary")
    parser.add_argument("--capture-time-us", type=int,
                        help="caller capture clock for the untimed prescription; required with --prescription")
    parser.add_argument("--grid-byte-order", choices=[GRID_BYTE_ORDER],
                        help="declared source of the cell value byte order; required with --prescription")
    parser.add_argument("--harvest", action="store_true", help="include qualified yield and average-moisture reports")
    parser.add_argument("--position", action="store_true", help="attach reported same-record PTN north/east to updates")
    parser.add_argument("--work-state", action="store_true", help="include reported actual element/child work states")
    parser.add_argument("--taskdata", type=Path, help="resolve declared device elements from saved ISOXML4.2 TASKDATA")
    parser.add_argument("--product-allocation", action="store_true",
                        help="carry the task's declared product allocation beside the measured "
                             "rate; requires --taskdata and attributes no material by inference")
    parser.add_argument("--declared-task", help="name the TSK when the TASKDATA declares more than "
                                                "one; nothing is chosen for the caller")
    parser.add_argument("--section-geometry", action="store_true",
                        help="carry declared DPT widths/offsets of each element; requires --taskdata")
    args = parser.parse_args()
    try:
        if args.prescription:
            # Configuration that cannot apply to the selected input is refused, never ignored.
            if args.capture_time_us is None or args.grid_byte_order is None:
                raise ValueError("a prescription requires a declared capture clock and byte order")
            if (args.harvest or args.position or args.work_state or args.taskdata is not None
                    or args.section_geometry):
                raise ValueError("timelog options do not apply to a prescription")
            if args.template.stat().st_size > DESCRIPTION_LIMIT or args.data.stat().st_size > GRID_LIMIT:
                raise ValueError("input exceeds bound")
            output, report = prescriptions(args.template.read_bytes(), args.data.read_bytes(),
                                           capture_time_us=args.capture_time_us,
                                           byte_order=args.grid_byte_order, grid_name=args.data.stem)
            args.output_directory.mkdir()
            (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
            (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            return
        if args.capture_time_us is not None or args.grid_byte_order is not None:
            raise ValueError("prescription options require --prescription")
        if args.template.stat().st_size > 65536 or args.data.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        if args.taskdata is not None and args.taskdata.stat().st_size > DESCRIPTION_LIMIT:
            raise ValueError("device description exceeds bound")
        output, report = convert(args.template.read_bytes(), args.data.read_bytes(), harvest=args.harvest,
                                 located=args.position, work_state=args.work_state,
                                 taskdata=None if args.taskdata is None else args.taskdata.read_bytes(),
                                 product_allocation=args.product_allocation,
                                 declared_task=args.declared_task,
                                 section_geometry=args.section_geometry)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError, ET.ParseError, struct.error):
        parser.exit(2, "ISOXML rate conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
