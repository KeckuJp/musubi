#!/usr/bin/env python3
"""Saved VDA3.0 protocol declarations, not runtime capabilities or 3D decoding."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
try:
    from scripts import convert_ardupilot_battery_csv as existing
except ModuleNotFoundError:
    import convert_ardupilot_battery_csv as existing

STRINGS = ('maximumMessageLength','maximumTopicSerialLength','maximumTopicElementLength',
           'maximumIdLength','maximumLoadIdLength')
ARRAYS = ('order.nodes','order.edges','node.actions','edge.actions','actions.actionsParameters',
          'instantActions','trajectory.knotVector','trajectory.controlPoints','zoneSet.zones',
          'state.nodeStates','state.edgeStates','state.loads','state.actionStates',
          'state.instantActionStates','state.zoneActionStates','state.errors','state.information',
          'error.errorReferences','information.infoReferences')
TIMES = ('minimumOrderInterval','minimumStateInterval','defaultStateInterval','visualizationInterval')
FIELDS = ('record_time_us','source_record_index','source_record_sha256','declaration_path',
          'disposition','reported_integer','seconds','truth_value','value_basis','item_index',
          'envelope_id_hex','format_hex','data_status','url_hex','description_hex')


# The common reader (read_telemetry_csv) refuses an input over 16 MiB, so every CSV this route
# writes is held to that many UTF-8 bytes; an accepted output is always readable.
READER_INPUT_BYTES = 16*1024**2


class BoundedCsv:
    """CSV rows encoded one at a time and refused past the common reader's input bound in bytes."""
    def __init__(self, fields, refusal):
        self.buffer = io.StringIO(newline='')
        self.writer = csv.DictWriter(self.buffer, fields, lineterminator='\n')
        self.refusal, self.parts, self.size = refusal, [], 0
        self.writer.writeheader()
        self.flush()

    def writerow(self, row):
        self.writer.writerow(row)
        self.flush()

    def flush(self):
        chunk = self.buffer.getvalue()
        self.buffer.seek(0)
        self.buffer.truncate()
        self.size += len(chunk.encode('utf-8'))
        if self.size > READER_INPUT_BYTES:
            raise ValueError(self.refusal)
        self.parts.append(chunk)

    def getvalue(self):
        return ''.join(self.parts)


# VDA2.1 provenance: under the 2.1 selector each sidecar row keeps the ORIGINAL 2.1 path in
# declaration_path and names the 3.0 meaning it was read under; the source is never relabelled 3.0.
V21_COLUMNS = ('factsheet_version', 'vda_3_0_meaning_path')
V21_ONLY = {'maximumStringLengths.enumLen'}  # no 3.0 counterpart
# 3.0 meanings the 2.1.0 source has no key for: reported NOT_IN_VERSION, never absent, empty or false.
V21_NOT_IN_VERSION = ('protocolLimits.maximumArrayLengths.state.instantActionStates',
    'protocolLimits.maximumArrayLengths.state.zoneActionStates', 'protocolLimits.maximumArrayLengths.zoneSet.zones',
    'typeSpecification.supportedZones',
    'protocolFeatures.mobileRobotActions.pauseAllowed', 'protocolFeatures.mobileRobotActions.cancelAllowed',
    'mobileRobotConfiguration.batteryCharging')


def v21_source_paths(table=None, meaning='', source=''):
    """3.0 meaning path -> original 2.1 path, from the shared key table (list markers dropped)."""
    table = existing.FACTSHEET_21_KEYS if table is None else table
    paths = {}
    for key, entry in table.items():
        name, child = (entry, None) if isinstance(entry, str) else entry
        target, origin = meaning + name, source + key
        paths[target] = origin
        if child is not None:
            paths.update(v21_source_paths(child[1] if isinstance(child, tuple) else child, target + '.', origin + '.'))
    return paths


V21_PATHS = v21_source_paths()


