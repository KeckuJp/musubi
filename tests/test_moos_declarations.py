"""Authored MOOS `.moos`/`.bhv` declaration imports; no real or vendor mission contents are used.

Every fixture here is written in this file against the pinned reader grammar
(moos-ivp f56c6907). Nothing is executed: no community, no behavior, no condition evaluation.
"""
import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / "scripts/convert_moos_declarations.py"
PROFILE = ROOT / "profiles/declared/imc-sensors/moos-declarations-profile.toml"
CAPTURE = 1_788_000_000_000_000


def load():
    spec = importlib.util.spec_from_file_location("convert_moos_declarations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["convert_moos_declarations"] = module
    spec.loader.exec_module(module)
    return module



MISSION_A = """\
// authored mission A, not a real deployment
ServerHost = localhost
ServerPort = 9000
Community  = alpha
LatOrigin  = 43.825300
LongOrigin = -70.330400

ProcessConfig = ANTLER
{
  MSBetweenLaunches = 200
  Run = MOOSDB          @ NewConsole = false
  Run = pHelmIvP        @ NewConsole = false
}

ProcessConfig = pHelmIvP
{
  AppTick    = 4
  CommsTick  = 4
  behaviors  = alpha.bhv
  domain     = course:0:359:360
  domain     = speed:0:4:21
  ok_skew    = any
}

ProcessConfig = pLogger
{
  AppTick   = 8            // inline comment kept out of the value
  File      = LOG_ALPHA
  Log       = NAV_X @ 0 NOSYNC
  WildCardLogging = true
}
"""

BEHAVIOR_A = """\
//-- authored behavior A
initialize   DEPLOY  = false
initialize   RETURN  = false
initialize_  LOITER  = true

Behavior = BHV_Waypoint
{
  name      = waypt_survey
  pwt       = 100
  condition = (RETURN = false) and (DEPLOY = true)
  condition = (MODE == SURVEYING)
  endflag   = RETURN = true
  speed     = 1.4
  radius    = 8.0
  points    = 60,-40 : 60,-160 : 150,-160
}

Behavior = BHV_Waypoint
{
  name      = waypt_return
  pwt       = 80
  condition = (RETURN = true)
  speed     = 2.0
  points    = 0,0
}

set MODE = ACTIVE {
}
"""

MISSION_B = """\
ServerHost = 127.0.0.1
ServerPort = 9100
Community = bravo
ServerPort = 9101

ProcessConfig = uProcessWatch
{
  AppTick = 2
  watch_all = true
  nowatch = uXMS*
}
"""

BEHAVIOR_B = """\
initialize AVOID = true

Behavior = BHV_AvdColregsV22
{
  name = avoid_contact
  condition = AVOID = true
  pwt_outer_dist = 50 \\

  match_type = ferry
}
"""


BEHAVIOR_C = """\
// authored behavior C: mode declarations with their declared conditions
initialize DEPLOY = false

Behavior = BHV_StationKeep
{
  name  = station_c
  speed = 1.2
}

set MODE = ACTIVE {
  DEPLOY,true
  (RETURN = false) and (DEPLOY = true)
}

set MODE = INACTIVE {
  DEPLOY,false
}
"""


class MoosDeclarationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.moos = load()

    def rows_of(self, text, declaration_format, capture=CAPTURE):
        output, report = self.moos.convert(text.encode(), capture,
                                           declaration_format=declaration_format)
        rows = list(csv.DictReader(io.StringIO(output)))
        for row in rows:  # decode the lossless transport back to the authored strings
            for column in ("setting_value_hex", "setting_value_stripped_hex", "source_text_hex",
                           "lexical_text_hex"):
                plain = column[:-len("_hex")]
                value = row[column]
                row[plain] = (bytes.fromhex(value[4:]).decode() if value.startswith("hex:") else "")
        return rows, report

    def refusal(self, text, declaration_format="moos-mission"):
        with self.assertRaises(self.moos.Refused) as caught:
            self.moos.convert(text.encode(), CAPTURE, declaration_format=declaration_format)
        return caught.exception.code


    def test_mission_scopes_names_values_and_order_are_preserved(self):
        rows, report = self.rows_of(MISSION_A, "moos-mission")
        self.assertEqual(report["process_blocks"], 3)
        self.assertEqual(report["global_settings"], 5)
        self.assertEqual(report["process_settings"], 13)
        self.assertEqual(report["declaration_rows"], len(rows))
        self.assertEqual(report["rows_by_kind"]["mission_process_block"], 3)
        self.assertGreater(report["comment_lines"], 0)
        self.assertGreater(report["blank_lines"], 0)

        globals_rows = [r for r in rows if r["scope_kind"] == "global"]
        self.assertEqual([r["setting_name"] for r in globals_rows],
                         ["ServerHost", "ServerPort", "Community", "LatOrigin", "LongOrigin"])
        origin = {r["setting_name"]: r for r in globals_rows}
        self.assertEqual(origin["LatOrigin"]["setting_value"], "43.825300")
        self.assertEqual(origin["LatOrigin"]["value_kind"], "decimal")
        self.assertEqual(origin["LongOrigin"]["setting_value"], "-70.330400")
        self.assertEqual(origin["ServerPort"]["value_kind"], "integer")
        self.assertEqual(origin["Community"]["value_kind"], "string")
        self.assertEqual(origin["Community"]["value_number"], "")
        self.assertTrue(all(r["interpretation_basis"].startswith("DECLARED_CONFIGURATION")
                            for r in rows))

        helm = [r for r in rows if r["scope_name"] == "pHelmIvP" and r["setting_name"]]
        self.assertEqual([r["setting_name"] for r in helm],
                         ["AppTick", "CommsTick", "behaviors", "domain", "domain", "ok_skew"])
        self.assertEqual([r["assignment_order"] for r in helm], ["1", "2", "3", "4", "5", "6"])
        domains = [r for r in helm if r["setting_name"] == "domain"]
        self.assertEqual([r["setting_occurrence"] for r in domains], ["1", "2"])
        self.assertEqual([r["setting_value"] for r in domains],
                         ["course:0:359:360", "speed:0:4:21"])
        for row in rows:
            line = MISSION_A.splitlines()[int(row["source_line"]) - 1]
            self.assertEqual(row["source_text"], line)
            self.assertEqual(row["lexical_text"], line)
            self.assertEqual(row["source_line_end"], row["source_line"])
            self.assertEqual(row["source_text_basis"], self.moos.SOURCE_EXACT)
        logger = {r["setting_name"]: r for r in rows if r["scope_name"] == "pLogger"
                  and r["setting_name"]}
        self.assertEqual(logger["AppTick"]["setting_value"], "8")
        self.assertEqual(logger["Log"]["setting_value"], "NAV_X @ 0 NOSYNC")
        self.assertEqual(logger["Log"]["setting_value_stripped"], "NAV_X@0NOSYNC")
        self.assertEqual(logger["Log"]["setting_value_hex"],
                         "hex:" + "NAV_X @ 0 NOSYNC".encode().hex(),
                         "free text travels hex encoded and reverses to the authored string")
        self.assertEqual(logger["WildCardLogging"]["value_kind"], "boolean")
        self.assertEqual(logger["WildCardLogging"]["value_number"], "1")
        self.assertIn("DEPENDS_ON_THE_CONSUMING_APPLICATION", report["whitespace_basis"])
        self.assertIn("BLOCK_UNAWARE", report["global_lookup_basis"])
        antler = [r for r in rows if r["scope_name"] == "ANTLER" and r["setting_name"] == "Run"]
        self.assertEqual([r["setting_occurrence"] for r in antler], ["1", "2"])

    def test_mission_refusals_leave_nothing_silently_dropped(self):
        head = "ServerHost = localhost\n"
        cases = [  # pairs, not a dict: two shapes may legitimately share one refusal code
            ("PROCESS_HEADER_CARRIES_ITS_OPENING_BRACE", head + "ProcessConfig = pFoo {\n  a = 1\n}\n"),
            ("PROCESS_BLOCK_OPENING_BRACE_MISSING", head + "ProcessConfig = pFoo\n  a = 1\n}\n"),
            ("PROCESS_BLOCK_NEVER_CLOSED", head + "ProcessConfig = pFoo\n{\n  a = 1\n"),
            ("CLOSING_BRACE_WITHOUT_AN_OPEN_BLOCK", head + "}\n"),
            ("TEXT_AFTER_THE_CLOSING_BRACE", head + "ProcessConfig = pFoo\n{\n a = 1\n} trailing\n"),
            ("NESTED_BLOCK_INSIDE_AN_OPEN_BLOCK", head + "ProcessConfig = pFoo\n{\n b = {\n}\n"),
            ("LINE_IS_NEITHER_COMMENT_ASSIGNMENT_NOR_BRACE", head + "just some prose\n"),
            ("ASSIGNMENT_WITHOUT_A_NAME", head + "= 5\n"),
            ("PROCESS_HEADER_WITHOUT_A_NAME", head + "ProcessConfig =\n{\n}\n"),
            ("PROCESS_HEADER_INSIDE_AN_OPEN_BLOCK",
             head + "ProcessConfig = pFoo\n{\nProcessConfig = pBar\n"),
            ("SHELL_VARIABLE_EXPANSION_NOT_DETERMINED_BY_THIS_FILE", head + "File = ${LOGDIR}/a\n"),
            ("NSPLUG_PREPROCESSOR_DIRECTIVE_NOT_DETERMINED_BY_THIS_FILE", head + "#include plug.moos\n"),
            ("READER_DEFINE_VARIABLE_NOT_DETERMINED_BY_THIS_FILE", head + "define: SPEED = 2\n"),
            ("COMMENT_INSIDE_QUOTED_VALUE", head + 'Name = "a // b"\n'),
            ("TEXT_ON_THE_OPENING_BRACE_LINE", head + "ProcessConfig = pFoo\n{ a = 1\n}\n"),
            ("NO_DECLARATION_IN_THE_SELECTED_FILE", "// only a comment\n\n"),
        ]
        for code, text in cases:
            self.assertEqual(self.refusal(text), code, text)
        self.assertEqual(self.refusal(head + "File = $(LOGDIR)/a\n"),
                         "SHELL_VARIABLE_EXPANSION_NOT_DETERMINED_BY_THIS_FILE")
        for bad in ({"declaration_format": "moos-alog"}, {"declaration_format": ""}):
            with self.assertRaises(self.moos.Refused):
                self.moos.convert(head.encode(), CAPTURE, **bad)
        for capture in (-1, 1.5, "0"):
            with self.assertRaises(self.moos.Refused):
                self.moos.convert(head.encode(), capture, declaration_format="moos-mission")
        with self.assertRaises(self.moos.Refused):
            self.moos.convert(b"", CAPTURE, declaration_format="moos-mission")
        with self.assertRaises(self.moos.Refused):
            self.moos.convert(b"\xff\xfe = 1\n", CAPTURE, declaration_format="moos-mission")
        self.assertEqual(self.refusal("A = " + "x" * (self.moos.MAX_LINE_BYTES + 1) + "\n"),
                         "LINE_EXCEEDS_BOUND")


    def test_behavior_blocks_conditions_and_initialize_lines_are_typed_declarations(self):
        rows, report = self.rows_of(BEHAVIOR_A, "moos-behavior")
        self.assertEqual(report["behavior_blocks"], 2)
        self.assertEqual(report["initialize_lines"], 3)
        self.assertEqual(report["mode_sets"], 1)
        self.assertEqual(report["behavior_settings"], 13)
        self.assertEqual(report["declaration_rows"], len(rows))

        initialize = [r for r in rows if r["declaration_kind"] == "behavior_initialize"]
        self.assertEqual([r["setting_name"] for r in initialize], ["DEPLOY", "RETURN", "LOITER"])
        self.assertEqual([r["value_kind"] for r in initialize], ["boolean"] * 3)
        self.assertEqual(initialize[2]["accumulates_upstream"], "deferred",
                         "initialize_ is the deferred form upstream")

        survey = [r for r in rows if r["scope_name"] == "BHV_Waypoint"
                  and r["scope_occurrence"] == "1" and r["setting_name"]]
        self.assertEqual([r["setting_name"] for r in survey],
                         ["name", "pwt", "condition", "condition", "endflag", "speed", "radius",
                          "points"])
        conditions = [r for r in survey if r["setting_name"] == "condition"]
        self.assertEqual([r["setting_occurrence"] for r in conditions], ["1", "2"])
        self.assertEqual([r["accumulates_upstream"] for r in conditions], ["accumulates"] * 2)
        self.assertEqual(conditions[0]["setting_value"], "(RETURN = false) and (DEPLOY = true)")
        self.assertEqual(conditions[0]["value_kind"], "string")
        self.assertTrue(all(r["expression_basis"].startswith("UNEVALUATED_AUTHORED_SOURCE_EXPRESSION")
                            for r in conditions))
        self.assertEqual([r["expression_basis"] for r in survey if r["setting_name"] == "endflag"],
                         [""], "only condition parameters carry the expression basis")
        accumulation = {r["setting_name"]: r["accumulates_upstream"] for r in survey}
        self.assertEqual(accumulation["name"], "overwrites")
        self.assertEqual(accumulation["pwt"], "overwrites")
        self.assertEqual(accumulation["endflag"], "accumulates")
        for lexical_only in ("speed", "radius", "points"):
            self.assertEqual(accumulation[lexical_only], self.moos.ACCUMULATION_UNQUALIFIED,
                             "a parameter the base class does not handle has no established rule")
        self.assertIn("UNQUALIFIED", report["accumulation_basis"])
        occurrences = {r["scope_occurrence"] for r in rows if r["scope_name"] == "BHV_Waypoint"}
        self.assertEqual(occurrences, {"1", "2"})
        self.assertEqual([r["setting_value"] for r in rows
                          if r["scope_occurrence"] == "2" and r["setting_name"] == "name"],
                         ["waypt_return"])
        modes = [r for r in rows if r["declaration_kind"] == "behavior_mode_set"]
        self.assertEqual([r["scope_name"] for r in modes], ["MODE = ACTIVE"])
        self.assertEqual(modes[0]["source_text"], "set MODE = ACTIVE {")
        self.assertEqual(modes[0]["lexical_text"], "set MODE = ACTIVE")
        self.assertEqual(modes[0]["source_text_basis"], self.moos.SOURCE_RECONSTRUCTED)

    def test_behavior_refusals_and_duplicate_names(self):
        head = "initialize DEPLOY = true\n"
        block = "Behavior = BHV_Waypoint\n{\n name = a\n}\n"
        cases = [  # pairs, not a dict: two shapes may legitimately share one refusal code
            ("BLOCK_NEVER_CLOSED", head + "Behavior = BHV_Waypoint\n{\n name = a\n"),
            ("BLOCK_OPENING_BRACE_MISSING", head + "Behavior = BHV_Waypoint\n name = a\n"),
            ("CLOSING_BRACE_WITHOUT_AN_OPEN_BLOCK", head + "}\n"),
            ("BRACE_IN_A_STATE_THAT_DOES_NOT_ACCEPT_ONE", head + "{\n name = a\n}\n"),
            ("LINE_OUTSIDE_ANY_DECLARED_BLOCK", head + "loose = 1\n"),
            ("BLOCK_LINE_WITHOUT_AN_ASSIGNMENT", head + "Behavior = BHV_Waypoint\n{\n prose\n}\n"),
            ("DUPLICATE_BEHAVIOR_NAME", head + block + block),
            ("INITIALIZE_WITHOUT_A_VALUE", "initialize DEPLOY =\n" + block),
            ("INITIALIZE_WITHOUT_A_VARIABLE", "initialize = true\n" + block),
            ("BEHAVIOR_HEADER_MALFORMED", head + "Behavior =\n{\n}\n"),
            ("LINE_OUTSIDE_ANY_DECLARED_BLOCK", head + "set\n{\n}\n"),
            ("MODE_SET_HEADER_MALFORMED", head + "set  \n{\n}\n"),
            ("SHELL_VARIABLE_EXPANSION_NOT_DETERMINED_BY_THIS_FILE",
             head + "Behavior = BHV_Waypoint\n{\n speed = ${FAST}\n}\n"),
            ("CONTINUATION_AT_END_OF_FILE", head + block + "trailing \\\n"),
            ("LINE_OUTSIDE_ANY_DECLARED_BLOCK", "initializeBogus DEPLOY = 1\n" + block),
            ("TEXT_BEFORE_THE_CLOSING_BRACE",
             head + "Behavior = BHV_Waypoint\n{\n name = a }\n}\n"),
            ("TEXT_BEFORE_THE_CLOSING_BRACE", head + block + "set MODE = A {\n x = 1 }\n}\n"),
        ]
        for code, text in cases:
            self.assertEqual(self.refusal(text, "moos-behavior"), code, text)
        for good in ("initialize DEPLOY = 1\n", "initialize_ DEPLOY = 1\n"):
            self.assertEqual(self.rows_of(good + block, "moos-behavior")[1]["initialize_lines"], 1)

    def test_mode_set_bodies_are_ordered_unevaluated_declared_conditions(self):
        rows, report = self.rows_of(BEHAVIOR_C, "moos-behavior")
        self.assertEqual(report["mode_sets"], 2)
        self.assertEqual(report["mode_conditions"], 3)
        self.assertEqual(report["behavior_settings"], 2)
        self.assertEqual(report["declaration_rows"], len(rows))
        self.assertIn("UNEVALUATED", report["mode_condition_basis"])

        heads = [r for r in rows if r["declaration_kind"] == "behavior_mode_set"]
        self.assertEqual([(r["setting_name"], r["setting_value"]) for r in heads],
                         [("MODE", "ACTIVE"), ("MODE", "INACTIVE")])

        conditions = [r for r in rows if r["declaration_kind"] == "behavior_mode_condition"]
        self.assertEqual([r["scope_name"] for r in conditions],
                         ["MODE = ACTIVE", "MODE = ACTIVE", "MODE = INACTIVE"])
        self.assertEqual([r["scope_kind"] for r in conditions], ["mode_set"] * 3)
        self.assertEqual([r["assignment_order"] for r in conditions], ["1", "2", "1"])
        self.assertEqual([r["setting_value"] for r in conditions],
                         ["DEPLOY,true", "(RETURN = false) and (DEPLOY = true)", "DEPLOY,false"])
        for row in conditions:
            self.assertEqual(row["setting_name"], "")
            self.assertEqual(row["setting_name_folded"], "")
            self.assertEqual(row["value_kind"], "")
            self.assertEqual(row["value_number"], "")
            self.assertEqual(row["accumulates_upstream"], "")
            self.assertTrue(row["expression_basis"].startswith("UNEVALUATED_AUTHORED_SOURCE_EXPRESSION"))
            self.assertEqual(row["source_text_basis"], self.moos.SOURCE_EXACT)
            self.assertEqual(row["source_text"],
                             BEHAVIOR_C.splitlines()[int(row["source_line"]) - 1])
            self.assertEqual(row["source_line_end"], row["source_line"])
        self.assertEqual([r["source_line"] for r in conditions], ["11", "12", "16"])

        settings = [r for r in rows if r["declaration_kind"] == "behavior_setting"]
        self.assertEqual([(r["setting_name"], r["accumulates_upstream"]) for r in settings],
                         [("name", "overwrites"), ("speed", self.moos.ACCUMULATION_UNQUALIFIED)])
        self.assertEqual(self.rows_of(BEHAVIOR_A, "moos-behavior")[1]["declaration_rows"], 19)
        self.assertEqual(self.rows_of(BEHAVIOR_A, "moos-behavior")[1]["mode_conditions"], 0)

    def test_mode_block_refusals_still_apply_to_condition_lines(self):
        head = "initialize DEPLOY = true\n"
        cases = [  # pairs, not a dict: two shapes may legitimately share one refusal code
            ("TEXT_BEFORE_THE_CLOSING_BRACE", head + "set MODE = A {\n DEPLOY,true }\n}\n"),
            ("BRACE_IN_A_STATE_THAT_DOES_NOT_ACCEPT_ONE", head + "set MODE = A {\n{\n}\n}\n"),
            ("BLOCK_NEVER_CLOSED", head + "set MODE = A {\n DEPLOY,true\n"),
            ("NSPLUG_PREPROCESSOR_DIRECTIVE_NOT_DETERMINED_BY_THIS_FILE",
             head + "set MODE = A {\n #include conditions.bhv\n}\n"),
            ("SHELL_VARIABLE_EXPANSION_NOT_DETERMINED_BY_THIS_FILE",
             head + "set MODE = A {\n DEPLOY,${WANT}\n}\n"),
            ("COMMENT_INSIDE_QUOTED_VALUE", head + 'set MODE = A {\n NAME = "a // b"\n}\n'),
            ("MODE_SET_HEADER_MALFORMED", head + "set  \n{\n}\n"),
        ]
        for code, text in cases:
            self.assertEqual(self.refusal(text, "moos-behavior"), code, text)
        rows, report = self.rows_of(head + "set MODE = A {\n // just a note\n DEPLOY,true // why\n}\n",
                                    "moos-behavior")
        self.assertEqual(report["mode_conditions"], 1)
        self.assertEqual([r["setting_value"] for r in rows
                          if r["declaration_kind"] == "behavior_mode_condition"], ["DEPLOY,true"])

    def test_directives_are_refused_and_genuine_comments_stay_comments(self):
        block = "Behavior = BHV_Waypoint\n{\n name = a\n}\n"
        rows, report = self.rows_of("# a genuine hash comment\n// and a slash comment\n" + block,
                                   "moos-behavior")
        self.assertEqual(report["comment_lines"], 2)
        self.assertEqual(report["behavior_blocks"], 1)
        self.assertTrue(rows)
        for text in ("#include common.bhv\n" + block,
                     "#ifdef FAST\n" + block,
                     "Behavior = BHV_Waypoint\n{\n  #include more.bhv\n  name = a\n}\n"):
            self.assertEqual(self.refusal(text, "moos-behavior"),
                             "NSPLUG_PREPROCESSOR_DIRECTIVE_NOT_DETERMINED_BY_THIS_FILE", text)
        self.assertEqual(self.refusal("define: SPEED = 2\n" + block, "moos-behavior"),
                         "READER_DEFINE_VARIABLE_NOT_DETERMINED_BY_THIS_FILE")

    def test_the_second_authored_input_reuses_the_same_importer(self):
        mission_rows, mission_report = self.rows_of(MISSION_B, "moos-mission")
        self.assertEqual(mission_report["process_blocks"], 1)
        ports = [r for r in mission_rows if r["setting_name"] == "ServerPort"]
        self.assertEqual([r["setting_value"] for r in ports], ["9100", "9101"])
        self.assertEqual([r["setting_occurrence"] for r in ports], ["1", "2"])
        self.assertEqual([r["scope_kind"] for r in ports], ["global", "global"])

        behavior_rows, behavior_report = self.rows_of(BEHAVIOR_B, "moos-behavior")
        self.assertEqual(behavior_report["behavior_blocks"], 1)
        self.assertEqual(behavior_report["continued_lines"], 1,
                         "the trailing backslash continuation is joined as upstream joins it")
        avoid = {r["setting_name"]: r for r in behavior_rows if r["setting_name"]}
        self.assertEqual(avoid["pwt_outer_dist"]["setting_value_stripped"], "50")
        joined = avoid["pwt_outer_dist"]
        self.assertEqual((joined["source_line"], joined["source_line_end"]), ("7", "8"))
        self.assertEqual(joined["source_text"], "  pwt_outer_dist = 50 \\\n")
        self.assertEqual(joined["lexical_text"], "  pwt_outer_dist = 50 ")
        self.assertEqual(joined["source_text_basis"], self.moos.SOURCE_RECONSTRUCTED)
        self.assertEqual(avoid["match_type"]["source_text_basis"], self.moos.SOURCE_EXACT)
        self.assertEqual(avoid["match_type"]["accumulates_upstream"],
                         self.moos.ACCUMULATION_UNQUALIFIED)
        self.assertEqual(avoid["match_type"]["setting_value"], "ferry")
        self.assertEqual(avoid["condition"]["expression_basis"].split("_")[0], "UNEVALUATED")
        self.assertNotEqual(mission_report["source_sha256"], behavior_report["source_sha256"])

    def test_a_practical_size_mission_stays_inside_the_bounds(self):
        blocks = []
        for index in range(200):
            blocks.append(f"ProcessConfig = pApp{index}\n{{\n" + "".join(
                f"  param{item} = value_{index}_{item}\n" for item in range(10)) + "}\n")
        text = "ServerHost = localhost\n" + "".join(blocks)
        rows, report = self.rows_of(text, "moos-mission")
        self.assertEqual(report["process_blocks"], 200)
        self.assertEqual(report["process_settings"], 2_000)
        self.assertEqual(len(rows), 1 + 200 + 2_000)
        self.assertLess(len(json.dumps(report)), self.moos.REPORT_LIMIT)
        self.assertEqual({r["scope_occurrence"] for r in rows if r["scope_kind"] == "process"}, {"1"})


    def test_cli_writes_rows_accounting_and_the_retained_source(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "alpha.moos"
            source.write_text(MISSION_A, encoding="utf-8")
            target = Path(work) / "out"
            done = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target),
                                   "--declaration-format", "moos-mission",
                                   "--capture-time-us", str(CAPTURE)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(sorted(p.name for p in target.iterdir()),
                             ["accounting.json", "observations.csv", "source.declaration"])
            self.assertEqual((target / "source.declaration").read_text(encoding="utf-8"), MISSION_A)
            report = json.loads((target / "accounting.json").read_text(encoding="utf-8"))
            self.assertEqual(report["pin"], "moos-ivp-f56c6907")
            self.assertIn("NO_TIME_EXISTS_IN_THE_SAVED_FILE", report["clock"])
            again = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target),
                                    "--declaration-format", "moos-mission",
                                    "--capture-time-us", str(CAPTURE)],
                                   capture_output=True, text=True, check=False)
            self.assertEqual(again.returncode, 2)
            bad = Path(work) / "bad.moos"
            bad.write_text("ProcessConfig = pFoo {\n}\n", encoding="utf-8")
            refused = subprocess.run([sys.executable, str(SCRIPT), str(bad), str(Path(work) / "no"),
                                      "--declaration-format", "moos-mission",
                                      "--capture-time-us", str(CAPTURE)],
                                     capture_output=True, text=True, check=False)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("PROCESS_HEADER_CARRIES_ITS_OPENING_BRACE", refused.stderr)
            self.assertFalse((Path(work) / "no").exists())

    def test_a_failure_after_the_directory_exists_says_the_contents_are_incomplete(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "alpha.moos"
            source.write_text(MISSION_A, encoding="utf-8")
            target = Path(work) / "partial"
            written = Path.write_text

            def fail_on_the_accounting(self, *arguments, **keywords):
                if self.name == "accounting.json":
                    raise OSError("no space left on device")
                return written(self, *arguments, **keywords)

            stderr = io.StringIO()
            with mock.patch.object(Path, "write_text", fail_on_the_accounting), \
                 mock.patch.object(sys, "argv",
                                   ["convert_moos_declarations.py", str(source), str(target),
                                    "--declaration-format", "moos-mission",
                                    "--capture-time-us", str(CAPTURE)]), \
                 contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as stopped:
                self.moos.main()
            self.assertEqual(stopped.exception.code, 2)
            self.assertTrue((target / "observations.csv").exists())
            self.assertIn("incomplete", stderr.getvalue())
            self.assertNotIn("nothing was written", stderr.getvalue())
            stderr = io.StringIO()
            with mock.patch.object(sys, "argv",
                                   ["convert_moos_declarations.py", str(source), str(target),
                                    "--declaration-format", "moos-mission",
                                    "--capture-time-us", str(CAPTURE)]), \
                 contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                self.moos.main()
            self.assertIn("nothing was written", stderr.getvalue())

    def observations_of(self, text, declaration_format):
        output, report = self.moos.convert(text.encode(), CAPTURE,
                                          declaration_format=declaration_format)
        with tempfile.TemporaryDirectory() as work:
            rows = Path(work) / "observations.csv"
            rows.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(rows),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
        return common, report

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "set MUSUBI_TELEMETRY_READER to read the declarations as common Observations")
    def test_mode_conditions_reach_common_observations_in_order(self):
        common, report = self.observations_of(BEHAVIOR_C, "moos-behavior")
        self.assertEqual(common["main_rows"], report["declaration_rows"])
        self.assertEqual(common["platform_domain"], "Unknown")
        conditions = [o["fields"] for o in common["observations"]
                      if o["fields"]["declaration_kind"] == "behavior_mode_condition"]
        self.assertEqual(len(conditions), 3)

        def authored(fields, column="setting_value_hex"):
            return bytes.fromhex(fields[column][4:]).decode()

        self.assertEqual([(f["scope_name"], f["assignment_order"], authored(f)) for f in conditions],
                         [("MODE = ACTIVE", 1.0, "DEPLOY,true"),
                          ("MODE = ACTIVE", 2.0, "(RETURN = false) and (DEPLOY = true)"),
                          ("MODE = INACTIVE", 1.0, "DEPLOY,false")])
        self.assertEqual([f["source_line"] for f in conditions], [11.0, 12.0, 16.0])
        for fields in conditions:
            self.assertIsNone(fields["value_kind"])
            self.assertIsNone(fields["accumulates_upstream"])
            self.assertIn("UNEVALUATED", fields["expression_basis"])
            self.assertIn(authored(fields), authored(fields, "source_text_hex"))

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "set MUSUBI_TELEMETRY_READER to read the declarations as common Observations")
    def test_declarations_reach_common_observations(self):
        for text, declaration_format, expected in ((MISSION_A, "moos-mission", "pHelmIvP"),
                                                   (BEHAVIOR_A, "moos-behavior", "BHV_Waypoint")):
            output, report = self.moos.convert(text.encode(), CAPTURE,
                                              declaration_format=declaration_format)
            with tempfile.TemporaryDirectory() as work:
                rows = Path(work) / "observations.csv"
                rows.write_text(output, encoding="utf-8")
                common = json.loads(subprocess.run(
                    [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(rows),
                     "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], report["declaration_rows"])
            self.assertEqual(common["platform_domain"], "Unknown",
                             "no domain is inferred from a mission or behavior file")
            first = common["observations"][0]
            self.assertEqual(first["clock_basis"], "Unknown")
            self.assertEqual(first["t_ms"] * 1000, CAPTURE)
            self.assertTrue(any(o["fields"]["scope_name"] == expected
                                for o in common["observations"]), expected)

            def authored_of(observation):
                value = observation["fields"].get("setting_value_hex") or ""
                return bytes.fromhex(value[4:]).decode() if value.startswith("hex:") else ""

            authored = {authored_of(o) for o in common["observations"]}
            if declaration_format == "moos-mission":
                self.assertIn("course:0:359:360", authored)
                self.assertIn("43.825300", authored)
                origin = next(o for o in common["observations"]
                              if o["fields"].get("setting_name") == "LatOrigin")
                self.assertEqual(origin["fields"]["value_number"], 43.8253)
                self.assertIn("43.825300",
                              bytes.fromhex(origin["fields"]["source_text_hex"][4:]).decode())
            else:
                self.assertIn("(RETURN = false) and (DEPLOY = true)", authored)
                self.assertIn("60,-40 : 60,-160 : 150,-160", authored,
                              "a comma-bearing value survives without a quoted cell")


if __name__ == "__main__":
    unittest.main()
