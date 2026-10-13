import copy
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_nav2_saved_result import convert, digest, NAV2, ROSIDL, RELATION


def fixture(code=4, byte=1):
    request = {"goal_id": {"uuid": [byte]*16}, "extra": "kept"}
    response = {"status": code,"result":{"result":{}},"extension":17}
    status = {"goal_info":{"goal_id":{"uuid":[byte]*16},"stamp":{"sec":2,"nanosec":123}},"status":2}
    raw = [json.dumps(x).encode() for x in (request,response,status)]
    declaration = dict(schema="nav2-saved-result-v1",nav2_pin=NAV2,rosidl_pin=ROSIDL,
        request_sha256=digest(raw[0]),response_sha256=digest(raw[1]),status_sha256=digest(raw[2]),
        goal_uuid=[byte]*16,relation=RELATION,source_id="declared-source",session_id=f"session-{byte}",
        action_name="/navigate_to_pose",citation="authored association not authenticated",
        request_capture_us=10,response_capture_us=30,status_capture_us=20)
    return raw,declaration


class Nav2SavedResultTests(unittest.TestCase):
    def test_two_sets_common_and_unknown(self):
        for code,byte in ((4,1),(99,7)):
            raw,decl=fixture(code,byte)
            text,report,profile=convert(*raw,json.dumps(decl).encode())
            row=list(csv.DictReader(io.StringIO(text)))[0]
            self.assertEqual(row["result_status_code"],str(code))
            self.assertEqual(row["result_status_reported"],"SUCCEEDED_REPORTED" if code==4 else "UNKNOWN_99")
            self.assertEqual(row["action_status_code"],"2")
            self.assertEqual(row["action_accepted_stamp_ns"],"2000000123")
            self.assertEqual(row["result_body_status"],"PRESENT_EMPTY_TYPED_RESULT")
            self.assertEqual(row["action_uuid_hex"],"hex:"+bytes([byte]*16).hex())
            self.assertEqual(report["source_response_sha256"],digest(raw[1]))
            self.assertEqual(report["csv_sha256"],digest(text.encode()))
            self.assertIn("extension",report["uninterpreted_keys"]["response"])
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)
                (path/"p.toml").write_text(profile)
                (path/"o.csv").write_text(text)
                reader=os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
                self.assertTrue(reader)
                run=subprocess.run([reader,str(path/"p.toml"),str(path/"o.csv")],capture_output=True,text=True,check=True)
                obs=json.loads(run.stdout)["observations"]
                self.assertEqual(len(obs),1)
                self.assertEqual(obs[0]["clock_basis"],"Unknown")

    def test_wrong_binding_missing_empty_and_status(self):
        raw,decl=fixture()
        for key,value in (("goal_uuid",[2]*16),("response_sha256","0"*64),("nav2_pin","new"),("response_capture_us",True)):
            with self.assertRaises(ValueError):
                convert(*raw,json.dumps(dict(decl,**{key:value})).encode())
        for response in ({"status":4},{"status":4,"result":{}},{"status":4,"result":{"result":{"unexpected":1}}},
                         {"status":True,"result":{"result":{}}},{"status":128,"result":{"result":{}}}):
            changed=list(raw)
            changed[1]=json.dumps(response).encode()
            d=dict(decl,response_sha256=digest(changed[1]))
            with self.assertRaises(ValueError):
                convert(*changed,json.dumps(d).encode())
        status=json.loads(raw[2]);status["goal_info"]["goal_id"]["uuid"]=[3]*16
        changed=list(raw);changed[2]=json.dumps(status).encode()
        with self.assertRaises(ValueError):
            convert(*changed,json.dumps(dict(decl,status_sha256=digest(changed[2]))).encode())