def v21_provenance(row, prefix=''):
    """Rewrite a row's 3.0 meaning path to its original 2.1 path and record both."""
    meaning = row['declaration_path']
    full = prefix + meaning
    row['factsheet_version'] = '2.1.0'
    row['vda_3_0_meaning_path'] = 'NONE_VDA2_1_0_ONLY' if meaning in V21_ONLY else meaning
    if any(full == gap or full.startswith(gap + '.') for gap in V21_NOT_IN_VERSION):
        row['declaration_path'] = ''
        if str(row.get('disposition', '')).startswith(('ABSENT', 'NO_EXPLICIT_LIMIT_ABSENT')):
            row['disposition'] = 'NOT_IN_VERSION'
        return row
    match = max((path for path in V21_PATHS if full == path or full.startswith(path + '.')), key=len, default=None)
    if match is None:
        raise ValueError('no VDA2.1 source path for a declared meaning')
    source = V21_PATHS[match] + full[len(match):]
    row['declaration_path'] = source[len(prefix):] if prefix and source.startswith(prefix) else source
    return row


def text_hex(value):
    if not isinstance(value, str) or len(value.encode()) > 4096:
        raise ValueError('invalid declaration text')
    return 'hex:'+value.encode().hex()


def convert(text, source_format=existing.FACTSHEET_FORMAT):
    if source_format not in existing.FACTSHEET_FORMATS:
        raise ValueError('unsupported factsheet selector')
    v21 = source_format == existing.FACTSHEET_21_FORMAT
    base = existing.convert(text, source_format, vda_geometry=True)
    limit = csv.field_size_limit(2*1024**2)
    try:
        summaries = [r for r in csv.DictReader(io.StringIO(base)) if r['geometry_row_kind']=='SUMMARY']
    finally:
        csv.field_size_limit(limit)
    lines = text.splitlines()
    if len(lines) != len(summaries):
        raise ValueError('source accounting mismatch')
    output = BoundedCsv(FIELDS + (V21_COLUMNS if v21 else ()),
                        'declaration output exceeds the common reader input bound; split at complete records')
    count = unknown = three_d = 0
    for index, (line, summary) in enumerate(zip(lines, summaries)):
        record = json.loads(line, object_pairs_hook=existing.unique_object)
        if v21:
            record = existing.factsheet_21_view(record)
        common = dict(record_time_us=summary['record_time_us'], source_record_index=index,
                      source_record_sha256='sha256:'+hashlib.sha256(line.encode()).hexdigest())
        def emit(**values):
            nonlocal count
            row = dict(common, **values)
            if v21:
                v21_provenance(row, 'protocolLimits.' if not row['declaration_path'].startswith(
                    ('protocolLimits', 'mobileRobotGeometry')) else '')
            output.writerow(row)
            count += 1
        if 'protocolLimits' not in record:
            emit(declaration_path='protocolLimits', disposition='PROTOCOL_LIMITS_ABSENT',
                 value_basis='NO_DECLARATION_NOT_INFINITE_CAPABILITY')
        else:
            limits = record['protocolLimits']
            if not isinstance(limits,dict):
                raise ValueError('invalid protocol limits')
            unknown += len(set(limits)-{'maximumStringLengths','maximumArrayLengths','timing'})
            for group, keys in (('maximumStringLengths',STRINGS+(('enumLen',) if v21 else ())),
                                ('maximumArrayLengths',ARRAYS),('timing',TIMES)):
                values = limits.get(group)
                if not isinstance(values,dict):
                    raise ValueError('required protocol group missing')
                allowed = set(keys) | ({'idNumericalOnly'} if group=='maximumStringLengths' else set())
                unknown += len(set(values)-allowed)
                for key in keys:
                    row = dict(declaration_path=group+'.'+key)
                    if group=='timing':
                        row['value_basis']='s_DECLARED_MESSAGE_INTERVAL_NOT_MEASURED_RATE'
                        if key not in values:
                            if key in TIMES[:2]:
                                raise ValueError('required interval missing')
                            row['disposition']='DEFAULT_DOCUMENT_NOT_RESOLVED' if key=='defaultStateInterval' else 'UNSPECIFIED'
                        else:
                            value = values[key]
                            if type(value) not in (int,float) or not math.isfinite(value) or value<0 or (type(value)==int and value>2**53-1):
                                raise ValueError('invalid or inexact interval')
                            row.update(seconds=value,disposition='REPORTED_INTERVAL')
                    else:
                        row['value_basis']='ARRAY_ELEMENT_COUNT' if group=='maximumArrayLengths' else 'WRITER_LENGTH_UNIT_UNSPECIFIED'
                        if key not in values:
                            row['disposition']='NO_EXPLICIT_LIMIT_ABSENT'
                        else:
                            value=values[key]
                            if type(value) is not int or not 0<=value<=2**63-1:
                                raise ValueError('invalid count')
                            row.update(reported_integer=value, disposition='NO_EXPLICIT_LIMIT_REPORTED_ZERO' if value==0 else 'REPORTED_MAXIMUM')
                    emit(**row)
                if group=='maximumStringLengths':
                    value=values.get('idNumericalOnly')
                    if 'idNumericalOnly' in values and type(value) is not bool:
                        raise ValueError('invalid numerical ID constraint')
                    emit(declaration_path=group+'.idNumericalOnly', disposition='ABSENT_UNKNOWN' if value is None else 'REPORTED_BOOLEAN',
                         truth_value='' if value is None else str(value).lower(),value_basis='BOOLEAN_ID_STRING_CONSTRAINT')
        geometry=record.get('mobileRobotGeometry',{})
        envelopes=geometry.get('envelopes3d',[])
        if not isinstance(envelopes,list) or len(envelopes)>1024:
            raise ValueError('invalid 3D declaration list')
        if not envelopes:
            emit(declaration_path='mobileRobotGeometry.envelopes3d',
                 disposition='EMPTY_DECLARED_LIST' if 'envelopes3d' in geometry else 'ABSENT',
                 value_basis='DECLARATION_AVAILABILITY_NOT_GEOMETRY')
        for ordinal,item in enumerate(envelopes):
            if not isinstance(item,dict) or 'envelope3dId' not in item or 'format' not in item:
                raise ValueError('missing 3D identity/format')
            if 'data' in item and not isinstance(item['data'],dict):
                raise ValueError('invalid 3D data')
            # 2.1 schema types the 3D description as integer, its specification as string: an integer
            # is kept in the original only, flagged, never coerced to text.
            conflict = v21 and type(item.get('description')) is int
            emit(declaration_path='mobileRobotGeometry.envelopes3d',item_index=ordinal,
                 envelope_id_hex=text_hex(item['envelope3dId']),format_hex=text_hex(item['format']),
                 url_hex=text_hex(item['url']) if 'url' in item else '',
                 description_hex='' if conflict or 'description' not in item else text_hex(item['description']),
                 data_status='PRESENT_UNINTERPRETED' if 'data' in item else 'ABSENT',
                 disposition='UNINTERPRETED_FORMAT_UNIT_FRAME' + ('_DESCRIPTION_INTEGER_PER_2_1_SCHEMA_STRING_PER_SPEC_RETAINED_IN_SOURCE' if conflict else ''),
                 value_basis='DECLARED_REFERENCE_NOT_GEOMETRY' + ('_ENVELOPE_ID_IS_2_1_SET_NAME' if v21 else ''))
            three_d+=1
    report = dict(records=len(lines),sidecar_rows=count,
        uninterpreted_3d=three_d,unknown_protocol_properties=unknown,
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        base_csv_sha256=hashlib.sha256(base.encode()).hexdigest(),
        retention='Read BOTH CSV outputs; keep original JSON. 3D contents and unknown fields remain uninterpreted in original.')
    if v21:
        report.update(factsheet_selector=source_format, factsheet_version='2.1.0',
                      normalization='VDA2.1.0 keys read through an in-memory 3.0-named view; source, SHA and version unchanged')
    return base,output.getvalue(),report


