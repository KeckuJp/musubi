"""Caller-declared saved camera report association, never identity or clock inference."""
import csv
import io
import json

from scripts.convert_controller_record_composition import digest, text, BASIS
from scripts.convert_ardupilot_battery_csv import unique_object
from scripts.convert_opcua_recorded import convert_onvif_status, convert_onvif_imaging_settings
from scripts.convert_onvif_recording_job import convert as recording

ROUTES = {'ptz': (convert_onvif_status, 'PTZ18.12'),
          'imaging': (convert_onvif_imaging_settings, 'Imaging25.12'),
          'recording': (recording, 'Recording25.12_WSDL26.06')}
FIELDS = ['source_capture_time_us', 'association_clock_basis', 'binding_manifest_sha256', 'binding_camera_hex', 'binding_run_hex',
          'binding_citation_hex', 'binding_basis', 'binding_role', 'binding_input_sha256',
          'binding_context_token_hex', 'binding_clock_hex', 'binding_schema_basis', 'binding_source_row']


def convert(inputs, manifest):
    if not isinstance(inputs, list) or len(inputs) != 3 or any(type(x) is not bytes or not 0 < len(x) <= 1024**2 for x in inputs):
        raise ValueError('three bounded original XML byte inputs required')
    if type(manifest) is not bytes or len(manifest) > 16384:
        raise ValueError('bounded manifest bytes required')
    spec = json.loads(manifest, object_pairs_hook=unique_object)
    if not isinstance(spec, dict) or set(spec) != {'schema','basis','camera','run','citation','sources'} or spec['schema'] != 'onvif-report-set-v1' or spec['basis'] != BASIS:
        raise ValueError('explicit report-set declaration required')
    labels = {'binding_'+key+'_hex': text(spec[key]) for key in ('camera','run','citation')}
    if not isinstance(spec['sources'], list) or len(spec['sources']) != 3:
        raise ValueError('exact three source declarations required')
    rows = []; reports = []; seen = set(); names = []; size = 0
    manifest_sha = digest(manifest)
    for raw, source in zip(inputs, spec['sources']):
        if not isinstance(source, dict) or set(source) != {'role','sha256','context_token','capture_time_us','clock','schema_basis'}:
            raise ValueError('source declaration shape')
        role = source['role']
        if not isinstance(role, str) or role not in ROUTES or role in seen:
            raise ValueError('missing or duplicate selected role')
        seen.add(role)
        resolver, basis = ROUTES[role]
        source_sha = digest(raw)
        if source['sha256'] != source_sha or source['schema_basis'] != basis:
            raise ValueError('original digest or selected schema basis mismatch')
        context = text(source['context_token']); clock = text(source['clock'])
        stamp = source['capture_time_us']
        if type(stamp) is not int or not 0 <= stamp < 2**63:
            raise ValueError('explicit capture time required')
        original, report = resolver(raw.decode('utf-8'), source['context_token'], stamp)
        reader = csv.DictReader(io.StringIO(original))
        names.extend(reader.fieldnames)
        count = 0
        for index, row in enumerate(reader):
            row.update(source_capture_time_us=row['record_time_us'], record_time_us=0,
                       association_clock_basis='UNLOCATED_ASSOCIATION_NOT_SYNCHRONIZED')
            row.update(labels, binding_manifest_sha256=manifest_sha, binding_basis=BASIS,
                binding_role=role, binding_input_sha256=source_sha, binding_context_token_hex=context,
                binding_clock_hex=clock, binding_schema_basis=basis, binding_source_row=index)
            size += sum(len(str(v).encode()) for v in row.values())
            if size > 128*1024**2:
                raise ValueError('combined output bound')
            rows.append(row); count += 1
        reports.append(dict(role=role, original_sha256=source_sha, rows=count, clock=source['clock'],
                            schema_basis=basis, source_report=report))
    if seen != set(ROUTES):
        raise ValueError('all selected roles required')
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, list(dict.fromkeys(names+FIELDS)), lineterminator='\n')
    writer.writeheader(); writer.writerows(rows)
    if len(out.getvalue().encode()) > 128*1024**2:
        raise ValueError('combined encoded output bound')
    report = dict(schema='onvif-report-set-v1', manifest_sha256=manifest_sha, basis=BASIS,
                  observations=len(rows), sources=reports,
                  clocks='SEPARATE_DECLARATIONS_NOT_SYNCHRONIZED', camera_identity='CALLER_NOT_AUTHENTICATED')
    if len(json.dumps(report,ensure_ascii=True).encode()) > 32*1024**2:
        raise ValueError('combined report bound')
    return out.getvalue(), report
