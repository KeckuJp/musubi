"""Shared authored input declaration for saved DataFlash JSONL tests."""
INPUT_PROFILE = '''profile_id = "authored_rcou_input"
version = "1"
family = "unknown"
source_role = "fc"
format = "pymavlink_dataflash_jsonl"
extensions = ["jsonl"]
default_clock_basis = "boot_relative"
[fields]
time = "TimeUS"
'''