# Opt-in typeSpecification/protocolFeatures/loadSpecification/mobileRobotConfiguration leaves, from the same
# pin's schema and VDA5050_EN.md tables. Where they disagree the row records which form was reported; the
# conflicting load-set speed unit is resolved only by the caller's explicit --load-speed-unit-policy.
CAPABILITY_FIELDS = ('record_time_us','source_record_index','source_record_sha256','declaration_path',
    'item_index','sub_index','disposition','value_hex','enum_status','truth_value','length_m','angle_rad',
    'mass_kg','speed_m_s','acceleration_m_s2','duration_s','percent','coordinate_unit_not_stated','unit_conflict_value','value_basis')
EXTENSIBLE = {'mobileRobotKinematics':('DIFFERENTIAL','OMNIDIRECTIONAL','THREE_WHEEL'),
    'mobileRobotClass':('FORKLIFT','CONVEYOR','TUGGER','CARRIER'),
    'localizationTypes':('NATURAL','REFLECTOR','RFID','DMC','SPOT','GRID'),
    'navigationTypes':('PHYSICAL_LINE_GUIDED','VIRTUAL_LINE_GUIDED','FREELY_NAVIGATING')}
CLOSED = {'supportedZones':('BLOCKED','LINE_GUIDED','RELEASE','COORDINATED_REPLANNING','SPEED_LIMIT','ACTION',
    'PRIORITY','PENALTY','DIRECTED','BIDIRECTED'), 'support':('SUPPORTED','REQUIRED'),
    'actionScopes':('INSTANT','NODE','EDGE','ZONE'), 'blockingTypes':('NONE','SOFT','SINGLE','HARD'),
    'valueDataType':('BOOL','NUMBER','INTEGER','STRING','OBJECT','ARRAY')}
