"""Passive fixed Nav2 GetResult records with explicit caller request binding.

convert(request_bytes, response_bytes, goal_status_bytes, declaration_bytes)
returns CSV, mandatory report and profile. Save all four originals plus all three
outputs. No service calls, action goals, clock alignment or physical success claim.
"""
import csv
import io
import json

from scripts.convert_rosbag_observations import action_status_quantities
from scripts.convert_image_flight_binding import digest, integer, label
from scripts.convert_position_csv import unique_object, invalid_constant

NAV2 = "a097086719c88f781aa59788eca29ac6ca5e56db"
ROSIDL = "5621b26eff54596a356ad8abd72c06e7650d21f7"
RELATION = "CALLER_SAVED_REQUEST_RESPONSE_SAME_ACTION_SOURCE_SESSION_NOT_AUTHENTICATED"
LABELS = ("source_id", "session_id", "action_name", "citation")
KEYS = {"schema","nav2_pin","rosidl_pin","request_sha256","response_sha256","status_sha256",
        "goal_uuid","relation","request_capture_us","response_capture_us","status_capture_us",*LABELS}


def load(raw, limit):
    if type(raw) is not bytes or len(raw)>limit:
        raise ValueError("bounded original bytes required")
    value=json.loads(raw.decode(),object_pairs_hook=unique_object,parse_constant=invalid_constant)
    if not isinstance(value,dict):
        raise ValueError("saved record object required")
    return value


def uuid(value):
    if not isinstance(value,list) or len(value)!=16 or any(type(x) is not int or not 0<=x<=255 for x in value):
        raise ValueError("invalid goal UUID")
    return "hex:"+bytes(value).hex()


def convert(request_bytes,response_bytes,status_bytes,declaration_bytes):
    request,response,status=[load(raw,1024**2) for raw in (request_bytes,response_bytes,status_bytes)]
    declared=load(declaration_bytes,16*1024)
    if set(declared)!=KEYS or declared["schema"]!="nav2-saved-result-v1" or declared["nav2_pin"]!=NAV2 or declared["rosidl_pin"]!=ROSIDL or declared["relation"]!=RELATION:
        raise ValueError("closed fixed-version caller association required")
    for key,raw in (("request",request_bytes),("response",response_bytes),("status",status_bytes)):
        if declared[key+"_sha256"]!=digest(raw):
            raise ValueError("source digest mismatch")
    labels={key+"_hex":label(declared[key]) for key in LABELS}
    for key in ("request_capture_us","response_capture_us","status_capture_us"):
        integer(declared[key])
    try:
        request_uuid=uuid(request["goal_id"]["uuid"])
        if request_uuid!=uuid(declared["goal_uuid"]) or request_uuid!=uuid(status["goal_info"]["goal_id"]["uuid"]):
            raise ValueError("different goal UUIDs")
        status_meaning=action_status_quantities({"status_list":[status]})[0]
        result=response["result"]
        if not isinstance(result,dict) or "result" not in result or type(result["result"]) is not dict or result["result"]!={}:
            raise ValueError("present fixed std_msgs Empty result required")
        # Reuse int8 enum rules; accepted stamp below remains the status record's
        # original stamp, never a timestamp claimed to occur in the response.
        result_meaning=action_status_quantities({"status_list":[{
            "goal_info":status["goal_info"],"status":response["status"]}]})[0]
    except (KeyError,TypeError,AttributeError) as error:
        raise ValueError("malformed saved action record") from error
    row=dict(record_time_us=declared["response_capture_us"],**status_meaning,
        result_status_code=result_meaning["action_status_code"],result_status_reported=result_meaning["action_status_reported"],
        result_body_status="PRESENT_EMPTY_TYPED_RESULT",response_goal_id_basis="ABSENT_CALLER_REQUEST_ASSOCIATION",
        result_source_clock="NOT_PRESENT",association_basis=RELATION,
        request_capture_us=declared["request_capture_us"],response_capture_us=declared["response_capture_us"],
        status_capture_us=declared["status_capture_us"],source_request_sha256=digest(request_bytes),
        source_response_sha256=digest(response_bytes),source_status_sha256=digest(status_bytes),
        source_declaration_sha256=digest(declaration_bytes),**labels)
    out=io.StringIO()
    writer=csv.DictWriter(out,fieldnames=list(row));writer.writeheader();writer.writerow(row)
    text=out.getvalue()
    if len(text.encode())>16*1024**2:
        raise ValueError("output exceeds bound")
    report={"source_records":3,"output_observations":1,"caller_association":declared,"clock":"Unknown",
        "response_has_no_uuid_or_timestamp":True,"physical_success":"NOT_EVALUATED",
        "source_request_sha256":digest(request_bytes),"source_response_sha256":digest(response_bytes),
        "source_status_sha256":digest(status_bytes),"source_declaration_sha256":digest(declaration_bytes),
        "csv_sha256":digest(text.encode()),"original_bytes_hex":{
            "request":request_bytes.hex(),"response":response_bytes.hex(),"status":status_bytes.hex()},
        "uninterpreted_keys":{"request":sorted(set(request)-{"goal_id"}),
            "response":sorted(set(response)-{"status","result"}),"result":sorted(set(result)-{"result"}),
            "status":sorted(set(status)-{"goal_info","status"})}}
    units={"record_time_us":"caller_response_capture_us_not_response_source_time",
        "action_accepted_stamp_ns":"ns_reported_goal_acceptance_not_result_time",
        "action_accepted_stamp_us":"floor_us_reported_goal_acceptance_not_result_time",
        "action_status_code":"int8_reported_status_snapshot_not_physical_state",
        "result_status_code":"int8_reported_response_status_not_physical_success",
        "action_uuid_hex":"16byte_goal_UUID_not_authenticated",
        "request_capture_us":"caller_capture_us_Unknown",
        "response_capture_us":"caller_capture_us_Unknown","status_capture_us":"caller_capture_us_Unknown"}
    profile='profile_id = "nav2_saved_result_v1"\nversion = "1"\nfamily = "unknown"\nsource_role = "recorded_export"\nformat = "telemetry_csv_us"\nextensions = ["csv"]\ndefault_clock_basis = "unknown"\nchannels = ["onboard"]\n[fields]\ntime = "record_time_us"\n[units]\n'
    profile+="\n".join(f'{key} = "{value}"' for key,value in units.items())+"\n"
    return text,report,profile