# VDA2.1.0 511d01d7 tokens, never mapped onto 3.0 tokens (DIFF is not DIFFERENTIAL, AUTONOMOUS is not
# FREELY_NAVIGATING). The 2.1 specification calls its type lists example values, hence extensible;
# blockingTypes NONE/SOFT/HARD comes from its specification table (its schema states no enum).
EXTENSIBLE_21 = {'mobileRobotKinematics':('DIFF','OMNI','THREEWHEEL'),
    'mobileRobotClass':('FORKLIFT','CONVEYOR','TUGGER','CARRIER'),
    'localizationTypes':('NATURAL','REFLECTOR','RFID','DMC','SPOT','GRID'),
    'navigationTypes':('PHYSICAL_LINE_GUIDED','VIRTUAL_LINE_GUIDED','AUTONOMOUS')}
CLOSED_21 = {'support':('SUPPORTED','REQUIRED'), 'actionScopes':('INSTANT','NODE','EDGE'),
    'blockingTypes':('NONE','SOFT','HARD'),
    'valueDataType':('BOOL','NUMBER','INTEGER','FLOAT','STRING','OBJECT','ARRAY')}
# (key, column, minimum zero) for loadSet quantities; units from the specification's loadSet table.
LOAD_QUANTITIES = (('maximumWeight','mass_kg',True),('minimumLoadhandlingHeight','length_m',True),
    ('maximumLoadhandlingHeight','length_m',True),('minimumLoadhandlingDepth','length_m',False),
    ('maximumLoadhandlingDepth','length_m',False),('minimumLoadhandlingTilt','angle_rad',False),
    ('maximumLoadhandlingTilt','angle_rad',False),('maximumSpeed','speed_m_s',True),
    ('maximumAcceleration','acceleration_m_s2',True),('maximumDeceleration','acceleration_m_s2',False),
    ('pickTime','duration_s',True),('dropTime','duration_s',True))
# loadSet.maximumSpeed: the pinned schema says m/s^2, the specification table says m/s. Neither is assumed;
# 'unresolved' keeps the number unit-free, 'spec-text' is the caller's explicit choice of the m/s reading.
LOAD_SPEED_POLICIES = ('unresolved', 'spec-text')
KNOWN_KEYS = {'typeSpecification':{'seriesName','seriesDescription','mobileRobotKinematics','mobileRobotKinematic',
        'mobileRobotClass','maximumLoadMass','localizationTypes','navigationTypes','supportedZones'},
    'protocolFeatures':{'optionalParameters','mobileRobotActions'},
    'loadSpecification':{'loadPositions','loadSets'},
    'mobileRobotConfiguration':{'versions','network','batteryCharging','batteryCharging '},
    # Nested objects that this sidecar maps: every one is accounted, not only the four sections.
    'optionalParameters[]':{'parameter','support','description'},
    'mobileRobotActions[]':{'actionType','actionDescription','actionScopes','actionParameters','actionResult',
        'blockingTypes','pauseAllowed','cancelAllowed'},
    'actionParameters[]':{'key','valueDataType','description','isOptional'},
    'loadSets[]':{'setName','loadType','loadPositions','boundingBoxReference','loadDimensions','description',
        *(key for key, _, _ in LOAD_QUANTITIES)},
    'boundingBoxReference':{'x','y','z','theta'}, 'loadDimensions':{'length','width','height'},
    'versions[]':{'key','value'},
    'network':{'dnsServers','ntpServers','localIpAddress','netmask','defaultGateway'},
    'batteryCharging':{'criticalLowChargingLevel','minimumDesiredChargingLevel','maximumDesiredChargingLevel',
        'minimumChargingTime'}}
KNOWN_KEYS['batteryCharging '] = KNOWN_KEYS['batteryCharging']


def number(value, minimum=None, maximum=None):
    if type(value) not in (int,float) or not math.isfinite(value) or (minimum is not None and value<minimum) \
            or (maximum is not None and value>maximum):
        raise ValueError('invalid declared number')
    return value


def convert_capabilities(text, load_speed_policy='unresolved', source_format=existing.FACTSHEET_FORMAT):
    """Declared type, protocol, load and configuration leaves; never runtime capability or certification."""
    if load_speed_policy not in LOAD_SPEED_POLICIES:
        raise ValueError('unknown load-set speed unit policy')
    base,_,_ = convert(text, source_format)
    v21 = source_format == existing.FACTSHEET_21_FORMAT
    extensible, closed = (EXTENSIBLE_21, CLOSED_21) if v21 else (EXTENSIBLE, CLOSED)
    limit = csv.field_size_limit(2*1024**2)
    try:
        summaries = [r for r in csv.DictReader(io.StringIO(base)) if r['geometry_row_kind']=='SUMMARY']
    finally:
        csv.field_size_limit(limit)
    output = BoundedCsv(CAPABILITY_FIELDS + (V21_COLUMNS if v21 else ()), 'capability output exceeds the common reader input bound; split at complete records')
    count = unknown = 0
    for index, (line, summary) in enumerate(zip(text.splitlines(), summaries)):
        record = json.loads(line, object_pairs_hook=existing.unique_object)
        if v21:
            record = existing.factsheet_21_view(record)
        common = dict(record_time_us=summary['record_time_us'], source_record_index=index,
                      source_record_sha256='sha256:'+hashlib.sha256(line.encode()).hexdigest())

        def emit(path, item='', sub='', **values):
            nonlocal count
            row = dict(common, declaration_path=path, item_index=item, sub_index=sub, **values)
            output.writerow(v21_provenance(row) if v21 else row)
            count += 1

        def section(parent, key, required=True):
            value = parent.get(key)
            if value is None and not required and key not in parent:
                return None
            if not isinstance(value, dict):
                raise ValueError('invalid or missing declaration object')
            account(value, key)
            return value

        def account(value, kind):
            nonlocal unknown
            if not isinstance(value, dict):
                raise ValueError('invalid declaration entry')
            unknown += len(set(value) - KNOWN_KEYS[kind])

        def listing(parent, key, required):
            if key not in parent:
                if required:
                    raise ValueError('required declaration list missing')
                return None
            if not isinstance(parent[key], list) or len(parent[key]) > 1024:
                raise ValueError('invalid or oversized declaration list')
            return parent[key]

        def enum_status(name, value):
            if name in closed:
                return 'KNOWN' if value in closed[name] else 'OUTSIDE_PINNED_ENUM_RETAINED'
            if name in extensible:
                return 'KNOWN' if value in extensible[name] else 'EXTENSIBLE_UNRECOGNIZED'
            return ''

        def text_leaf(path, parent, key, required, item='', sub='', name=None, disposition='REPORTED'):
            if key not in parent:
                if required:
                    raise ValueError('required declaration text missing')
                emit(path, item, sub, disposition='ABSENT', value_basis='DECLARED_TEXT')
                return
            emit(path, item, sub, disposition=disposition, value_hex=text_hex(parent[key]),
                 enum_status=enum_status(name or key, parent[key]), value_basis='DECLARED_TEXT')

        def text_list(path, parent, key, required, item='', empty='EMPTY_DECLARED_LIST', absent='ABSENT'):
            values = listing(parent, key, required)
            if not values:
                emit(path, item, disposition=absent if values is None else empty, value_basis='DECLARED_LIST')
                return
            for position, value in enumerate(values):
                if item == '':
                    emit(path, position, disposition='REPORTED', value_hex=text_hex(value),
                         enum_status=enum_status(key, value), value_basis='DECLARED_LIST_ITEM_IN_SOURCE_ORDER')
                else:
                    emit(path, item, position, disposition='REPORTED', value_hex=text_hex(value),
                         enum_status=enum_status(key, value), value_basis='DECLARED_LIST_ITEM_IN_SOURCE_ORDER')

        def truth(path, parent, key, item, sub='', string_form=False):
            value = parent.get(key)
            if key not in parent:
                emit(path, item, sub, disposition='ABSENT', value_basis='DECLARED_BOOLEAN')
                return
            if string_form and value in ('true', 'false'):
                emit(path, item, sub, disposition='REPORTED_STRING_FORM_PER_SCHEMA', truth_value=value,
                     value_basis='SPEC_BOOLEAN_SCHEMA_STRING')
                return
            if type(value) is not bool:
                raise ValueError('invalid declared boolean')
            emit(path, item, sub, disposition='REPORTED', truth_value=str(value).lower(),
                 value_basis='SPEC_BOOLEAN_SCHEMA_STRING' if string_form else 'DECLARED_BOOLEAN')

        # typeSpecification: maximumLoadMass is already carried by the base factsheet output.
        spec = section(record, 'typeSpecification')
        path = 'typeSpecification.'
        text_leaf(path+'seriesName', spec, 'seriesName', True)
        text_leaf(path+'seriesDescription', spec, 'seriesDescription', False)
        spellings = [key for key in ('mobileRobotKinematics', 'mobileRobotKinematic') if key in spec]
        if len(spellings) != 1:
            raise ValueError('kinematics must be declared under exactly one spelling')
        text_leaf(path+'mobileRobotKinematics', spec, spellings[0], True, name='mobileRobotKinematics',
                  disposition='REPORTED' if spellings[0] == 'mobileRobotKinematics'
                  else 'REPORTED_UNDER_SCHEMA_REQUIRED_SPELLING_mobileRobotKinematic')
        text_leaf(path+'mobileRobotClass', spec, 'mobileRobotClass', True)
        text_list(path+'localizationTypes', spec, 'localizationTypes', True)
        text_list(path+'navigationTypes', spec, 'navigationTypes', True)
        text_list(path+'supportedZones', spec, 'supportedZones', False)

        features = section(record, 'protocolFeatures')
        path = 'protocolFeatures.optionalParameters'
        parameters = listing(features, 'optionalParameters', True)
        if not parameters:
            emit(path, disposition='EMPTY_DECLARED_LIST', value_basis='UNLISTED_OPTIONAL_PARAMETERS_NOT_SUPPORTED_PER_SPEC')
        for position, item in enumerate(parameters):
            account(item, 'optionalParameters[]')
            for key, required in (('parameter', True), ('support', True), ('description', False)):
                text_leaf(path+'.'+key, item, key, required, position)
        path = 'protocolFeatures.mobileRobotActions'
        actions = listing(features, 'mobileRobotActions', True)
        if not actions:
            emit(path, disposition='EMPTY_DECLARED_LIST', value_basis='DECLARED_LIST')
        for position, action in enumerate(actions):
            account(action, 'mobileRobotActions[]')
            for key, required in (('actionType', True), ('actionDescription', False), ('actionResult', False)):
                text_leaf(path+'.'+key, action, key, required, position)
            text_list(path+'.actionScopes', action, 'actionScopes', True, position)
            text_list(path+'.blockingTypes', action, 'blockingTypes', False, position)
            for key in ('pauseAllowed', 'cancelAllowed'):
                if key not in action and not v21:
                    raise ValueError('required action flag missing')
                truth(path+'.'+key, action, key, position, string_form=True)
            items = listing(action, 'actionParameters', False)
            if not items:
                emit(path+'.actionParameters', position,
                     disposition='ABSENT_NO_PARAMETERS' if items is None else 'EMPTY_DECLARED_LIST',
                     value_basis='DECLARED_LIST')
            for sub, item in enumerate(items or []):
                account(item, 'actionParameters[]')
                for key, required in (('key', True), ('valueDataType', True), ('description', False)):
                    text_leaf(path+'.actionParameters.'+key, item, key, required, position, sub)
                truth(path+'.actionParameters.isOptional', item, 'isOptional', position, sub)

        load = section(record, 'loadSpecification')
        path = 'loadSpecification.'
        text_list(path+'loadPositions', load, 'loadPositions', False,
                  empty='EMPTY_DECLARED_LIST_NO_LOAD_HANDLING_DEVICE',
                  absent='ABSENT_NO_LOAD_HANDLING_DEVICE_DECLARED')
        sets = listing(load, 'loadSets', False)
        if not sets:
            emit(path+'loadSets', disposition='ABSENT' if sets is None else 'EMPTY_DECLARED_LIST', value_basis='DECLARED_LIST')
        for position, item in enumerate(sets or []):
            account(item, 'loadSets[]')
            prefix = path+'loadSets.'
            for key, required in (('setName', True), ('loadType', True), ('description', False)):
                text_leaf(prefix+key, item, key, required, position)
            text_list(prefix+'loadPositions', item, 'loadPositions', False, position,
                      empty='EMPTY_DECLARED_LIST_ALL_POSITIONS', absent='ABSENT_ALL_POSITIONS')
            for key, column, nonnegative in LOAD_QUANTITIES:
                basis = 'DECLARED_FOR_THIS_LOAD_SET_NOT_MEASURED'
                if key == 'maximumSpeed':
                    if load_speed_policy == 'spec-text':
                        basis = 'CALLER_POLICY_SPEC_TEXT_m_s_OVER_SCHEMA_m_s2_DECLARED_NOT_MEASURED'
                    else:
                        basis = 'UNIT_CONFLICT_SCHEMA_m_s2_SPEC_TEXT_m_s_UNRESOLVED'
                        column = 'unit_conflict_value'
                if key not in item:
                    emit(prefix+key, position, disposition='ABSENT', value_basis=basis)
                else:
                    emit(prefix+key, position, value_basis=basis,
                         disposition='REPORTED_UNIT_CONFLICT_UNRESOLVED' if column == 'unit_conflict_value' else 'REPORTED',
                         **{column: number(item[key], 0 if nonnegative else None)})
            reference = section(item, 'boundingBoxReference', required=False)
            for key in ('x', 'y', 'z', 'theta'):
                if reference is None or key not in reference:
                    if reference is not None and key != 'theta':
                        raise ValueError('bounding box reference coordinate missing')
                    emit(prefix+'boundingBoxReference.'+key, position, disposition='ABSENT',
                         value_basis='ROBOT_FRAME_UNIT_NOT_STATED_AT_PIN')
                else:
                    emit(prefix+'boundingBoxReference.'+key, position, disposition='REPORTED',
                         coordinate_unit_not_stated=number(reference[key]),
                         value_basis='ROBOT_FRAME_UNIT_NOT_STATED_AT_PIN')
            dimensions = section(item, 'loadDimensions', required=False)
            for key in ('length', 'width', 'height'):
                if dimensions is None or key not in dimensions:
                    if dimensions is not None and key != 'height':
                        raise ValueError('load dimension missing')
                    emit(prefix+'loadDimensions.'+key, position, disposition='ABSENT',
                         value_basis='LOAD_BOUNDING_BOX_ROBOT_AXES_DECLARED')
                else:
                    emit(prefix+'loadDimensions.'+key, position, disposition='REPORTED',
                         length_m=number(dimensions[key], 0), value_basis='LOAD_BOUNDING_BOX_ROBOT_AXES_DECLARED')

        configuration = section(record, 'mobileRobotConfiguration', required=False)
        path = 'mobileRobotConfiguration.'
        if configuration is None:
            emit(path.rstrip('.'), disposition='ABSENT', value_basis='OPTIONAL_DECLARATION')
            continue
        versions = listing(configuration, 'versions', False)
        if not versions:
            emit(path+'versions', disposition='ABSENT' if versions is None else 'EMPTY_DECLARED_LIST', value_basis='DECLARED_LIST')
        for position, item in enumerate(versions or []):
            account(item, 'versions[]')
            for key in ('key', 'value'):
                text_leaf(path+'versions.'+key, item, key, True, position)
        network = section(configuration, 'network', required=False)
        if network is None:
            emit(path+'network', disposition='ABSENT', value_basis='DECLARED_A_PRIORI_NOT_OBSERVED')
        else:
            for key in ('dnsServers', 'ntpServers'):
                text_list(path+'network.'+key, network, key, False)
            for key in ('localIpAddress', 'netmask', 'defaultGateway'):
                text_leaf(path+'network.'+key, network, key, False)
        keys = [key for key in ('batteryCharging', 'batteryCharging ') if key in configuration]
        if len(keys) > 1:
            raise ValueError('battery charging declared under both spellings')
        charging = section(configuration, keys[0], required=False) if keys else None
        disposition = 'REPORTED_UNDER_SCHEMA_KEY_WITH_TRAILING_SPACE' if keys == ['batteryCharging '] else 'REPORTED'
        for key in ('criticalLowChargingLevel', 'minimumDesiredChargingLevel', 'maximumDesiredChargingLevel',
                    'minimumChargingTime'):
            target = path+'batteryCharging.'+key
            if charging is None or key not in charging:
                emit(target, disposition='ABSENT', value_basis='DECLARED_CHARGING_PARAMETER')
            elif key == 'minimumChargingTime':
                value = charging[key]
                if type(value) is not int or not 0 <= value <= 2**32-1:
                    raise ValueError('invalid minimum charging time')
                emit(target, disposition=disposition, duration_s=value, value_basis='DECLARED_CHARGING_PARAMETER')
            else:
                emit(target, disposition=disposition, percent=number(charging[key], 0, 100),
                     value_basis='DECLARED_CHARGING_PARAMETER_NOT_MEASURED_STATE_OF_CHARGE')
    return output.getvalue(), dict(capability_rows=count, capability_output_bytes=output.size,
                                unknown_capability_properties=unknown, load_speed_unit_policy=load_speed_policy)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path)
    parser.add_argument('output_directory',type=Path)
    parser.add_argument('--capabilities',action='store_true',
                        help='also write declared type/protocol/load/configuration leaves to capabilities.csv')
    parser.add_argument('--source-format',choices=tuple(existing.FACTSHEET_FORMATS),default=existing.FACTSHEET_FORMAT,
                        help='explicit factsheet version selector; the record version must match it')
    parser.add_argument('--load-speed-unit-policy',choices=LOAD_SPEED_POLICIES,default=None,
                        help='loadSet.maximumSpeed unit conflict (schema m/s^2, spec text m/s); default unresolved')
    args=parser.parse_args()
    if args.load_speed_unit_policy is not None and not args.capabilities:
        parser.error('--load-speed-unit-policy requires --capabilities')
    try:
        with args.input.open('rb') as stream:
            data=stream.read(16*1024**2+1)
        if len(data)>16*1024**2:
            raise ValueError('input exceeds bound')
        base,sidecar,report=convert(data.decode('utf-8'),args.source_format)
        if args.capabilities:
            capabilities,details=convert_capabilities(data.decode('utf-8'),
                load_speed_policy=args.load_speed_unit_policy or 'unresolved',source_format=args.source_format)
            report.update(details)
        args.output_directory.mkdir()
        if args.capabilities:
            (args.output_directory/'capabilities.csv').write_text(capabilities,encoding='utf-8')
        (args.output_directory/'source.jsonl').write_bytes(data)
        (args.output_directory/'geometry.csv').write_text(base,encoding='utf-8')
        (args.output_directory/'limits.csv').write_text(sidecar,encoding='utf-8')
        (args.output_directory/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    except (ValueError,OSError,TypeError,KeyError,RecursionError,OverflowError):
        parser.exit(2,'Saved factsheet conversion failed; no successful conversion claim\n')


if __name__=='__main__':
    main()
