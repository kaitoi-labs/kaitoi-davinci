"""Tests that need neither Resolve nor a network: the tool model, templates, checks, the busy marker, pure bridge helpers."""

from __future__ import annotations

from . import HOME  # noqa: F401 - first: outside the tests package, where home is not redirected, this file does not load

import json
import os
import re
import tempfile
import unittest
from unittest import mock

from kaitoi_resolve import busy, checks, config, history, resolve_bridge, templates, tools, uninstall


def field(name, data_type, **extra):
    return tools.Field(name=name, label=extra.pop("label", name.title()), data_type=data_type, **extra)


def tool(inputs=(), outputs=(), **extra):
    base = dict(id="workflow:e1", source="workflow", name="Rain", description="Make it rain.", category="Weather", tags=("storm",))
    base.update(extra)
    return tools.Tool(inputs=tuple(inputs), outputs=tuple(outputs), **base)


V2V = tool([field("video", "video"), field("prompt", "string")], [tools.Output("video", "Result", "video")])
CREATE = tool([field("color", "color")], [tools.Output("image", "Card", "image")], id="workflow:e2", name="Card")
MIX = tool([field("video", "video"), field("audio", "audio")], [tools.Output("video", "Out", "video"), tools.Output("audio", "Stem", "audio")], id="workflow:e3", name="Mix")


class ToolModel(unittest.TestCase):
    def test_types_and_widgets(self):
        self.assertEqual(tools.normalize_type("Integer"), "int")
        self.assertEqual(tools.normalize_type("image[]"), "image")
        self.assertEqual(field("x", "video").widget, "media")
        self.assertEqual(field("x", "string", options=("a", "b")).widget, "choice")
        self.assertEqual(field("x", "bool").widget, "bool")
        self.assertEqual(field("x", "float").widget, "number")
        self.assertTrue(field("prompt", "string").is_prompt)
        self.assertFalse(field("resolution", "string").is_prompt)

    def test_tasks_in_words(self):
        self.assertEqual(tools.task_label(V2V), "Video → Video")
        self.assertEqual(tools.task_label(CREATE), "Create Image")
        self.assertEqual(tools.task_label(MIX), "Video + Audio → Video + Audio")
        self.assertEqual(tools.task_key(MIX), "video+audio>video+audio")

    def test_fit_follows_what_the_timeline_supplies(self):
        self.assertTrue(tools.fits(V2V, {"video", "image", "audio"}))
        self.assertFalse(tools.fits(V2V, {"audio"}))
        self.assertTrue(tools.fits(CREATE, set()), "a tool that takes no media always fits")
        self.assertEqual(tools.primary_input(MIX, {"audio"}).name, "audio")
        self.assertIsNone(tools.primary_input(V2V, set()))

    def test_search_reads_tags_but_needs_every_word(self):
        self.assertTrue(tools.matches(V2V, "storm"))
        self.assertTrue(tools.matches(V2V, "rain video"))
        self.assertFalse(tools.matches(V2V, "rain snow"))

    def test_filter_and_group(self):
        everything = [V2V, CREATE, MIX]
        self.assertEqual([t.name for t in tools.filter_tools(everything, supplies=set())], ["Card"])
        self.assertEqual([t.name for t in tools.filter_tools(everything, task="video>video")], ["Rain"])
        self.assertEqual([name for name, _ in tools.group(everything)], ["Weather"])

    def test_form_values(self):
        number = field("speed", "float", minimum=0.5, maximum=4)
        self.assertEqual(tools.parse_value(number, " 2,5 "), 2.5)
        self.assertIsNone(tools.parse_value(number, ""), "an empty box leaves the value to the workflow")
        for bad in ("abc", "9", "0.1"):
            with self.assertRaises(tools.ToolInputError):
                tools.parse_value(number, bad)
        with self.assertRaises(tools.ToolInputError):
            tools.parse_value(field("n", "int"), "2.5")
        self.assertIs(tools.parse_value(field("b", "bool"), "On"), True)
        for leave in ("Workflow default", "Node default"):
            self.assertIsNone(tools.parse_value(field("b", "bool"), leave))
        self.assertEqual((tools.leave_label("node"), tools.leave_label("workflow")), ("Node default", "Workflow default"))
        self.assertEqual(tools.leave_label("workflow", "Zoom In"), "Workflow default (Zoom In)")
        for leave in ("Workflow default (Zoom In)", "Node default (0.5)", "Node default"):
            self.assertTrue(tools.is_leave_label(leave))
            self.assertIsNone(tools.parse_value(field("m", "string", options=("a", "b")), leave))
        for real in ("Zoom In", "Workflow defaults", "Node default-ish (x)", ""):
            self.assertFalse(tools.is_leave_label(real))
        self.assertEqual(tools.default_label(field("m", "string", default="b", options=("a", "b"), option_labels=("A", "B"))), "B")
        self.assertEqual(tools.default_label(field("k", "bool", default=False)), "Off")
        self.assertIsNone(tools.default_label(field("n", "int", default=5)), "a number shows in its own box, not in an entry")
        self.assertEqual(tools.parse_value(field("c", "color"), "22cc88"), "#22cc88")
        with self.assertRaises(tools.ToolInputError):
            tools.parse_value(field("c", "color"), "green")
        choice = field("m", "string", options=("zoom_in", "zoom_out"))
        self.assertEqual(tools.parse_value(choice, "zoom_out"), "zoom_out")
        with self.assertRaises(tools.ToolInputError):
            tools.parse_value(choice, "sideways")

    def test_a_choice_is_shown_by_its_label_and_sent_by_its_value(self):
        details = tools.pin_details({"options": [{"value": "zoom_in", "label": "Zoom In"}, {"value": 30, "label": ""}, {"label": "no value"}, {"value": [1]}], "min": 1, "max": True, "step": "2"})
        self.assertEqual(details["options"], ("zoom_in", 30))
        self.assertEqual(details["option_labels"], ("Zoom In", "30"))
        self.assertEqual((details["minimum"], details["maximum"], details["step"]), (1, None, None), "a range that is not a number is no range")
        choice = field("m", "string", **{k: details[k] for k in ("options", "option_labels")})
        self.assertEqual(choice.choices, (("zoom_in", "Zoom In"), (30, "30")))
        self.assertEqual(tools.parse_value(choice, "Zoom In"), "zoom_in")
        self.assertEqual(tools.parse_value(choice, "30"), 30, "a number stays a number")
        self.assertEqual(choice.choice_label("zoom_in"), "Zoom In")
        self.assertIsNone(choice.choice_label("sideways"))
        plain = tools.pin_details({"options": ["mp3", "wav"]})
        self.assertEqual((plain["options"], plain["option_labels"]), (("mp3", "wav"), ()))
        self.assertEqual(tools.pin_details({"options": "model.options"})["options"], (), "a list only known while the node runs")
        blank = field("preset", "string", **{k: v for k, v in tools.pin_details({"options": ["", "3d-model"]}).items() if k in ("options", "option_labels")})
        self.assertEqual(blank.choices, (("", "None"), ("3d-model", "3d-model")), "an empty choice is shown by name, or nobody could pick it")
        self.assertEqual(tools.parse_value(blank, "None"), "")
        self.assertEqual(tools.parse_value(blank, ""), "", "for this input, empty is a value, not 'leave it'")
        self.assertIsNone(tools.parse_value(field("text", "string"), ""), "for any other, an empty box leaves it to the tool")
        self.assertEqual(tools.pin_details({})["options"], ())

    def test_an_endpoint_input_carries_its_choices_and_range(self):
        contract = {"inputs": [
            {"name": "Motion", "fieldName": "motion", "dataType": "string", "options": [{"value": "zoom_in", "label": "Zoom In"}]},
            {"name": "Duration", "fieldName": "duration", "dataType": "int", "min": 1, "max": 30, "step": 1},
        ], "outputs": []}
        made = tools.tool_from_endpoint({"templateId": "t", "name": "Move"}, {"endpointId": "e"}, contract)
        self.assertEqual(made.inputs[0].choices, (("zoom_in", "Zoom In"),))
        self.assertEqual((made.inputs[1].minimum, made.inputs[1].maximum, made.inputs[1].step), (1, 30, 1))

    def test_request_inputs_leave_out_what_was_not_set(self):
        inputs = tools.build_inputs(V2V, {"prompt": None}, {"video": "file-1"})
        self.assertEqual(inputs, {"video": {"type": "file", "fileId": "file-1"}})

    def test_endpoint_contract_becomes_a_tool(self):
        template = {"templateId": "t2", "name": "Looks", "category": None, "tags": [], "endpoints": [{}, {}]}
        made = tools.tool_from_endpoint(template, {"endpointId": "e2", "name": "Warm"}, {"inputs": [{"name": "Clip", "fieldName": "video", "dataType": "video"}], "outputs": []})
        self.assertEqual((made.name, made.category, made.id), ("Looks: Warm", tools.DEFAULT_CATEGORY, "workflow:e2"))

    def test_an_input_the_node_fills_in_itself_is_a_plain_box_with_no_made_up_default(self):
        detail = {"type": "builtin/tts", "title": "TTS", "source": "builtin", "outputs": [], "inputs": [
            {"name": "selectedVoice", "type": "string", "label": "Voice", "default": "selectedVoice.value", "options": None},
            {"name": "note", "type": "string", "label": "Note", "default": "see notes.value for details"},
            {"name": "format", "type": "string", "default": "mp3", "options": [{"value": "mp3", "label": "mp3"}]},
        ]}
        node = tools.node_from_detail(detail)
        voice, note, fmt = node.inputs
        self.assertEqual((voice.default, voice.dynamic, voice.widget), (None, True, "text"), "a pointer into the node's state is not a value")
        self.assertEqual((note.default, note.dynamic), ("see notes.value for details", False), "ordinary text that mentions one is left alone")
        self.assertEqual((fmt.default, fmt.dynamic), ("mp3", False))
        self.assertEqual(tools.node_from_summary(tools._node_row(node)), node, "and it survives being stored")
        self.assertEqual(templates.tool_from_dict(templates.tool_to_dict(node)).inputs, node.inputs)

    def test_node_pins(self):
        detail = {
            "type": "builtin/video/x", "title": "X", "category": "Video Editing", "source": "builtin",
            "inputs": [{"name": "inputVideo", "type": "video"}, {"name": "speed", "type": "float", "default": 1.0}, {"name": "model", "type": "node"}],
            "outputs": [{"name": "outputVideo", "type": "video"}, {"name": "self", "type": "node"}],
        }
        node = tools.node_from_detail(detail)
        self.assertEqual([f.name for f in node.inputs], ["inputVideo", "speed"], "a pin a form cannot hold is left to the node")
        self.assertEqual([o.name for o in node.outputs], ["outputVideo"])
        self.assertTrue(node.ready)
        self.assertEqual(tools.node_from_summary(detail), node, "a catalog entry that carries its pins is ready as it stands")
        summary = tools.node_from_summary({key: value for key, value in detail.items() if key not in ("inputs", "outputs")})
        self.assertFalse(summary.ready)
        self.assertEqual(tools.task_label(summary), "Built-in")
        self.assertTrue(tools.fits(summary, set()), "nothing is known against a node that has not loaded")
        self.assertEqual(tools.origins([summary]), [("builtin", "Built-in")])
        self.assertEqual([t.name for t in tools.filter_tools([summary], origin="community")], [])


class TestsStayOutOfTheRealHome(unittest.TestCase):
    def test_the_plugins_folders_are_below_a_home_made_for_this_run(self):
        import tests

        self.assertIs(HOME, tests.HOME)
        self.assertNotEqual(os.path.realpath(tests.HOME), os.path.realpath(tests.REAL_HOME))
        self.assertEqual(os.path.expanduser("~"), tests.HOME)
        inside = os.path.realpath(tests.HOME) + os.sep
        # First the one that is only looked at: the others make their folder when asked where it is.
        self.assertTrue(os.path.realpath(config.APP_DIR).startswith(inside), config.APP_DIR)
        from kaitoi_resolve import credentials

        for path in (config.APP_DIR, templates.templates_root(), templates._manifest_path(), tools.snapshot_path(), tools.nodes_path(), credentials.path()):
            self.assertTrue(os.path.realpath(path).startswith(inside), path)


    def test_a_test_file_started_outside_the_package_does_not_run(self):
        """Started so that tests/__init__.py is skipped, home would not be redirected: every file must refuse to load."""
        import subprocess
        import sys

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"], cwd=repo, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(out.returncode, 0)
        self.assertRegex(out.stderr, r"Ran 4 tests")
        self.assertIn("errors=4", out.stderr, "the four files, each failing to load; not one test ran")
        self.assertEqual(out.stderr.count("attempted relative import"), 4, out.stderr[-600:])
        for name in ("test_offline", "test_signin", "test_network", "test_panel"):
            out = subprocess.run([sys.executable, "-B", os.path.join("tests", name + ".py")], cwd=repo, env={**os.environ, "PYTHONPATH": repo},
                                 capture_output=True, text=True, timeout=120)
            self.assertNotEqual(out.returncode, 0, name)
            self.assertIn("attempted relative import", out.stderr, name)


class Templates(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patch_root = mock.patch.object(templates, "templates_root", lambda: os.path.join(self.dir.name, "Templates"))
        patch_app = mock.patch.object(config, "APP_DIR", os.path.join(self.dir.name, "app"))
        patch_root.start(); patch_app.start()
        self.addCleanup(patch_root.stop); self.addCleanup(patch_app.stop)

    def spec_of(self, text):
        raw = re.search(r'KaitoiSpec = \{.*?INPS_DefaultText = "((?:[^"\\]|\\.)*)"', text, re.S).group(1)
        return json.loads(raw.replace('\\"', '"').replace("\\\\", "\\"))

    def test_effect_feeds_the_clip_and_draws_the_rest(self):
        rich = tool(
            [field("video", "video"), field("prompt", "string"), field("speed", "float", default=1.0, minimum=0.25, maximum=4), field("keep", "bool", default=True),
             field("mode", "string", options=("a", "b")), field("music", "audio"), field("seed", "int")],
            [tools.Output("video", "Result", "video")],
        )
        text = templates.effect_setting(rich)
        self.assertIn("MainInput1 = InstanceInput", text)
        self.assertNotIn("K_video", text, "the clip the effect sits on needs no control")
        for needle in ('K_prompt', 'TEC_Lines = 4', '"SliderControl"', '"CheckboxControl"', '"ComboControl"', '"FileControl"', "KaitoiGenerate", "KaitoiCancel", "KaitoiStatus"):
            self.assertIn(needle, text)
        spec = self.spec_of(text)
        reads = {c["field"]: c for c in spec["controls"]}
        self.assertEqual(reads["speed"]["read"], "number")
        self.assertEqual(reads["keep"]["read"], "check")
        self.assertEqual((reads["mode"]["read"], reads["mode"]["offset"]), ("combo", 1))
        self.assertEqual(reads["music"]["read"], "file")
        self.assertEqual(reads["seed"]["read"], "text", "a number with no known default is typed, so empty can mean the workflow's own")
        self.assertEqual(templates.tool_from_dict(spec["tool"]), rich)

    def test_a_choice_control_lists_labels_and_reads_back_values(self):
        picky = tool(
            [field("motion", "string", default="zoom_out", options=("zoom_in", "zoom_out"), option_labels=("Zoom In", "Zoom Out")),
             field("fps", "int", options=(24, 30))],
            [tools.Output("video", "Result", "video")],
        )
        text = templates.generator_setting(picky)
        for needle in ('CCS_AddString = "Zoom In"', 'CCS_AddString = "Zoom Out"', 'CCS_AddString = "24"'):
            self.assertIn(needle, text)
        self.assertNotIn('CCS_AddString = "zoom_in"', text, "the list shows labels, not the values behind them")
        reads = {c["field"]: c for c in self.spec_of(text)["controls"]}
        self.assertEqual((reads["motion"]["options"], reads["motion"]["offset"]), (["zoom_in", "zoom_out"], 1))
        self.assertIn('CCS_AddString = "Workflow default (Zoom Out)"', text, "the entry that sends nothing says what the workflow will use")
        self.assertIsNotNone(re.search(r"K_motion = \{[^}]*?INP_Default = 0,", text), "it starts there, so a value changed later in Studio is the one that runs")
        self.assertIn('CCS_AddString = "Workflow default"', text, "with no known default it says only that")
        self.assertEqual((reads["fps"]["options"], reads["fps"]["offset"]), ([24, 30], 1), "a number is sent as a number")
        self.assertEqual(templates.tool_from_dict(self.spec_of(text)["tool"]), picky)

    def test_a_node_template_says_node_default_and_a_workflow_template_workflow_default(self):
        inputs = [field("mode", "string", options=("a", "b")), field("keep", "bool")]
        as_workflow = templates.generator_setting(tool(inputs, [tools.Output("video", "Result", "video")]))
        as_node = templates.generator_setting(tool(inputs, [tools.Output("video", "Result", "video")], id="node:builtin/x", source="node"))
        self.assertEqual((as_workflow.count('CCS_AddString = "Workflow default"'), as_workflow.count('"Node default"')), (2, 0))
        self.assertEqual((as_node.count('CCS_AddString = "Node default"'), as_node.count('"Workflow default"')), (2, 0))

    def test_generator_feeds_the_first_media_input(self):
        text = templates.generator_setting(MIX)
        self.assertNotIn("MainInput1", text)
        self.assertNotIn("K_video", text)
        self.assertIn("K_audio", text)
        self.assertIn("TopLeftAlpha", text, "a generator must render transparent so what is beneath it shows")

    def test_buttons_mark_the_action_and_only_generate_launches(self):
        text = templates.generator_setting(CREATE)
        generate = re.search(r'KaitoiGenerate = \{.*?BTNCS_Execute = "((?:[^"\\]|\\.)*)"', text, re.S).group(1)
        cancel = re.search(r'KaitoiCancel = \{.*?BTNCS_Execute = "((?:[^"\\]|\\.)*)"', text, re.S).group(1)
        self.assertIn(templates.ACTION_KEY, generate)
        self.assertIn(templates.CANCEL_KEY, cancel)
        self.assertIn('\\"yes\\" ~= \\"yes\\"', generate)
        self.assertIn('\\"no\\" ~= \\"yes\\"', cancel)

    def test_names_are_safe_for_files(self):
        self.assertEqual(templates.template_name(tool(name="Looks: Warm / Cold?")), "Looks Warm Cold")
        self.assertTrue(templates.has_effect(V2V))
        self.assertFalse(templates.has_effect(CREATE))

    def test_sync_installs_and_removes_only_its_own(self):
        templates.sync([V2V, CREATE])
        generators = templates.folder("Generators")
        self.assertEqual(sorted(os.listdir(generators)), ["Card.setting", "Rain.setting"])
        self.assertEqual(os.listdir(templates.folder("Effects")), ["Rain.setting"])
        foreign = os.path.join(generators, "Mine.setting")
        with open(foreign, "w") as fh:
            fh.write("{}")
        templates.sync([CREATE])
        self.assertEqual(sorted(os.listdir(generators)), ["Card.setting", "Mine.setting"], "a file Kaitoi did not write is never removed")
        self.assertEqual(os.listdir(templates.folder("Effects")), [])


    def test_install_state_and_removal(self):
        self.assertEqual(templates.state(V2V.id, set()), templates.NOT_INSTALLED)
        entry = templates.install(V2V)
        self.assertEqual((entry["name"], entry["kinds"]), ("Rain", ["Generators", "Effects"]))
        self.assertEqual(templates.state(V2V.id, set()), templates.RESTART, "it takes a clip, and Resolve has not loaded its effect")
        self.assertEqual(templates.state(V2V.id, {"rain"}), templates.READY)
        templates.install(CREATE)
        self.assertEqual(templates.state(CREATE.id, set()), templates.READY, "a generator needs no restart")
        self.assertTrue(templates.uninstall(V2V.id))
        self.assertFalse(templates.uninstall(V2V.id))
        self.assertEqual(os.listdir(templates.folder("Effects")), [])
        self.assertEqual(sorted(templates.installed()), [CREATE.id])
        self.assertEqual(templates.uninstall_all(), 1)
        self.assertEqual(os.listdir(templates.folder("Generators")), [])

    def test_remove_everything_takes_unrecorded_kaitoi_templates_and_nothing_else(self):
        templates.install(CREATE)
        generators = templates.folder("Generators")
        old = os.path.join(generators, "Old One.setting")  # written before the plugin kept a record
        with open(old, "w") as fh:
            fh.write(templates.generator_setting(V2V))
        with open(os.path.join(generators, "Old One.png"), "wb") as fh:
            fh.write(b"png")
        with open(os.path.join(generators, "Mine.setting"), "w") as fh:
            fh.write("{ Tools = {} }")
        with open(os.path.join(generators, "Mine.png"), "wb") as fh:
            fh.write(b"png")
        self.assertEqual(templates.uninstall_all(), 2)
        self.assertEqual(sorted(os.listdir(generators)), ["Mine.png", "Mine.setting"], "a file without Kaitoi's marker is never removed")
        self.assertEqual(templates.installed(), {})

    def test_two_tools_with_one_title_do_not_overwrite_each_other(self):
        twin = tool([field("video", "video")], [tools.Output("video", "Out", "video")], id="node:user/other", source="node", name="Rain")
        first, second = templates.install(V2V), templates.install(twin)
        self.assertEqual((first["name"], second["name"]), ("Rain", "Rain 2"))
        self.assertEqual(templates.install(twin)["name"], "Rain 2", "installing again keeps its name")
        self.assertEqual(sorted(os.listdir(templates.folder("Effects"))), ["Rain 2.setting", "Rain.setting"])

    def test_a_title_differing_only_in_case_gets_its_own_name(self):
        twin = tool([field("video", "video")], [], id="node:user/lower", source="node", name="rain")
        self.assertEqual((templates.install(V2V)["name"], templates.install(twin)["name"]), ("Rain", "rain 2"), "most disks would treat them as one file")

    def test_someone_elses_template_of_the_same_name_is_not_written_over(self):
        generators = templates.folder("Generators")
        os.makedirs(generators)
        theirs = os.path.join(generators, "Rain.setting")
        with open(theirs, "wb") as fh:
            fh.write(b"{ Tools = {} } \xff\xfe not text Kaitoi can read")
        entry = templates.install(V2V)
        self.assertEqual(entry["name"], "Rain 2")
        with open(theirs, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"{ Tools = {} }"), "their file is as they left it")
        templates.uninstall(V2V.id)
        self.assertEqual(os.listdir(generators), ["Rain.setting"])

    def test_only_files_inside_a_kaitoi_folder_are_ever_removed(self):
        outside = os.path.join(self.dir.name, "Templates", "Edit", "Generators", "Precious.setting")
        os.makedirs(os.path.dirname(outside))
        with open(outside, "w") as fh:
            fh.write(templates.generator_setting(V2V))  # even one that carries Kaitoi's marker
        templates._remove_files([outside])
        self.assertTrue(os.path.isfile(outside))
        os.makedirs(os.path.dirname(templates._manifest_path()), exist_ok=True)
        with open(templates._manifest_path(), "w") as fh:
            json.dump({"tools": {"workflow:x": {"name": "Precious", "kinds": ["Generators"], "files": [outside]}}, "files": [outside]}, fh)
        templates.uninstall_all()
        self.assertTrue(os.path.isfile(outside), "a record pointing outside the Kaitoi folders is not obeyed")

    def test_remove_everything_counts_files_an_older_version_recorded(self):
        generators = templates.folder("Generators")
        os.makedirs(generators)
        old = os.path.join(generators, "Old.setting")
        with open(old, "w") as fh:
            fh.write(templates.generator_setting(V2V))
        os.makedirs(os.path.dirname(templates._manifest_path()), exist_ok=True)
        with open(templates._manifest_path(), "w") as fh:
            json.dump({"files": [old]}, fh)
        self.assertEqual(templates.uninstall_all(), 1)
        self.assertEqual(os.listdir(generators), [])

    def test_a_tool_that_stops_taking_a_clip_loses_its_effect_and_keeps_its_icon_record(self):
        icon = os.path.join(self.dir.name, "icon.png")
        with open(icon, "wb") as fh:
            fh.write(b"png")
        first = templates.install(V2V, icon=icon)
        self.assertEqual(sorted(os.path.basename(path) for path in first["files"]), ["Rain.png", "Rain.png", "Rain.setting", "Rain.setting"])
        no_clip = tool([field("prompt", "string")], [tools.Output("video", "Result", "video")])
        second = templates.install(no_clip, icon=icon)
        self.assertEqual(second["kinds"], ["Generators"])
        self.assertEqual(os.listdir(templates.folder("Effects")), [], "its effect and the effect's icon are gone")
        templates.uninstall(no_clip.id)
        self.assertEqual(os.listdir(templates.folder("Generators")), [], "the icon was recorded, so it went with the template")

    def test_a_resolve_that_cannot_list_effects_never_asks_for_a_restart(self):
        templates.install(V2V)
        self.assertEqual(templates.state(V2V.id, None), templates.READY)
        self.assertEqual(templates.state("workflow:none", None), templates.NOT_INSTALLED)

    def test_two_processes_installing_at_once_lose_nothing(self):
        import subprocess, sys

        script = (
            "import os, sys\n"
            "from kaitoi_resolve import config, templates, tools\n"
            "root, who = sys.argv[1], sys.argv[2]\n"
            "config.APP_DIR = os.path.join(root, 'app')\n"
            "templates.templates_root = lambda: os.path.join(root, 'Templates')\n"
            "for n in range(25):\n"
            "    templates.install(tools.Tool(id=f'node:{who}/{n}', source='node', name=f'{who} {n}', description='', category='', tags=(), inputs=(), outputs=()))\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        running = [subprocess.Popen([sys.executable, "-c", script, self.dir.name, who], cwd=repo, env=env) for who in ("a", "b", "c")]
        self.assertEqual([process.wait(timeout=60) for process in running], [0, 0, 0])
        recorded = templates.installed()
        self.assertEqual(len(recorded), 75, "every install is on record")
        self.assertEqual(len([name for name in os.listdir(templates.folder("Generators")) if name.endswith(".setting")]), 75)
        self.assertEqual([name for name in os.listdir(os.path.join(self.dir.name, "app")) if name.endswith(".part")], [], "no half-written file left behind")

    def test_sync_leaves_tools_of_another_kind_installed(self):
        node = tool([field("video", "video")], [], id="node:builtin/x", source="node", name="Node X")
        templates.install(node)
        templates.sync([V2V])
        templates.sync([])
        self.assertEqual(sorted(templates.installed()), ["node:builtin/x"], "unpublishing workflows must not remove installed nodes")


class Sliders(unittest.TestCase):
    def test_a_number_with_a_range_becomes_a_slider_with_sensible_stops(self):
        whole = field("duration", "int", minimum=1, maximum=30, step=1, default=5)
        scale = tools.slider_scale(whole)
        self.assertEqual(scale, (1, 29))
        self.assertEqual([tools.slider_value(whole, at, scale) for at in (0, 4, 29, 99)], [1, 5, 30, 30])
        self.assertEqual(tools.slider_position(whole, 5, scale), 4)
        self.assertEqual((tools.slider_position(whole, -3, scale), tools.slider_position(whole, 999, scale)), (0, 29))

        volume = field("volume", "float", minimum=0.0, maximum=2.0, step=0.01)
        scale = tools.slider_scale(volume)
        self.assertEqual(scale[1], 200)
        self.assertEqual(tools.slider_value(volume, 137, scale), 1.37, "no stray digits from adding small steps")
        self.assertEqual(tools.slider_position(volume, 1.37, scale), 137)

        bitrate = field("bitrate", "int", minimum=64, maximum=320, step=32)
        scale = tools.slider_scale(bitrate)
        self.assertEqual([tools.slider_value(bitrate, at, scale) for at in range(scale[1] + 1)], [64, 96, 128, 160, 192, 224, 256, 288, 320])

    def test_a_range_too_fine_or_missing_is_handled(self):
        frames = field("frame", "int", minimum=0, maximum=99999, step=1)
        step, stops = tools.slider_scale(frames)
        self.assertLessEqual(stops, tools.SLIDER_MOST_STEPS)
        self.assertEqual(tools.slider_value(frames, 0, (step, stops)), 0)
        self.assertLessEqual(tools.slider_value(frames, stops, (step, stops)), 99999)
        no_step = field("gain", "float", minimum=0, maximum=1)
        self.assertEqual(tools.slider_scale(no_step)[1], 100, "with no step declared, a hundred stops")
        for unusable in (field("n", "int"), field("n", "int", minimum=5, maximum=5), field("n", "int", minimum=9, maximum=1),
                         field("n", "string", minimum=0, maximum=1), field("n", "int", minimum=0, maximum=10, options=(1, 2))):
            self.assertIsNone(tools.slider_scale(unusable))
        for untypable in (field("n", "float", minimum=-1e308, maximum=1e308), field("n", "float", minimum=0, maximum=1e-7)):
            self.assertIsNone(tools.slider_scale(untypable), "a range no slider can show is typed in a box instead")
        fine = field("n", "float", minimum=0, maximum=1, step=1e-9)
        self.assertEqual(tools.slider_scale(fine), (0.001, 1000), "a step finer than the slider has stops is widened to what it can show")
        odd = field("n", "float", minimum=0, maximum=1, step=-3)
        self.assertEqual(tools.slider_scale(odd)[1], 100, "a step that makes no sense is ignored")

    def test_numbers_are_written_the_way_a_person_would(self):
        self.assertEqual([tools.number_text(v) for v in (3, 3.0, 2.5, 0.01, 1e-05, 1.37)], ["3", "3", "2.5", "0.01", "0.00001", "1.37"])


class ThumbnailShape(unittest.TestCase):
    def jpeg(self, width, height, extra=b""):
        import struct

        app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
        frame = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x00" * 9
        handle = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        handle.write(b"\xff\xd8" + app0 + extra + frame + b"\xff\xd9")
        handle.close()
        self.addCleanup(os.remove, handle.name)
        return handle.name

    def test_a_jpeg_says_its_size_and_only_16_by_9_counts_as_a_row_tile(self):
        import struct

        self.assertEqual(tools.jpeg_size(self.jpeg(256, 144)), (256, 144))
        comment = b"\xff\xfe" + struct.pack(">H", 2 + 5) + b"hello"  # a segment before the frame header is stepped over
        self.assertEqual(tools.jpeg_size(self.jpeg(200, 200, comment)), (200, 200))
        self.assertTrue(tools.is_wide_tile(self.jpeg(256, 144)))
        self.assertTrue(tools.is_wide_tile(self.jpeg(256, 150)), "close enough to stay in line")
        for other in ((200, 200), (144, 256), (256, 100)):
            self.assertFalse(tools.is_wide_tile(self.jpeg(*other)))
        self.assertIsNone(tools.jpeg_size(__file__))
        self.assertFalse(tools.is_wide_tile("/nonexistent.jpg"))


class TileCut(unittest.TestCase):
    """A picture of any shape, cut to 16:9 with nothing but the standard library."""

    def colour(self, x, y):
        return ((x * 7) % 256, (y * 11) % 256, (x * y) % 256)

    def test_every_kind_of_row_and_colour_layout_is_read_back_exactly(self):
        from .fake_kaitoi import fake_png

        makers = {
            1: lambda x, y: (self.colour(x, y)[0],),
            2: lambda x, y: (self.colour(x, y)[0], 255),
            3: self.colour,
            4: lambda x, y: (*self.colour(x, y), 255),
        }
        for channels, maker in makers.items():
            width, height, found, rows = tools._png_rows(fake_png(23, 17, channels, maker))
            self.assertEqual((width, height, found), (23, 17, channels))
            for y in range(17):  # rows 0 to 4 use the five kinds of row in turn, three times over
                self.assertEqual(rows[y], bytes(value for x in range(23) for value in maker(x, y)), f"{channels} channels, row {y}")

    def test_the_middle_is_kept_whether_the_picture_is_too_tall_or_too_wide(self):
        from .fake_kaitoi import fake_png

        tall = tools._png_rows(tools.crop_png_to_tile(fake_png(64, 64, 3, self.colour)))
        self.assertEqual(tall[:3], (64, 36, 3))
        for y in (0, 35):
            self.assertEqual(tall[3][y], bytes(value for x in range(64) for value in self.colour(x, y + 14)))
        wide = tools._png_rows(tools.crop_png_to_tile(fake_png(100, 18, 3, self.colour)))
        self.assertEqual(wide[:3], (32, 18, 3))
        self.assertEqual(wide[3][5], bytes(value for x in range(34, 66) for value in self.colour(x, 5)))
        same = tools._png_rows(tools.crop_png_to_tile(fake_png(32, 18, 3, self.colour)))
        self.assertEqual(same[:3], (32, 18, 3))

    def test_a_see_through_picture_is_laid_on_the_dark_tile_and_grey_becomes_colour(self):
        from .fake_kaitoi import fake_png

        clear = tools._png_rows(tools.crop_png_to_tile(fake_png(16, 9, 4, lambda x, y: (255, 0, 0, 0 if x < 8 else 255))))
        self.assertEqual(clear[3][0][:3], bytes(tools.TILE_BACKGROUND), "nothing of a fully clear pixel shows")
        self.assertEqual(clear[3][0][-3:], bytes((255, 0, 0)))
        half = tools._png_rows(tools.crop_png_to_tile(fake_png(16, 9, 2, lambda x, y: (255, 128))))
        self.assertEqual(half[3][0][:3], bytes((255 * 128 + c * 127) // 255 for c in tools.TILE_BACKGROUND))
        grey = tools._png_rows(tools.crop_png_to_tile(fake_png(16, 9, 1, lambda x, y: (90,))))
        self.assertEqual(grey[3][4][:3], bytes((90, 90, 90)))

    def test_anything_else_is_refused_and_never_raises(self):
        import struct

        from .fake_kaitoi import fake_png

        good = fake_png(16, 9, 3, self.colour)
        header = good.index(b"IHDR") + 4
        def with_header(**changes):
            fields = dict(zip(("width", "height", "depth", "colour", "pack", "filter", "interlace"), struct.unpack(">IIBBBBB", good[header : header + 13])))
            fields.update(changes)
            return good[:header] + struct.pack(">IIBBBBB", *fields.values()) + good[header + 13 :]

        self.assertIsNotNone(tools.crop_png_to_tile(good))
        for name, bad in {
            "not a PNG": b"\xff\xd8\xff" + b"J" * 200,
            "empty": b"",
            "only the signature": good[:8],
            "cut short": good[: len(good) // 2],
            "16 bits a channel": with_header(depth=16),
            "a palette": with_header(colour=3),
            "interlaced": with_header(interlace=1),
            "taller than it holds": with_header(height=10),
            "huge": with_header(width=50000),
            "cut inside its header": good[:25],
            "a header that says it is empty": good[: header - 8] + b"\x00\x00\x00\x00" + good[header - 4 :],
            "wider than twice what is asked for": fake_png(513, 4, 1, lambda x, y: (9,)),
            "taller than twice what is asked for": fake_png(4, 513, 1, lambda x, y: (9,)),
            "no width": with_header(width=0),
        }.items():
            self.assertIsNone(tools.crop_png_to_tile(bad), name)


    def test_a_small_file_made_to_unpack_into_a_huge_one_is_not_unpacked(self):
        import struct
        import tracemalloc
        import zlib

        def chunk(kind, body):
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

        packed = zlib.compress(bytes(64 * 1024 * 1024), 9)  # 64 MB of nothing in about 64 KB
        self.assertLess(len(packed), 100 * 1024)
        bomb = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 9, 8, 2, 0, 0, 0)) + chunk(b"IDAT", packed) + chunk(b"IEND", b"")
        tracemalloc.start()
        try:
            tile = tools.crop_png_to_tile(bomb)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 4 * 1024 * 1024, "only what a 16 by 9 picture holds is unpacked")
        self.assertEqual(tools._png_rows(tile)[:3], (16, 9, 3))

    def test_the_largest_picture_taken_is_read_in_well_under_a_second(self):
        import time as clock

        from .fake_kaitoi import fake_png

        self.assertIsNotNone(tools._png_rows(fake_png(512, 2, 1, lambda x, y: (x % 256,))), "twice the size asked for is still taken")
        started = clock.perf_counter()
        self.assertIsNotNone(tools.crop_png_to_tile(fake_png(256, 256, 4, lambda x, y: (x, y, 7, 255))))
        self.assertLess(clock.perf_counter() - started, 2.0, "the size that is asked for")


class ListsThatArriveLate(unittest.TestCase):
    """A node's own lists can arrive while its form is already open."""

    def node(self, **voice):
        return tools.node_from_detail({"type": "builtin/voice/speak", "title": "Speak", "category": "Audio", "inputs": [
            {"name": "voice", "type": "string", "dynamic": True, **voice},
            {"name": "greeting", "type": "string", "dynamic": True, "default": "Hello there"},
            {"name": "speed", "type": "string"},
        ], "outputs": [{"name": "audio", "type": "audio"}]})

    def test_a_box_with_nothing_to_show_until_now_is_filled_and_an_emptied_one_is_left(self):
        before = self.node()
        after = tools.apply_initial_state(before, {"inputs": {"voice": {"options": None, "value": "Aria"}, "greeting": {"options": None, "value": "Good morning"}}})
        self.assertEqual(next(field for field in after.inputs if field.name == "voice").default, "Aria")
        typed = {"voice": "", "greeting": "", "speed": "", tools_picked(): {}}
        kept = tools.empty_boxes_to_refill(typed, before, after)
        self.assertNotIn("voice", kept, "it was empty because there was nothing to put in it")
        self.assertEqual(kept["greeting"], "", "it showed a sample and the user emptied it")
        self.assertEqual(kept["speed"], "", "an ordinary box is never touched")
        self.assertIn(tools_picked(), kept)
        self.assertEqual(tools.empty_boxes_to_refill({"voice": "Rachel"}, before, after), {"voice": "Rachel"}, "what was typed while waiting is kept")

    def test_a_box_whose_sample_never_came_is_not_remembered_as_emptied(self):
        waiting = self.node()
        self.assertEqual(tools.empty_boxes_to_refill({"voice": "", "greeting": "", "speed": ""}, waiting, waiting), {"greeting": "", "speed": ""})
        self.assertEqual(tools.empty_boxes_to_refill({"voice": ""}, None, waiting), {})


def tools_picked() -> str:
    from kaitoi_resolve import ui

    return ui.PICKED


class TypedIntoAPicker(unittest.TestCase):
    def field(self, options):
        return tools.node_from_detail({"type": "builtin/x", "title": "X", "inputs": [{"name": "voice", "type": "string", "options": options}], "outputs": []}).inputs[0]

    def test_what_was_typed_is_read_as_written_before_any_looser_way(self):
        # Each option's value is another option's name, in another case.
        crossed = self.field([{"value": "aria", "label": "Blade"}, {"value": "Blade", "label": "Aria"}])
        self.assertEqual(tools.option_position(crossed, "aria"), 1, "a value as written")
        self.assertEqual(tools.option_position(crossed, "Blade"), 2, "a value as written, before the same word as a name")
        self.assertEqual(tools.option_position(crossed, "Aria"), 2, "a name as written")
        self.assertEqual(tools.option_position(crossed, "  ARIA "), 1, "then without regard to case: values first")
        plain = self.field([{"value": "v1", "label": "Aria"}, {"value": "v2", "label": "Blade"}])
        self.assertEqual([tools.option_position(plain, typed) for typed in ("v2", "V2", "blade", "Rachel", "")], [2, 2, 2, 0, 0])
        numbers = self.field([1, 2.5, 10])
        self.assertEqual([tools.option_position(numbers, typed) for typed in ("1", "1.0", "2,5", "10.00", "3")], [1, 1, 2, 3, 0])


class PluginFolder(unittest.TestCase):
    def test_every_part_of_the_plugin_closes_the_folder_whichever_runs_first(self):
        before = os.umask(0)  # as inside Resolve
        self.addCleanup(os.umask, before)
        for name, first in {
            "the busy marker": busy._path,
            "the history": history.path,
            "the workflow list": tools.snapshot_path,
            "the node list": tools.nodes_path,
            "what is installed": templates._manifest_path,
            "the pictures": tools.thumbnail_dir,
        }.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as home:
                folder = os.path.join(home, "app")
                with mock.patch.object(config, "APP_DIR", folder):
                    first()
                self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700, name)

    def test_a_folder_resolve_left_open_to_everyone_is_closed_to_them(self):
        with tempfile.TemporaryDirectory() as home:
            folder = os.path.join(home, "app")
            os.mkdir(folder)
            os.chmod(folder, 0o777)  # as Resolve creates it
            with mock.patch.object(config, "APP_DIR", folder):
                self.assertEqual(os.path.dirname(config.path()), folder)
            self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)


class Columns(unittest.TestCase):
    def test_columns_that_fit_are_left_alone_and_wider_ones_are_cut_to_the_room(self):
        from kaitoi_resolve import ui

        self.assertEqual(ui.fit_columns(204, 116, 481), (204, 116))
        self.assertEqual(ui.fit_columns(100, 60, 300), (100, 60))
        self.assertEqual(ui.fit_columns(204, 116, 440), (199, 99))
        for first, second, width in ((204, 116, 440), (330, 220, 481), (900, 40, 481), (40, 900, 600)):
            fitted = ui.fit_columns(first, second, width)
            self.assertLessEqual(sum(fitted), width - 142, (first, second, width))
            self.assertLessEqual(fitted[0], first, "never wider than asked")
            self.assertLessEqual(fitted[1], second, "never wider than asked")
        self.assertEqual(ui.fit_columns(900, 40, 481), (299, 40), "the wide one gives way")
        self.assertEqual(ui.fit_columns(300, 300, 200), (120, 80), "a window too narrow for anything keeps both readable")


class NodeIcons(unittest.TestCase):
    def node(self, **over):
        return tools.node_from_summary({"type": "builtin/x", "title": "X", "category": "Utilities", **over})

    def test_a_node_always_gets_an_icon_its_own_when_the_plugin_has_it(self):
        from kaitoi_resolve import ui

        self.assertTrue(ui._placeholder(self.node(icon="film")).endswith(os.path.join("icons", "film.png")))
        for icon, category, expected in (
            ("streamline-ultimate-color:color-palette-2", "Image/Color", "image"),
            ("lobe:elevenlabs", "Audio/Text to Speech", "audio-lines"),
            ("", "Models/Image to Video", "film"),
            ("no-such-icon", "Utilities", "file-text"),
            ("../../etc/passwd", "Utilities", "file-text"),
        ):
            shown = ui._placeholder(self.node(icon=icon, category=category))
            self.assertEqual(shown, os.path.join(ui.ASSETS, "icons", f"{expected}.png"), icon)
            self.assertTrue(os.path.isfile(shown), shown)

    def test_a_workflow_without_a_thumbnail_keeps_its_coloured_tile(self):
        from kaitoi_resolve import ui

        workflow = tools.Tool(id="t1:e1", name="Rain", description="", category="", tags=[], inputs=[], outputs=[], source="workflow")
        self.assertIn("placeholder_", os.path.basename(ui._placeholder(workflow)))


class InspectorStatus(unittest.TestCase):
    def test_the_status_line_names_the_step_and_what_it_is_doing(self):
        from kaitoi_resolve import inspector, pipeline

        total = len(pipeline.STAGES)
        self.assertEqual(inspector.progress_line({"stage": 3, "status": "Run: Rain: in the queue 40%"}), f"4/{total}  Run: Rain: in the queue 40%")
        self.assertEqual(inspector.progress_line({"stage": -1, "status": "Starting..."}), f"1/{total}  Starting...")
        self.assertEqual(inspector.progress_line({"stage": None, "status": None}), f"1/{total}")


class Installer(unittest.TestCase):
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def launcher(self):
        with open(os.path.join(self.REPO, "Kaitoi.py"), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_the_launcher_looks_where_each_installer_puts_the_package(self):
        source = self.launcher()
        for home in ("/Library/Application Support/Kaitoi/Resolve", '"PROGRAMFILES"', "/opt/kaitoi/resolve"):
            self.assertIn(home, source)
        with open(os.path.join(self.REPO, "packaging", "macos", "build-pkg.sh"), "r", encoding="utf-8") as fh:
            self.assertIn('Library/Application Support/Kaitoi/Resolve"', fh.read())
        with open(os.path.join(self.REPO, "packaging", "windows", "kaitoi-resolve.iss"), "r", encoding="utf-8") as fh:
            self.assertIn("DefaultDirName={autopf}\\Kaitoi\\Resolve", fh.read(), "Program Files: ProgramData is writable by every user of the PC")

    def test_the_launcher_prefers_the_installed_package_and_never_takes_code_from_a_shared_windows_folder(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("kaitoi_launcher_under_test", os.path.join(self.REPO, "Kaitoi.py"))
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)  # finds the package beside itself; starts nothing outside Resolve
        with tempfile.TemporaryDirectory() as root:
            installed, manual, shared = (os.path.join(root, name) for name in ("installed", "manual", "ProgramData", ))
            shared_scripts = os.path.join(shared, "Blackmagic Design", "DaVinci Resolve", "Fusion", "Scripts", "Edit")
            for directory in (installed, manual, shared_scripts):
                os.makedirs(os.path.join(directory, "kaitoi_resolve"))
            with mock.patch.object(launcher, "_package_homes", lambda: [installed]), mock.patch.object(launcher, "_script_dirs", lambda: [manual]):
                self.assertEqual(launcher._find_package_dir(), installed, "an old manual copy does not win over the installed one")
            with mock.patch.object(launcher, "_package_homes", lambda: [os.path.join(root, "absent")]), mock.patch.object(launcher, "_script_dirs", lambda: [manual]):
                self.assertEqual(launcher._find_package_dir(), manual, "with nothing installed, the manual copy is used")
            with mock.patch.object(launcher, "_package_homes", lambda: [os.path.join(root, "absent")]), \
                    mock.patch.object(launcher, "_script_dirs", lambda: [shared_scripts, manual]), \
                    mock.patch.object(launcher.sys, "platform", "win32"), mock.patch.dict(os.environ, {"PROGRAMDATA": shared}):
                self.assertEqual(launcher._find_package_dir(), manual, "on Windows, code is never taken from the folder every user can write to")

    def test_on_a_mac_nothing_is_run_or_loaded_from_the_folder_every_user_shares(self):
        """Resolve's folder for every user of a Mac can be written by every account: the installer puts nothing there, and nothing is looked for there."""
        import importlib.util

        shared = "/Library/Application Support/Blackmagic Design"
        spec = importlib.util.spec_from_file_location("kaitoi_launcher_under_test", os.path.join(self.REPO, "Kaitoi.py"))
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with mock.patch.object(launcher.sys, "platform", "darwin"), mock.patch.object(launcher.sys, "argv", [""]):
            looked = launcher._script_dirs()
            self.assertTrue(any(directory.endswith("Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit") for directory in looked), "the user's own is still looked in")
            self.assertEqual([directory for directory in looked if directory.startswith(shared)], [])
            for directory, is_shared in ((shared, True), (shared + "/DaVinci Resolve/Fusion/Scripts/Edit", True), (shared + " Copy/Scripts", False),
                                         (os.path.expanduser("~") + shared, False), ("/Library/Application Support/Kaitoi/Resolve", False)):
                self.assertEqual(launcher._shared(directory), is_shared, directory)
        with mock.patch.object(launcher.sys, "platform", "linux"):
            self.assertFalse(launcher._shared("/opt/resolve/Fusion/Scripts/Edit"))
        # The same folder reached under another name is still the shared folder.
        with tempfile.TemporaryDirectory() as root:
            real, alias, other = os.path.join(root, "Shared Design"), os.path.join(root, "alias"), os.path.join(root, "other")
            os.makedirs(os.path.join(real, "Scripts", "Edit"))
            os.makedirs(os.path.join(other, "Scripts", "Edit"))
            os.symlink(real, alias)
            with mock.patch.object(launcher, "_shared_root", lambda: real):
                self.assertTrue(launcher._shared(os.path.join(alias, "Scripts", "Edit")), "by a second name for the folder")
                self.assertTrue(launcher._shared(os.path.join(alias, "Scripts", "not made yet")))
                below = os.path.join(root, "a link to a folder inside it")
                os.symlink(os.path.join(real, "Scripts"), below)
                self.assertTrue(launcher._shared(below), "through a link that leads inside it")
                self.assertTrue(launcher._shared(os.path.join(below, "Edit")))
                if os.path.exists(real.upper()):  # a disk that takes either letter case, as a Mac's does
                    self.assertTrue(launcher._shared(os.path.join(os.path.dirname(real), "SHARED DESIGN", "Scripts")))
                self.assertFalse(launcher._shared(os.path.join(other, "Scripts", "Edit")))
                self.assertFalse(launcher._shared(root), "the folder it is in is not it")
            absent = os.path.join(root, "not there")
            with mock.patch.object(launcher, "_shared_root", lambda: absent):
                self.assertTrue(launcher._shared(os.path.join(absent, "Scripts", "Edit")), "known by its name alone, before Resolve has made it")
                self.assertFalse(launcher._shared(os.path.join(root, "not there either")))
        # Handed the shared folder all the same (Resolve started the script from a copy someone put there), no package is taken from it,
        # neither one lying in it nor one that a launcher lying in it leads to.
        with tempfile.TemporaryDirectory() as root:
            mine, elsewhere = os.path.join(root, "mine"), os.path.join(root, "elsewhere")
            shared_scripts = os.path.join(root, "shared", "Scripts", "Edit")
            for directory in (mine, elsewhere, shared_scripts):
                os.makedirs(os.path.join(directory, "kaitoi_resolve"))
            for directory in (mine, shared_scripts):
                with open(os.path.join(directory, "Kaitoi.py"), "w", encoding="utf-8"):
                    pass
            linked = os.path.join(root, "linked")
            os.makedirs(linked)
            os.symlink(os.path.join(shared_scripts, "Kaitoi.py"), os.path.join(linked, "Kaitoi.py"))
            in_shared = lambda directory: os.path.realpath(directory).startswith(os.path.realpath(os.path.join(root, "shared")))  # noqa: E731
            with mock.patch.object(launcher, "_package_homes", lambda: [os.path.join(root, "absent")]), mock.patch.object(launcher, "_shared", in_shared):
                with mock.patch.object(launcher, "_script_dirs", lambda: [shared_scripts, linked, mine]):
                    self.assertEqual(launcher._find_package_dir(), mine)
                with mock.patch.object(launcher, "_script_dirs", lambda: [shared_scripts, linked]), self.assertRaises(ImportError):
                    launcher._find_package_dir()
        # The buttons on the effects start the script the same way: from the user's own folder.
        own = 'home .. "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/'
        made = templates._button_code("generate")
        self.assertIn(own.replace('"', '\\"'), made)
        texts = [made]
        for kind in ("Effects", "Generators"):
            with open(os.path.join(self.REPO, "templates", "Edit", kind, "Kaitoi.setting"), "r", encoding="utf-8") as fh:
                texts.append(fh.read())
            self.assertEqual(texts[-1].count(own.replace('"', '\\"')), 2, "both buttons of the template Kaitoi installs")
        for text in texts:
            self.assertEqual(re.findall(r'(?<!home \.\. )\\"/Library/Application Support/Blackmagic Design[^"]*', text), [], "a path in the shared folder, tried when the user's own copy is missing")

    @unittest.skipUnless(__import__("sys").platform == "darwin" and __import__("shutil").which("pkgbuild"), "needs macOS packaging tools")
    def test_the_macos_package_carries_only_its_own_folder(self):
        import subprocess

        with tempfile.TemporaryDirectory() as out:
            env = {key: value for key, value in os.environ.items() if not key.startswith(("APPLE_", "MACOS_"))}
            built = subprocess.run([os.path.join(self.REPO, "packaging", "macos", "build-pkg.sh")], env={**env, "OUT_DIR": out}, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            package = next(os.path.join(out, name) for name in os.listdir(out) if name.endswith(".pkg"))
            listed = subprocess.run(["pkgutil", "--payload-files", package], capture_output=True, text=True).stdout.splitlines()
            files = {line[1:] for line in listed if "/._" not in line}  # "._" entries only carry file attributes
            for wanted in ("/install/Kaitoi.py", "/install/Generators/Kaitoi.setting", "/install/Effects/Kaitoi.setting", "/install/README.txt",
                           "/kaitoi_resolve/ui.py", "/kaitoi_resolve/uninstall.py", "/kaitoi_resolve/assets/icons/film.png", "/LICENSE", "/Uninstall Kaitoi.command"):
                self.assertIn(wanted, files)
            self.assertFalse([name for name in files if "__pycache__" in name or name.endswith((".pyc", ".DS_Store")) or "/tests/" in name])
            # An installer sets the owner and permissions of every folder a
            # package carries. Resolve's folders are kept open to its user;
            # carried here, they would be closed to them.
            self.assertFalse([name for name in files if "Blackmagic" in name or name.startswith("/Library")], "none of Resolve's folders, and nothing above its own")
            self.assertEqual({name.split("/")[1] for name in files if name}, {"install", "kaitoi_resolve", "LICENSE", "Uninstall Kaitoi.command"})
            expanded = os.path.join(out, "expanded")
            subprocess.run(["pkgutil", "--expand", package, expanded], check=True, capture_output=True)
            info = next(os.path.join(base, "PackageInfo") for base, _, names in os.walk(expanded) if "PackageInfo" in names)
            with open(info, "r", encoding="utf-8") as fh:
                self.assertIn('install-location="/Library/Application Support/Kaitoi/Resolve"', fh.read())
            carried = next(os.path.join(base, "postinstall") for base, _, names in os.walk(expanded) if "postinstall" in names)
            with open(carried, "rb") as packed, open(os.path.join(self.REPO, "packaging", "macos", "postinstall"), "rb") as source:
                self.assertEqual(packed.read(), source.read(), "the step that is tested below is the one that ships")
            # The uninstaller: the file tested below, where the window looks for it, and able to be run.
            whole = os.path.join(out, "whole")
            subprocess.run(["pkgutil", "--expand-full", package, whole], check=True, capture_output=True)
            shipped = next(os.path.join(base, "Uninstall Kaitoi.command") for base, _, names in os.walk(whole) if "Uninstall Kaitoi.command" in names)
            with open(shipped, "rb") as packed, open(os.path.join(self.REPO, "packaging", "macos", "uninstall"), "rb") as source:
                self.assertEqual(packed.read(), source.read())
            self.assertEqual(os.stat(shipped).st_mode & 0o777, 0o755, "run by a double-click, and changed by nobody but an administrator")
            self.assertTrue(os.path.isdir(os.path.join(os.path.dirname(shipped), "kaitoi_resolve")), "beside the code: that is where the window looks for it")
            with open(os.path.join(os.path.dirname(shipped), "install", "README.txt"), encoding="utf-8") as fh:
                self.assertIn('"Uninstall Kaitoi.command"', fh.read())
            with open(os.path.join(expanded, "Distribution"), "r", encoding="utf-8") as fh:
                self.assertIn('<domains enable_localSystem="true" enable_anywhere="false" enable_currentUserHome="false"/>', fh.read(), "this Mac's own disk only: the last step and Resolve look nowhere else")
            self.assertEqual(subprocess.run(["/usr/sbin/installer", "-dominfo", "-pkg", package], capture_output=True, text=True).stdout.split(), ["LocalSystem"])
            with open(package + ".sha256", "r", encoding="utf-8") as fh:
                self.assertIn(os.path.basename(package), fh.read())
            check = os.path.join(self.REPO, "packaging", "macos", "check-payload.sh")
            for _ in range(5):  # the listing is long enough for a careless check to cut it off, and fail or pass by chance
                self.assertEqual(subprocess.run([check, package], capture_output=True).returncode, 0)

    @unittest.skipUnless(__import__("sys").platform == "darwin" and __import__("shutil").which("pkgbuild"), "needs macOS packaging tools")
    def test_a_package_that_carries_someone_elses_folder_is_refused_every_time(self):
        import subprocess

        check = os.path.join(self.REPO, "packaging", "macos", "check-payload.sh")
        with tempfile.TemporaryDirectory() as work:
            home = "/Library/Application Support/Kaitoi/Resolve"

            def package(name, extra, installs_into=home):
                root = os.path.join(work, name)
                for path in ["install/Kaitoi.py", "install/Generators/Kaitoi.setting", "install/Effects/Kaitoi.setting", "kaitoi_resolve/ui.py"] + [f"kaitoi_resolve/assets/icons/icon-{at:03}.png" for at in range(40)] + extra:
                    os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
                    with open(os.path.join(root, path), "w", encoding="utf-8") as fh:
                        fh.write("x")
                built = os.path.join(work, name + ".pkg")
                subprocess.run(["pkgbuild", "--root", root, "--install-location", installs_into, "--identifier", "io.kaitoi.test", "--version", "1", built], check=True, capture_output=True)
                return built

            good = package("good", [])  # real packages, for the real lister's format; PackageCheck covers the order
            last = package("last", ["zz/Blackmagic Design/Fusion/Kaitoi.py"])
            library = package("library", ["Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/Kaitoi.py"])
            misplaced = package("misplaced", [], installs_into="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion")  # only our files, in their folder
            at_root = package("at_root", [], installs_into="/")
            for _ in range(5):
                self.assertEqual(subprocess.run([check, good], capture_output=True).returncode, 0)
                for bad in (last, library, misplaced, at_root):
                    refused = subprocess.run([check, bad], capture_output=True, text=True)
                    self.assertEqual(refused.returncode, 1, bad)
                    self.assertIn("not Kaitoi's own", refused.stdout)
            os.remove(os.path.join(work, "good", "install", "Kaitoi.py"))
            subprocess.run(["pkgbuild", "--root", os.path.join(work, "good"), "--install-location", home, "--identifier", "io.kaitoi.test", "--version", "1", good], check=True, capture_output=True)
            self.assertEqual(subprocess.run([check, good], capture_output=True).returncode, 1, "and one that lacks the script")


@unittest.skipUnless(__import__("sys").platform == "darwin", "the macOS uninstaller")
class TheMacUninstaller(unittest.TestCase):
    """packaging/macos/uninstall, run on a home folder and a shared folder made here.

    Every run goes through :meth:`uninstall`, which always names both. Left to
    itself the script would act on the real home folder and ask for the real
    password.
    """

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    SCRIPT = os.path.join(REPO, "packaging", "macos", "uninstall")
    OWN = "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion"
    OURS = ["Scripts/Edit/Kaitoi.py", "Templates/Edit/Generators/Kaitoi.setting", "Templates/Edit/Effects/Kaitoi.setting",
            "Templates/Edit/Generators/Kaitoi/Rain.setting", "Templates/Edit/Generators/Kaitoi/Rain.png",
            "Templates/Edit/Effects/Kaitoi/Rain.setting", "Templates/Edit/Effects/Kaitoi/Rain.png"]
    THEIRS = ["Scripts/Edit/My Own.py", "Templates/Edit/Generators/Mine.setting", "Templates/Edit/Effects/Other Folder/Theirs.setting"]
    KEPT = ["Movies/Kaitoi/result.mp4", ".kaitoi/credentials.json"]
    SETTINGS = [".kaitoi_resolve/config.json", ".kaitoi_resolve/history.json", ".kaitoi_resolve/plugin.log"]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = os.path.join(os.path.realpath(self.dir.name), "a mac")  # a space, as real paths here have
        self.home = os.path.join(self.root, "home")
        self.shared = os.path.join(self.root, "shared", "Kaitoi", "Resolve")
        self.own = os.path.join(self.home, self.OWN)
        for name in self.OURS:
            self.put(f"{self.OWN}/{name}", "an effect of Kaitoi's { KaitoiSpec = ... }" if "/Kaitoi/" in name and name.endswith(".setting") else "kaitoi")
        for name in self.THEIRS:
            self.put(f"{self.OWN}/{name}", "theirs")
        for name in self.KEPT + self.SETTINGS + [".kaitoi_resolve/templates.json"]:
            self.put(name)
        for name in ("kaitoi_resolve/ui.py", "install/Kaitoi.py", "Uninstall Kaitoi.command"):
            path = os.path.join(self.shared, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("code")

    def put(self, name, text="x"):
        path = os.path.join(self.home, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def uninstall(self, *arguments, typed=None, **changed):
        import subprocess

        self.assertTrue(self.home.startswith(self.root) and self.shared.startswith(self.root) and tempfile.gettempdir() not in ("", "/"))
        env = {**os.environ, "HOME": self.home, "KAITOI_TEST_SHARED": self.shared, **changed}
        self.assertTrue(env["HOME"].startswith(self.root + os.sep) and env["KAITOI_TEST_SHARED"].startswith(self.root + os.sep), "never the real home folder, never the real password: both are inside this test's own folder")
        done = subprocess.run(["/bin/bash", self.SCRIPT, *arguments], env=env, input=typed if typed is not None else "", capture_output=True, text=True, cwd=self.root)
        self.assertEqual(done.stderr, "")
        return done.returncode, done.stdout

    def everything(self, folder=None):
        """Every file, folder and link below a folder, with what each file holds."""
        folder = folder or self.root
        found = {}
        for base, folders, names in os.walk(folder):
            for name in folders + names:
                path = os.path.join(base, name)
                if os.path.islink(path):
                    found[os.path.relpath(path, folder)] = "-> " + os.readlink(path)
                elif os.path.isdir(path):
                    found[os.path.relpath(path, folder)] = None
                else:
                    with open(path, encoding="utf-8") as fh:
                        found[os.path.relpath(path, folder)] = fh.read()
        return found

    def files(self, folder):
        return sorted(os.path.relpath(os.path.join(base, name), folder) for base, _, names in os.walk(folder) for name in names)

    def test_all_of_kaitoi_goes_and_nothing_of_anyone_elses(self):
        code, said = self.uninstall("--yes")
        self.assertEqual((code, said), (0, "Kaitoi was removed. Restart DaVinci Resolve to clear it from the menus.\n"))
        self.assertEqual(self.files(self.own), sorted(self.THEIRS), "the user's own script and effects, and nothing of Kaitoi's")
        self.assertFalse(os.path.exists(os.path.join(self.own, "Templates/Edit/Generators/Kaitoi")), "its two folders of effects go when they are empty")
        self.assertFalse(os.path.exists(os.path.join(self.own, "Templates/Edit/Effects/Kaitoi")))
        self.assertFalse(os.path.exists(self.shared), "the code every account shares")
        self.assertEqual([name for name in self.files(self.home) if not name.startswith("Library")], sorted(self.KEPT + self.SETTINGS), "results, the sign-in and the settings stay; the list of installed effects does not")
        self.assertEqual(self.uninstall("--yes")[0], 0, "a second time there is nothing to do, and that is not a failure")

    def test_settings_and_history_go_only_when_asked(self):
        self.assertEqual(self.uninstall("--yes", "--settings")[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".kaitoi_resolve")))
        self.assertEqual([name for name in self.files(self.home) if not name.startswith("Library")], sorted(self.KEPT), "results and the sign-in are never touched")

    def test_a_password_that_is_not_given_leaves_everything_as_it_was(self):
        before = self.everything()
        code, said = self.uninstall("--yes", "--settings", KAITOI_TEST_REFUSE="1")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was removed", said)
        self.assertEqual(self.everything(), before, "the step that needs the password comes first")

    def test_asked_in_terminal_nothing_goes_without_a_yes(self):
        before = self.everything()
        for typed in ("", "\n", "n\n", "no\n", "maybe\n", "yes please\n"):
            code, said = self.uninstall(typed=typed)
            self.assertEqual(code, 1, repr(typed))
            self.assertTrue(said.endswith("Nothing was removed.\n"))
            self.assertEqual(self.everything(), before, repr(typed))
        code, said = self.uninstall(typed="y\n\n")
        self.assertEqual(code, 0, said)
        self.assertIn("every account of this Mac", said)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".kaitoi_resolve/config.json")), "settings are kept unless that is answered with a yes too")
        self.assertEqual(self.files(self.own), sorted(self.THEIRS))

    def test_asked_in_terminal_a_second_yes_removes_the_settings(self):
        self.assertEqual(self.uninstall(typed="yes\ny\n")[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".kaitoi_resolve")))

    def test_someone_elses_effect_in_a_kaitoi_folder_stays_and_is_named(self):
        theirs = self.put(f"{self.OWN}/Templates/Edit/Effects/Kaitoi/Saved By Hand.setting", "{ Tools = {} }")
        picture = self.put(f"{self.OWN}/Templates/Edit/Effects/Kaitoi/Saved By Hand.png")
        code, said = self.uninstall("--yes")
        self.assertEqual(code, 3)
        self.assertEqual(said.splitlines()[1:], ["Left in place: " + os.path.dirname(theirs)])
        self.assertEqual(sorted(os.listdir(os.path.dirname(theirs))), ["Saved By Hand.png", "Saved By Hand.setting"])
        self.assertTrue(os.path.exists(picture))
        self.assertFalse(os.path.exists(os.path.join(self.own, "Templates/Edit/Generators/Kaitoi")), "the other folder held only Kaitoi's")
        self.assertFalse(os.path.exists(self.shared))

    def test_a_folder_where_one_of_kaitois_files_would_be_is_left_alone(self):
        os.remove(os.path.join(self.own, "Templates/Edit/Effects/Kaitoi.setting"))
        inside = self.put(f"{self.OWN}/Templates/Edit/Effects/Kaitoi.setting/theirs.txt", "theirs")
        code, said = self.uninstall("--yes")
        self.assertEqual(code, 3)
        self.assertIn("Left in place: " + os.path.dirname(inside), said)
        self.assertTrue(os.path.exists(inside))

    def test_links_are_removed_and_never_followed(self):
        """An install from the plugin's folder leaves a link to the script; and a link is never emptied as if it were Kaitoi's folder."""
        checkout = os.path.join(self.root, "checkout")
        elsewhere = os.path.join(self.root, "elsewhere")
        for folder in (checkout, elsewhere):
            os.makedirs(folder)
        for name in ("Kaitoi.py",):
            with open(os.path.join(checkout, name), "w", encoding="utf-8") as fh:
                fh.write("the script, in the plugin's own folder")
        with open(os.path.join(elsewhere, "Rain.setting"), "w", encoding="utf-8") as fh:
            fh.write("KaitoiSpec, somewhere that is not Kaitoi's folder")
        script = os.path.join(self.own, "Scripts/Edit/Kaitoi.py")
        os.remove(script)
        os.symlink(os.path.join(checkout, "Kaitoi.py"), script)
        copied = self.put(f"{self.OWN}/Scripts/Edit/kaitoi_resolve/ui.py")
        effects = os.path.join(self.own, "Templates/Edit/Generators/Kaitoi")
        for name in os.listdir(effects):
            os.remove(os.path.join(effects, name))
        os.rmdir(effects)
        os.symlink(elsewhere, effects)
        settings = os.path.join(self.home, ".kaitoi_resolve")
        kept_elsewhere = os.path.join(self.root, "settings elsewhere")
        os.rename(settings, kept_elsewhere)
        os.symlink(kept_elsewhere, settings)
        code, said = self.uninstall("--yes", "--settings")
        self.assertEqual(code, 3)
        self.assertEqual(said.splitlines()[1:], ["Left in place: " + effects], "a link where the folder of effects would be is said, and left")
        self.assertFalse(os.path.lexists(script))
        self.assertFalse(os.path.exists(os.path.dirname(copied)), "the copy of the code an older install left beside the script")
        self.assertEqual(os.listdir(checkout), ["Kaitoi.py"], "what the link led to is not Kaitoi's to delete")
        self.assertEqual(os.listdir(elsewhere), ["Rain.setting"])
        self.assertFalse(os.path.lexists(settings))
        self.assertIn("config.json", os.listdir(kept_elsewhere), "the link to the settings goes, not the folder it led to")

    def test_with_no_code_installed_for_every_account_the_users_own_files_still_go(self):
        """An install from the plugin's folder: there is nothing that needs a password."""
        import shutil

        shutil.rmtree(self.shared)
        self.assertEqual(self.uninstall("--yes", KAITOI_TEST_REFUSE="1")[0], 0, "the password is not asked for, so it cannot be refused")
        self.assertEqual(self.files(self.own), sorted(self.THEIRS))

    def test_anything_else_on_the_command_line_stops_it_before_it_starts(self):
        before = self.everything()
        for arguments in (["--all"], ["--yes", "extra"], ["-y"], ["--yes", "--settings=1"]):
            code, said = self.uninstall(*arguments)
            self.assertEqual(code, 2, arguments)
            self.assertIn("usage:", said)
        self.assertEqual(self.everything(), before)

    def test_a_home_folder_that_cannot_be_found_stops_it(self):
        """Run only with a home folder that is missing from this test's own folder: were the check to fail, nothing real is below it.
        With no home at all the paths would start at the top of the disk, so that case is read as text and never run."""
        before = self.everything()
        code, said = self.uninstall("--yes", HOME=os.path.join(self.root, "nobody"))
        self.assertEqual(code, 1)
        self.assertIn("nothing was removed", said)
        self.assertEqual(self.everything(), before)
        script = self.script()
        guard = '''    local home="${HOME:-}"
    if [ -z "$home" ] || [ ! -d "$home/Library" ]; then
        echo "Your home folder could not be found, so nothing was removed."
        return 1
    fi
'''
        self.assertEqual(script.count(guard), 1)
        self.assertEqual(len(re.findall(r"(?<![\w$])home=", script)), 1, "set once, and checked before anything is built from it")
        self.assertLess(script.index(guard), script.index('local fusion="$home/Library/$below"'))

    def test_it_refuses_to_run_as_root(self):
        """Read as text: as root, the home folder and the files would be root's. It comes before anything else is looked at."""
        script = self.script()
        guard = '''    if [ "$(/usr/bin/id -u)" = "0" ]; then
        echo "Run this as yourself, without sudo. It removes Kaitoi from your own DaVinci Resolve folder, and macOS asks for a password when one is needed."
        return 1
    fi
'''
        self.assertEqual(script.count(guard), 1)
        self.assertLess(script.index(guard), script.index('local home="${HOME:-}"'))
        self.assertNotRegex(script, r"\bEUID\b|\bUID\b", "who it runs as is asked of the system, not read from a variable anyone can set")

    def test_nothing_is_deleted_through_a_link_on_the_way_down(self):
        """A Scripts or Templates folder that is a link leads somewhere other people or other computers may share."""
        import shutil

        shared_scripts = os.path.join(self.root, "synced", "Scripts")
        shared_templates = os.path.join(self.root, "synced", "Templates")
        shutil.move(os.path.join(self.own, "Scripts"), shared_scripts)
        shutil.move(os.path.join(self.own, "Templates"), shared_templates)
        os.symlink(shared_scripts, os.path.join(self.own, "Scripts"))
        os.symlink(shared_templates, os.path.join(self.own, "Templates"))
        for name in ("Edit/kaitoi_resolve/ui.py", "Edit/Kaitoi Video.py"):
            path = os.path.join(shared_scripts, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("kaitoi")
        before = self.everything(os.path.join(self.root, "synced"))
        code, said = self.uninstall("--yes")
        self.assertEqual(code, 3)
        self.assertEqual(self.everything(os.path.join(self.root, "synced")), before, "nothing behind either link was touched")
        named = [line[len("Left in place: "):] for line in said.splitlines()[1:]]
        self.assertEqual(sorted(named), sorted(os.path.join(self.own, name) for name in (
            "Scripts/Edit/Kaitoi.py", "Scripts/Edit/Kaitoi Video.py", "Scripts/Edit/kaitoi_resolve",
            "Templates/Edit/Generators/Kaitoi.setting", "Templates/Edit/Generators/Kaitoi", "Templates/Edit/Effects/Kaitoi.setting", "Templates/Edit/Effects/Kaitoi")),
            "and each thing of Kaitoi's there is named, so it can be removed by hand")
        self.assertFalse(os.path.exists(self.shared), "Kaitoi's own code is still removed")

    def test_a_link_on_the_way_with_nothing_of_kaitois_behind_it_is_not_mentioned(self):
        import shutil

        elsewhere = os.path.join(self.root, "synced", "Templates")
        shutil.rmtree(os.path.join(self.own, "Templates"))
        os.makedirs(os.path.join(elsewhere, "Edit", "Effects"))
        with open(os.path.join(elsewhere, "Edit", "Effects", "Mine.setting"), "w", encoding="utf-8") as fh:
            fh.write("theirs")
        os.symlink(elsewhere, os.path.join(self.own, "Templates"))
        code, said = self.uninstall("--yes")
        self.assertEqual((code, said.count("\n")), (0, 1), said)
        self.assertEqual(self.files(elsewhere), ["Edit/Effects/Mine.setting"])

    def test_the_library_itself_as_a_link_is_not_gone_through_either(self):
        real = os.path.join(self.root, "the real library")
        os.rename(os.path.join(self.home, "Library"), real)
        os.symlink(real, os.path.join(self.home, "Library"))
        before = self.everything(real)
        code, said = self.uninstall("--yes")
        self.assertEqual(code, 3)
        self.assertEqual(self.everything(real), before)
        self.assertEqual(said.count("Left in place: "), len(self.OURS) - 2, "the script, the two effects and the two folders of effects")

    def test_a_stand_in_for_the_shared_folder_must_be_named_as_the_real_one_is(self):
        """So that the name of some other folder, set by mistake, deletes nothing."""
        before = self.everything()
        for other in (os.path.join(self.root, "home"), os.path.join(self.root, "shared"), os.path.join(self.root, "shared", "Kaitoi"), self.shared + "/", self.shared + " 2"):
            code, said = self.uninstall("--yes", "--settings", KAITOI_TEST_SHARED=other)
            self.assertEqual(code, 2, other)
            self.assertIn("Nothing was removed.", said)
        self.assertEqual(self.everything(), before)

    def script(self):
        with open(self.SCRIPT, encoding="utf-8") as fh:
            return fh.read()

    def test_the_step_run_as_an_administrator_is_written_out_in_full(self):
        """Read as text: the real step cannot be run in a test. It deletes Kaitoi's own folder and nothing else, and takes nothing from outside."""
        script = self.script()
        steps = re.findall(r"^.*osascript.*$", script, re.M)
        self.assertEqual(len(steps), 1)
        step = steps[0].strip()
        self.assertEqual(step, r"""elif ! /usr/bin/osascript -e 'do shell script "/bin/rm -rf \"/Library/Application Support/Kaitoi/Resolve\" && { /bin/rmdir \"/Library/Application Support/Kaitoi\"; /usr/sbin/pkgutil --forget io.kaitoi.resolve-plugin; /usr/bin/true; } >/dev/null 2>&1" with prompt "Kaitoi needs your password to remove its files from this Mac." with administrator privileges' >/dev/null 2>&1; then""")
        self.assertNotRegex(step, r"\$|`", "nothing in it comes from a variable or from another command")
        code = "\n".join(line for line in script.splitlines() if not line.strip().startswith("#"))
        self.assertNotRegex(code, r"\beval\b|\bsource\b|\bexec\b|^\s*\. ", "nothing is run that is not written here")
        self.assertEqual(re.findall(r".*\bsudo\b.*", code), ['        echo "Run this as yourself, without sudo. It removes Kaitoi from your own DaVinci Resolve folder, and macOS asks for a password when one is needed."'],
                         "the word is only said, to someone who started it that way: there is no other way to act as someone else")
        self.assertEqual(code.count("administrator privileges"), 1)
        # Every program is named by its full path: none is found through a PATH someone else set.
        for program in re.findall(r"(?:^|[;|&!({]|\bif|\bthen|\belif)\s*(/[\w/]+)", code, re.M):
            self.assertRegex(program, r"^/(usr/)?s?bin/[a-z]+$")
        self.assertEqual(sorted(set(re.findall(r"(?<![\w/\"$}])(?:rm|rmdir|grep|osascript|pkgutil|id|read|cd|printf|echo)\b(?= )", code)) - {"read", "cd", "printf", "echo"}), [], "a program run by its bare name")

    def test_the_tests_stand_in_never_reaches_the_real_folder_or_the_password(self):
        script = self.script()
        self.assertEqual(script.count("KAITOI_TEST_SHARED"), 4, "tested for, checked, said when it is wrong, then taken")
        self.assertEqual(script.count("KAITOI_TEST_"), 5, "and the refusal a test can ask for")
        branch = re.search(r'if \[ "\$testing" = "1" \]; then\n(.*?)\n        elif ! /usr/bin/osascript', script, re.S)
        self.assertIsNotNone(branch, "a test takes one branch and the password the other: never both")
        self.assertNotIn("osascript", branch.group(1))
        self.assertIn('elif [ "$testing" = "0" ] && /usr/sbin/pkgutil --pkg-info io.kaitoi.resolve-plugin', script, "nor is the real package looked up from a test")

    def test_it_is_read_to_its_end_before_any_of_it_runs(self):
        """It lies in the folder it deletes."""
        script = self.script()
        self.assertTrue(script.rstrip().endswith('main "$@"\nexit $?'))
        outside = [line for line in script.split("\nmain() {\n")[0].splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual(outside, ["set -u"])
        self.assertTrue(os.access(self.SCRIPT, os.X_OK))


class TheUninstallerFromTheWindow(unittest.TestCase):
    """kaitoi_resolve.uninstall: finding the uninstaller the installer left, and running it."""

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_it_is_looked_for_beside_the_code_on_a_mac_only(self):
        with tempfile.TemporaryDirectory() as root:
            package = os.path.join(root, "Resolve", "kaitoi_resolve")
            os.makedirs(package)
            beside = os.path.join(root, "Resolve", "Uninstall Kaitoi.command")
            with mock.patch.object(uninstall, "__file__", os.path.join(package, "uninstall.py")), mock.patch.object(uninstall.sys, "platform", "darwin"):
                self.assertIsNone(uninstall.script(), "an install from the plugin's own folder has none")
                with open(beside, "w", encoding="utf-8") as fh:
                    fh.write("#!/bin/bash\n")
                os.chmod(beside, 0o644)
                self.assertIsNone(uninstall.script(), "one that cannot be run is not offered")
                os.chmod(beside, 0o755)
                self.assertEqual(uninstall.script(), beside)
                with mock.patch.object(uninstall.sys, "platform", "win32"):
                    self.assertIsNone(uninstall.script(), "Windows removes Kaitoi from its own list of apps")
                with mock.patch.object(uninstall.sys, "platform", "linux"):
                    self.assertIsNone(uninstall.script())
        with mock.patch.object(uninstall.sys, "platform", "darwin"):
            self.assertIsNone(uninstall.script(), "run from the repository, as these tests are, there is none beside the code")

    @unittest.skipUnless(__import__("sys").platform == "darwin", "the macOS uninstaller")
    def test_the_real_one_is_run_without_questions_and_its_answer_is_read(self):
        script = os.path.join(self.REPO, "packaging", "macos", "uninstall")
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            home, shared = os.path.join(root, "jos\u00e9"), os.path.join(root, "shared", "Kaitoi", "Resolve")  # an accent, as a home folder can have
            own = os.path.join(home, "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion")

            def fill():
                for path in (os.path.join(own, "Scripts/Edit/Kaitoi.py"), os.path.join(home, ".kaitoi_resolve/config.json"), os.path.join(shared, "kaitoi_resolve/ui.py")):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write("x")

            # Both named, always: left to itself it would act on the real home folder and ask for the real password.
            with mock.patch.dict(os.environ, {"HOME": home, "KAITOI_TEST_SHARED": shared}):
                fill()
                with mock.patch.dict(os.environ, {"KAITOI_TEST_REFUSE": "1"}):
                    self.assertEqual(uninstall.run(script, settings=True), (uninstall.NOTHING, []))
                self.assertTrue(os.path.exists(os.path.join(own, "Scripts/Edit/Kaitoi.py")))
                self.assertEqual(uninstall.run(script, settings=False), (uninstall.REMOVED, []))
                self.assertFalse(os.path.exists(os.path.join(own, "Scripts/Edit/Kaitoi.py")))
                self.assertFalse(os.path.exists(shared))
                self.assertTrue(os.path.exists(os.path.join(home, ".kaitoi_resolve/config.json")), "settings stay unless asked")
                fill()
                theirs = os.path.join(own, "Templates/Edit/Effects/Kaitoi")
                os.makedirs(theirs)
                with open(os.path.join(theirs, "Saved By Hand.setting"), "w", encoding="utf-8") as fh:
                    fh.write("theirs")
                self.assertEqual(uninstall.run(script, settings=True), (uninstall.LEFT, [theirs]))
                self.assertFalse(os.path.exists(os.path.join(home, ".kaitoi_resolve")))

    def test_what_the_uninstaller_says_is_read_as_utf_8_whatever_the_python_it_runs_in_expects(self):
        """Resolve's own Python may take text to be ASCII. A home folder with an accent in its name must not turn a finished uninstall into an error."""
        said = mock.Mock(returncode=3, stdout="Kaitoi was removed.\nLeft in place: /Users/jos\u00e9/Library/a folder/Kaitoi\n")
        with mock.patch.object(uninstall.subprocess, "run", return_value=said) as ran:
            self.assertEqual(uninstall.run("/a folder/Uninstall Kaitoi.command", settings=True), (3, ["/Users/jos\u00e9/Library/a folder/Kaitoi"]))
        (command,), how = ran.call_args
        self.assertEqual(command, ["/a folder/Uninstall Kaitoi.command", "--yes", "--settings"])
        self.assertEqual((how["encoding"], how["errors"]), ("utf-8", "replace"))
        self.assertNotIn("text", how)
        self.assertEqual(how["cwd"], "/", "not from the folder it deletes")

    def test_while_kaitoi_is_being_removed_its_folder_is_not_made_again(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "gone")
            with mock.patch.object(config, "APP_DIR", folder), mock.patch.object(config, "_private", set()), mock.patch("builtins.print"):
                self.addCleanup(config.leave_gone, False)
                config.leave_gone(True)
                self.assertEqual(config.app_dir(), folder)
                config.log("said after the uninstall")
                self.assertFalse(busy.holder())
                self.assertEqual(tools.thumbnail_dir(), os.path.join(folder, "thumbnails"))
                self.assertFalse(os.path.exists(folder), "nothing is put back behind the uninstaller")
                config.leave_gone(False)
                config.log("said when the uninstall did not happen")
                with open(os.path.join(folder, "plugin.log"), encoding="utf-8") as fh:
                    self.assertIn("did not happen", fh.read())


class PackageCheck(unittest.TestCase):
    """packaging/macos/check-payload.sh against listings written here.

    A real package lists its files in an order of its own. The mistake this
    guards against (the listing piped into a search that stops at its first
    match, which cuts the lister off and reads as a failure) shows only when
    the match comes early in a long listing, so the listing is made by hand.
    """

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    OURS = ["./install/Kaitoi.py", "./install/Generators/Kaitoi.setting", "./install/Effects/Kaitoi.setting", "./kaitoi_resolve/ui.py"]
    BULK = [f"./kaitoi_resolve/assets/icons/an-icon-with-a-long-name-to-fill-the-listing-{at:05}.png" for at in range(6000)]  # about 400 KB

    HOME = "/Library/Application Support/Kaitoi/Resolve"

    def check(self, lines, installs_into=HOME):
        import subprocess

        with tempfile.TemporaryDirectory() as work:
            listing = os.path.join(work, "listing.txt")
            with open(listing, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            lister = os.path.join(work, "pkgutil")  # stands in for the real one, and is cut off the same way
            with open(lister, "w", encoding="utf-8") as fh:
                fh.write(f'''#!/bin/bash
if [ "$1" = "--expand" ]; then
    /bin/mkdir -p "$3/component.pkg"
    echo '<pkg-info format-version="2" install-location="{installs_into}" auth="root">' > "$3/component.pkg/PackageInfo"
    exit 0
fi
exec /bin/cat "{listing}"
''')
            os.chmod(lister, 0o755)
            env = {**os.environ, "PATH": work + os.pathsep + os.environ.get("PATH", "")}
            done = subprocess.run(["/bin/bash", os.path.join(self.REPO, "packaging", "macos", "check-payload.sh"), "any.pkg"], env=env, capture_output=True, text=True)
            return done.returncode, done.stdout

    def test_a_long_listing_of_only_our_files_passes_wherever_the_wanted_ones_are(self):
        for name, lines in {"first": ["."] + self.OURS + self.BULK, "last": ["."] + self.BULK + self.OURS, "spread": ["."] + self.OURS[:2] + self.BULK + self.OURS[2:]}.items():
            for _ in range(3):
                self.assertEqual(self.check(lines)[0], 0, name)

    def test_someone_elses_folder_is_refused_wherever_it_is_listed(self):
        ours = ["."] + self.OURS + self.BULK
        for foreign in ("./Library", "./Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/Kaitoi.py", "./anything/Blackmagic Design/x", "./Users/Shared/x", "./usr/local/bin/x",
                        "./Scripts/Edit/Kaitoi.py", "./Templates", "./README.txt", "./._Scripts"):
            for name, lines in {"first": ours[:1] + [foreign] + ours[1:], "last": ours + [foreign], "middle": ours[:3000] + [foreign] + ours[3000:]}.items():
                code, said = self.check(lines)
                self.assertEqual(code, 1, f"{foreign} listed {name}")
                self.assertIn("not Kaitoi's own", said)

    def test_a_package_without_one_of_its_own_files_or_with_nothing_is_refused(self):
        for missing in self.OURS:
            self.assertEqual(self.check(["."] + [line for line in self.OURS if line != missing] + self.BULK)[0], 1, missing)
        self.assertEqual(self.check([])[0], 1)
        self.assertEqual(self.check(["."] + self.OURS + ["./kaitoi_resolve/Library/notes.txt", "./install/Library", "./LICENSE", "./._LICENSE", "./kaitoi_resolve/._ui.py", "./._install"])[0], 0, "what is inside our two folders is ours, whatever it is called")

    def test_the_uninstaller_is_ours_and_nothing_that_only_looks_like_it_is(self):
        ours = ["."] + self.OURS
        self.assertEqual(self.check(ours + ["./Uninstall Kaitoi.command", "./._Uninstall Kaitoi.command"])[0], 0)
        for foreign in ("./Uninstall Kaitoi.command/inside", "./Uninstall KaitoiXcommand", "./Uninstall Other.command", "./Uninstall Kaitoi.command.sh"):
            code, said = self.check(ours + [foreign])
            self.assertEqual(code, 1, foreign)
            self.assertIn("not Kaitoi's own", said)

    def test_our_own_files_installed_into_someone_elses_folder_are_refused(self):
        ours = ["."] + self.OURS
        self.assertEqual(self.check(ours)[0], 0)
        for elsewhere in ("/", "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion", "/Library/Application Support", "/Library/Application Support/Kaitoi", "",
                          # Kaitoi's folder is in each of these, and none of them is it.
                          "/Library/Application Support/Kaitoi/Resolve/../../..", "/Library/Application Support/Kaitoi/Resolve 2", "/Volumes/Other/Library/Application Support/Kaitoi/Resolve"):
            code, said = self.check(ours, installs_into=elsewhere)
            self.assertEqual(code, 1, elsewhere)
            self.assertRegex(said, "not Kaitoi's own|could not read")


@unittest.skipUnless(__import__("sys").platform == "darwin", "the macOS package's last step")
class AfterTheMacInstall(unittest.TestCase):
    """packaging/macos/postinstall, run as an ordinary user on folders of its own."""

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    OWN = "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion"
    THREE = sorted(["Scripts/Edit/Kaitoi.py", "Templates/Edit/Generators/Kaitoi.setting", "Templates/Edit/Effects/Kaitoi.setting"])
    TEXTS = {"Scripts/Edit/Kaitoi.py": "script", "Templates/Edit/Generators/Kaitoi.setting": "generator", "Templates/Edit/Effects/Kaitoi.setting": "effect"}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = os.path.join(self.dir.name, "a mac")  # a space, as real paths here have
        for folder in ("staged/Generators", "staged/Effects", "home/Library"):
            os.makedirs(os.path.join(self.root, folder))
        self.library = os.path.join(self.root, "home", "Library")
        os.chmod(self.library, 0o700)  # as every account's is
        for name, text in (("Kaitoi.py", "script"), ("Generators/Kaitoi.setting", "generator"), ("Effects/Kaitoi.setting", "effect")):
            self.put(f"staged/{name}", text)
        self.own = os.path.join(self.root, "home", self.OWN)

    def put(self, name, text="x"):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def read(self, name):
        with open(os.path.join(self.own, name), encoding="utf-8") as fh:
            return fh.read()

    def closed(self, folder):
        os.chmod(folder, 0o555)
        self.addCleanup(os.chmod, folder, 0o755)

    def locked(self, path):
        """A file its owner cannot replace until the lock is taken off."""
        import subprocess

        subprocess.run(["/usr/bin/chflags", "uchg", path], check=True)
        self.addCleanup(subprocess.run, ["/usr/bin/chflags", "nouchg", path])

    # The only programs run outside as_user, as the shell says it ran them: they look and change nothing.
    LOOKED = [r"/usr/bin/stat -f%Su /dev/console", r"/usr/bin/id -u", r"/usr/bin/id -u [\w.'-]*", r"/usr/bin/dscl -plist /Search -read /Users/[\w.-]* NFSHomeDirectory",
              r"/usr/bin/plutil -extract dsAttrTypeStandard:NFSHomeDirectory.0 raw -"]

    def install(self, **changed):
        """Run the install step, and hold every run to the rule the reading tests below put on the text:
        the shell prints each command it runs and the function it is in, and outside as_user only the lookups may be programs."""
        import pwd
        import subprocess

        env = {**os.environ, "USER": pwd.getpwuid(os.getuid()).pw_name, "KAITOI_TEST_STAGED": os.path.join(self.root, "staged"), "KAITOI_TEST_HOME": os.path.join(self.root, "home"),
               "PS4": "+${FUNCNAME[0]:-main}| ", **changed}
        done = subprocess.run(["/bin/bash", "-x", os.path.join(self.REPO, "packaging", "macos", "postinstall")], env=env, capture_output=True, text=True)  # the system's own, as Installer uses
        self.assertEqual(done.returncode, 0, "whatever happens, the install itself is not failed: " + done.stderr[-2000:])
        ran = re.findall(r"^\++(\w+)\| (\S+)(.*)$", done.stderr, re.M)
        self.assertGreater(len(ran), 15, "the shell stopped saying what it ran")
        loose = [program + rest for where, program, rest in ran if program.startswith("/") and where != "as_user" and not any(re.fullmatch(look, program + rest) for look in self.LOOKED)]
        self.assertEqual(loose, [], "a program run outside as_user that is not one of the lookups")
        self.assertEqual([program for where, program, _ in ran if where == "as_user" and not re.fullmatch(r"/(usr/)?bin/[a-z]+", program)], [], "as_user was handed something that is not a system program by its full path")
        words = {program for where, program, _ in ran if where != "as_user" and not program.startswith("/") and not re.match(r"[A-Za-z_]\w*(\[[^\]]*\])?=", program)}
        self.assertEqual(sorted(words - self.WORDS - {"'['", "[["}), [], "a command run by its bare name")
        return done.stdout

    def files(self, folder):
        return sorted(os.path.relpath(os.path.join(base, name), folder) for base, _, names in os.walk(folder) for name in names)

    def everything(self, folder):
        """Every file, folder and link below a folder: nothing made there goes unseen."""
        return sorted(os.path.relpath(os.path.join(base, name), folder) for base, folders, names in os.walk(folder) for name in folders + names)

    def test_the_three_files_go_into_the_users_own_resolve_folder(self):
        folders = [self.own, f"{self.own}/Scripts", f"{self.own}/Scripts/Edit", f"{self.own}/Templates"]
        for folder in folders:
            os.makedirs(folder, exist_ok=True)
        for folder in folders:
            os.chmod(folder, 0o777)  # as Resolve keeps them, inside a Library closed to everyone else
        # What an install from the plugin's folder left, and a script of the user's own.
        self.put(f"home/{self.OWN}/Scripts/Edit/Kaitoi.py", "old")
        self.put(f"home/{self.OWN}/Scripts/Edit/Kaitoi Video.py")
        self.put(f"home/{self.OWN}/Scripts/Edit/kaitoi_resolve/ui.py")
        self.put(f"home/{self.OWN}/Scripts/Edit/My Own.py")
        self.assertIn("added to DaVinci Resolve for", self.install())
        self.assertEqual(self.files(self.own), sorted(self.THREE + ["Scripts/Edit/My Own.py"]), "Kaitoi's three, the user's own script, and no leftover Resolve would list")
        for name in self.THREE:
            self.assertEqual(self.read(name), self.TEXTS[name])
            self.assertEqual(os.stat(f"{self.own}/{name}").st_mode & 0o777, 0o644, "the files themselves only their owner can change")
        self.assertEqual([os.stat(folder).st_mode & 0o777 for folder in folders], [0o777] * 4, "no existing folder has its permissions changed")
        self.assertEqual(os.stat(self.library).st_mode & 0o777, 0o700)
        self.install()
        self.assertEqual(self.files(self.own), sorted(self.THREE + ["Scripts/Edit/My Own.py"]), "installing again changes nothing")

    def test_folders_resolve_has_not_made_yet_are_made(self):
        self.assertIn("added to DaVinci Resolve for", self.install())
        self.assertEqual(self.files(self.own), self.THREE)
        self.assertEqual(os.stat(self.library).st_mode & 0o777, 0o700, "the Library above them stays closed to other accounts")

    def test_a_folder_that_cannot_be_written_leaves_everything_as_it_was(self):
        self.put(f"home/{self.OWN}/Scripts/Edit/Kaitoi.py", "old")
        self.put(f"home/{self.OWN}/Scripts/Edit/Kaitoi Video.py")
        self.put(f"home/{self.OWN}/Templates/Edit/Generators/Kaitoi.setting", "old")
        os.makedirs(f"{self.own}/Templates/Edit/Effects")
        self.closed(f"{self.own}/Templates/Edit/Effects")
        said = self.install()
        self.assertIn("could not be written, so Kaitoi was not added", said)
        self.assertIn("README.txt", said)
        self.assertEqual(self.files(self.own), ["Scripts/Edit/Kaitoi Video.py", "Scripts/Edit/Kaitoi.py", "Templates/Edit/Generators/Kaitoi.setting"], "nothing new, nothing unfinished, nothing taken away")
        self.assertEqual((self.read("Scripts/Edit/Kaitoi.py"), self.read("Templates/Edit/Generators/Kaitoi.setting")), ("old", "old"), "not one file is replaced unless all three can be written")

    def test_with_nowhere_to_write_the_install_still_ends_well_and_says_where_the_files_are(self):
        support = os.path.join(self.library, "Application Support")
        os.makedirs(support)
        self.closed(support)
        said = self.install()
        self.assertIn("could not be written, so Kaitoi was not added", said)
        self.assertIn(os.path.join(self.root, "staged", "README.txt"), said)
        self.assertEqual(self.everything(self.library), ["Application Support"])

    def test_an_account_without_a_library_folder_is_not_given_one(self):
        os.rmdir(self.library)
        said = self.install()
        self.assertIn("has no Library folder, so Kaitoi was not added", said)
        self.assertIn("README.txt", said)
        self.assertEqual(os.listdir(os.path.join(self.root, "home")), [], "one made here would be open to the other accounts")

    def test_a_library_other_accounts_can_get_into_is_not_used(self):
        """What keeps the other accounts of the Mac out of this step is the Library being private."""
        for mode in (0o755, 0o750, 0o705, 0o770, 0o707, 0o1701):
            os.chmod(self.library, mode)
            said = self.install()
            self.assertIn("Library folder of", said, oct(mode))
            self.assertNotIn("could not be", said, oct(mode))
            self.assertIn("is open to other accounts of this Mac", said, oct(mode))
            self.assertIn("README.txt", said)
            self.assertEqual(self.everything(self.library), [], oct(mode))
        os.chmod(self.library, 0o1700)  # a bit that lets nobody else in is no reason to refuse
        self.assertIn("added to DaVinci Resolve for", self.install())

    def test_a_library_its_owner_has_closed_to_themselves_is_not_called_open(self):
        os.chmod(self.library, 0o000)
        self.addCleanup(os.chmod, self.library, 0o700)
        said = self.install()
        self.assertIn("Library folder of", said)
        self.assertIn("could not be checked", said)
        self.assertNotIn("open to other accounts", said, "nobody can get in at all")

    def test_a_home_folder_other_accounts_can_change_is_not_used(self):
        """There, the Library could be swapped for another after it has been checked."""
        home = os.path.join(self.root, "home")
        for mode in (0o775, 0o757, 0o777, 0o1777):
            os.chmod(home, mode)
            said = self.install()
            self.assertIn("the home folder of", said, oct(mode))
            self.assertIn("can be changed by other accounts of this Mac", said, oct(mode))
            self.assertEqual(self.everything(self.library), [], oct(mode))
        os.chmod(home, 0o750)  # as a Mac makes them: others in its group may look, and change nothing
        self.assertIn("added to DaVinci Resolve for", self.install())

    def named(self, folder, entry):
        """Let someone in by name, which the folder's permission numbers do not show."""
        import subprocess

        subprocess.run(["/bin/chmod", "+a", entry, folder], check=True)
        self.addCleanup(subprocess.run, ["/bin/chmod", "-N", folder])

    def test_others_let_into_the_library_or_the_home_folder_by_name_are_seen_too(self):
        home = os.path.join(self.root, "home")
        for folder in (self.library, home):
            for first in (None, "everyone deny delete"):  # alone, and after the entry every Mac puts there itself
                with self.subTest(folder=os.path.basename(folder), first=first):
                    if first:
                        self.named(folder, first)
                    self.named(folder, "everyone allow list,search,add_file,add_subdirectory,delete_child")
                    said = self.install()
                    self.assertIn("has extra permissions that let an account in by name", said)
                    self.assertIn("Library folder of" if folder == self.library else "home folder of", said)
                    self.assertEqual(self.everything(self.library), [])
                    __import__("subprocess").run(["/bin/chmod", "-N", folder], check=True)

    def test_a_home_folder_that_is_a_link_is_judged_by_the_folder_it_leads_to(self):
        """Only an administrator decides where a home folder is, so a link is followed; what is checked is where it leads."""
        linked = os.path.join(self.root, "linked home")
        for wrong, said_of_it in (("open", "can be changed by other accounts of this Mac"), ("named", "has extra permissions that let an account in by name")):
            with self.subTest(wrong):
                real = os.path.join(self.root, f"real home, {wrong}")
                os.makedirs(os.path.join(real, "Library"))
                os.chmod(os.path.join(real, "Library"), 0o700)
                if wrong == "open":
                    os.chmod(real, 0o777)
                else:
                    self.named(real, "everyone allow add_subdirectory,delete_child")
                os.symlink(real, linked)
                try:
                    self.assertIn(said_of_it, self.install(KAITOI_TEST_HOME=linked))
                    self.assertEqual(self.everything(os.path.join(real, "Library")), [])
                finally:
                    os.remove(linked)
        real = os.path.join(self.root, "real home, closed")
        os.makedirs(os.path.join(real, "Library"))
        os.chmod(os.path.join(real, "Library"), 0o700)
        os.symlink(real, linked)
        self.assertIn("added to DaVinci Resolve for", self.install(KAITOI_TEST_HOME=linked))

    def test_the_entries_every_mac_puts_on_those_folders_refuse_nobody(self):
        for folder in (self.library, os.path.join(self.root, "home")):
            self.named(folder, "everyone deny delete")  # on every home folder and Library: it lets nobody in
        self.assertIn("added to DaVinci Resolve for", self.install())
        self.assertEqual(self.files(self.own), self.THREE)

    def test_a_home_folder_named_like_an_entry_is_not_refused(self):
        # Who is let in by name is read from a listing whose first line is the folder's own name.
        home = os.path.join(self.root, "allow me in")
        os.makedirs(os.path.join(home, "Library"))
        os.chmod(os.path.join(home, "Library"), 0o700)
        self.assertIn("added to DaVinci Resolve for", self.install(KAITOI_TEST_HOME=home))
        self.assertEqual(self.files(os.path.join(home, self.OWN)), self.THREE)

    def test_a_library_that_is_a_link_is_not_used(self):
        real = os.path.join(self.root, "elsewhere")
        os.rmdir(self.library)
        os.makedirs(real)
        os.chmod(real, 0o700)  # private and the user's own, and still not the Library
        os.symlink(real, self.library)
        said = self.install()
        self.assertIn("Library folder of", said)
        self.assertIn("is a link", said)
        self.assertEqual(self.everything(real), [])

    def test_folders_that_belong_to_another_account_are_not_used(self):
        # "nobody" is an account of every Mac, and not the one these folders belong to.
        said = self.install(USER="nobody")
        self.assertIn("the home folder of nobody belongs to another account", said)
        self.assertEqual(self.everything(self.library), [])

    def test_a_file_where_a_folder_goes_is_said_to_be_in_the_way(self):
        in_the_way = self.put("home/Library/Application Support/Blackmagic Design", "a file of the user's")
        said = self.install()
        self.assertIn(f"something that is not a folder is in the way at {in_the_way}", said)
        self.assertNotIn("could not be written", said)
        self.assertEqual(self.everything(self.library), ["Application Support", "Application Support/Blackmagic Design"])
        with open(in_the_way, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "a file of the user's")

    def test_a_name_that_is_no_account_gets_nothing_and_is_told_so(self):
        said = self.install(USER="no-such-account-kaitoi")
        self.assertIn("no-such-account-kaitoi is not an account of this Mac", said)
        self.assertNotIn("nobody is logged in", said, "somebody is: the name was wrong")
        self.assertEqual(self.everything(self.library), [])

    def test_one_of_the_systems_own_accounts_gets_nothing(self):
        said = self.install(USER="daemon")
        self.assertIn("daemon has an account number below 501", said)
        self.assertEqual(self.everything(self.library), [])

    def test_a_package_folder_missing_its_files_is_said_to_be_that(self):
        os.remove(os.path.join(self.root, "staged", "Effects", "Kaitoi.setting"))
        said = self.install()
        self.assertIn("the package's own files are missing", said)
        self.assertIn("Run the installer again", said)
        self.assertNotIn("could not be written", said, "Resolve's folder was never tried")
        self.assertEqual(self.everything(self.library), [], "two of three is not an install")

    def test_a_link_on_the_way_down_to_a_folder_others_can_write_is_refused(self):
        """A user's Resolve folder linked to one every account shares: placing files there can be interfered with."""
        shared = os.path.join(self.root, "shared", "Blackmagic Design")
        os.makedirs(os.path.join(shared, "DaVinci Resolve", "Fusion", "Scripts", "Edit"))
        victim = self.put("shared/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/kaitoi_resolve/theirs.py", "theirs")
        os.makedirs(os.path.join(self.library, "Application Support"))
        link = os.path.join(self.library, "Application Support", "Blackmagic Design")
        os.symlink(shared, link)
        before = self.everything(os.path.join(self.root, "shared"))
        said = self.install()
        self.assertIn(f"{link} is a link", said)
        self.assertIn("was not added", said)
        self.assertEqual(self.everything(os.path.join(self.root, "shared")), before, "nothing is made, placed or deleted through the link")
        with open(victim, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "theirs")
        self.assertTrue(os.path.islink(link), "and the link is the user's to keep")

    def test_a_link_further_down_is_refused_too_and_nothing_is_left_half_done(self):
        elsewhere = os.path.join(self.library, "elsewhere")  # inside the Library: still a link, still refused
        os.makedirs(elsewhere)
        os.makedirs(f"{self.own}/Templates/Edit")
        os.symlink(elsewhere, f"{self.own}/Templates/Edit/Effects")  # the last of the three folders
        self.put(f"home/{self.OWN}/Scripts/Edit/Kaitoi.py", "old")
        said = self.install()
        self.assertIn("Templates/Edit/Effects is a link", said)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(self.files(self.own), ["Scripts/Edit/Kaitoi.py"], "the two already written beside their places are taken away again")
        self.assertEqual(self.read("Scripts/Edit/Kaitoi.py"), "old")

    def test_a_file_that_cannot_be_replaced_is_reported_and_not_called_a_success(self):
        self.put(f"home/{self.OWN}/Scripts/Edit/Kaitoi.py", "old")
        self.locked(self.put(f"home/{self.OWN}/Templates/Edit/Generators/Kaitoi.setting", "old"))
        said = self.install()
        self.assertIn("only 1 of Kaitoi's 3 files", said)
        self.assertIn("README.txt", said)
        self.assertNotIn("added to DaVinci Resolve for", said)
        self.assertEqual(self.files(self.own), ["Scripts/Edit/Kaitoi.py", "Templates/Edit/Generators/Kaitoi.setting"], "it stops at the first that will not go, and leaves nothing unfinished")
        self.assertEqual((self.read("Scripts/Edit/Kaitoi.py"), self.read("Templates/Edit/Generators/Kaitoi.setting")), ("script", "old"))

    def test_when_the_first_file_cannot_be_replaced_nothing_is_changed(self):
        self.locked(self.put(f"home/{self.OWN}/Scripts/Edit/Kaitoi.py", "old"))
        self.put(f"home/{self.OWN}/Scripts/Edit/Kaitoi Video.py")
        said = self.install()
        self.assertIn("was not added", said)
        self.assertNotIn("only", said)
        self.assertEqual(self.files(self.own), ["Scripts/Edit/Kaitoi Video.py", "Scripts/Edit/Kaitoi.py"])
        self.assertEqual(self.read("Scripts/Edit/Kaitoi.py"), "old")

    def test_a_link_where_a_file_goes_is_replaced_and_never_written_through(self):
        precious = self.put("precious.txt", "someone's file")
        os.makedirs(f"{self.own}/Scripts/Edit")
        os.symlink(precious, f"{self.own}/Scripts/Edit/Kaitoi.py")
        self.install()
        with open(precious, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "someone's file")
        self.assertFalse(os.path.islink(f"{self.own}/Scripts/Edit/Kaitoi.py"))
        self.assertEqual(self.read("Scripts/Edit/Kaitoi.py"), "script")
        self.assertEqual(self.files(self.own), self.THREE, "and nothing unfinished is left beside them")

    def test_a_link_to_a_folder_where_a_file_goes_is_replaced_and_nothing_is_put_inside_it(self):
        elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(elsewhere)
        os.makedirs(f"{self.own}/Scripts/Edit")
        os.symlink(elsewhere, f"{self.own}/Scripts/Edit/Kaitoi.py")
        self.assertIn("added to DaVinci Resolve for", self.install())
        self.assertEqual(os.listdir(elsewhere), [], "a move onto a link to a folder would otherwise land inside that folder")
        self.assertFalse(os.path.islink(f"{self.own}/Scripts/Edit/Kaitoi.py"))
        self.assertEqual(self.read("Scripts/Edit/Kaitoi.py"), "script")

    def test_a_folder_where_a_file_goes_is_left_alone_and_said_to_be_in_the_way(self):
        self.put(f"home/{self.OWN}/Templates/Edit/Effects/Kaitoi.setting/theirs.txt", "theirs")
        self.put(f"home/{self.OWN}/Scripts/Edit/Kaitoi.py", "old")
        said = self.install()
        self.assertIn("a folder is in the way at", said)
        self.assertNotIn("could not be written", said, "it could: something of the user's is where the file goes")
        self.assertEqual(self.files(self.own), ["Scripts/Edit/Kaitoi.py", "Templates/Edit/Effects/Kaitoi.setting/theirs.txt"], "nothing is put inside it and nothing unfinished is left")
        self.assertEqual(self.read("Scripts/Edit/Kaitoi.py"), "old")

    def script(self):
        with open(os.path.join(self.REPO, "packaging", "macos", "postinstall"), encoding="utf-8") as fh:
            return fh.read()

    def code(self):
        """The script without its comments. A " #" only ever starts one: never inside quotes, where what follows would be dropped unread."""
        lines = []
        for line in self.script().splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            kept, hash_, _ = line.partition(" #")
            if hash_:
                self.assertEqual(kept.count('"') % 2, 0, f"a \" #\" inside quotes, which would hide the rest of the line from these checks: {line}")
            lines.append(kept.rstrip())
        return "\n".join(lines)

    def test_resolves_folder_for_every_user_of_the_mac_is_never_named(self):
        """Read as text: an install cannot be pointed away from the real shared folder in a test."""
        code = self.code()
        self.assertEqual(re.findall(r'"(/[^"$]*)', code.replace("2>/dev/null", "")), ["/Library/Application Support/Kaitoi/Resolve/install", "/Users/"], "the only places named outright: the package's own folder, and where accounts are looked up")
        self.assertEqual(re.findall(r"^(.*)Blackmagic Design", code, re.M), ['below="Application Support/'], "Resolve's folder is named once, as a path below the user's own Library")
        # What decides who is acted as and where things go is set once, in this order, and never again:
        # nothing after the checks can point the steps that follow somewhere else.
        self.assertEqual(re.findall(r"(?<![\w$])(staged|user|home|uid|library|below|fusion)=", code), ["staged", "user", "user", "home", "staged", "home", "uid", "library", "below", "fusion"])
        self.assertIn('\nlibrary="$home/Library"\n', code)
        self.assertIn('\nfusion="$library/$below"\n', code)
        self.assertEqual(len(re.findall(r"\$home\b|\$\{home\b", code)), 4, "the home folder is used for nothing else: the test's own, tested for, checked, then the Library")
        self.assertEqual(len(re.findall(r"\$below\b|\$\{below\b", code)), 2, "and the path below it twice: the folder itself, and the walk down to it")

    # Where a command can begin: the start of a line, and after anything that
    # ends or opens one; then past any "NAME=value" set for that command alone.
    # The command is looked at and not taken, so that "if", itself a command
    # word, is still seen as what the next command comes after.
    BEGINS = r'''(?:^|&&|\|\||;|\||&|\$\(|(?<![=$(\w])\(|\bthen\b|\bdo\b|\belse\b|\bif\b|\belif\b|\bwhile\b|!|\{)[^\S\n]*(?:[A-Za-z_]\w*=(?!\()(?:"[^"\n]*"|[^\s"])*[^\S\n]+)*'''
    COMMAND = re.compile(BEGINS + r'''(?=["']?([A-Za-z_/][\w./-]*)["']?(?:\s|$|;|\)))''', re.M)  # in quotes or not
    FROM_A_VARIABLE = re.compile(BEGINS + r'''"?\$[{\w@*]''', re.M)
    WORDS = {"if", "then", "else", "fi", "for", "while", "do", "done", "local", "return", "exit", "echo", "true",
             "as_user", "not_added", "only_theirs", "folder_for", "stage", "finish", "clear_unfinished"}  # the shell's own, and the script's own
    # The only programs run outside as_user, each with the whole of what it is given: they look and change nothing.
    LOOKS = [
        "/usr/bin/stat -f%Su /dev/console 2>/dev/null",
        '/usr/bin/dscl -plist /Search -read "/Users/$user" NFSHomeDirectory 2>/dev/null',
        "/usr/bin/plutil -extract dsAttrTypeStandard:NFSHomeDirectory.0 raw - 2>/dev/null",
        "/usr/bin/id -u",
        '/usr/bin/id -u "$user" 2>/dev/null',
    ]

    def test_every_step_that_could_change_anything_runs_as_the_user_and_never_as_root(self):
        """Read as text: the root branch cannot be run in a test.

        Not a list of commands that write, which the next new one would not be
        on: outside as_user, only the shell's own words, the script's own
        functions and five exact lookups are allowed at all.
        """
        code = self.code()
        commands = self.COMMAND.findall(code)
        self.assertGreater(len(commands), 60)
        self.assertEqual(sorted({command for command in commands if not command.startswith("/")} - self.WORDS), [], "a program run by its bare name, or a shell word that could run one")
        programs = [command for command in commands if command.startswith("/")]
        self.assertEqual(sorted(set(programs) - {look.split()[0] for look in self.LOOKS} - {"/usr/bin/sudo"}), [], "a program run as root that is not one of those that only look")
        self.assertEqual(programs.count("/usr/bin/sudo"), 1)
        # A program that only looks when given this can change things when given that: each is pinned to its whole command.
        looks = [found.strip() for found in re.findall(r"(?<!as_user )(/usr/bin/(?:stat|dscl|plutil|id)\b[^|)\n]*)", code)]
        self.assertEqual(looks, self.LOOKS)
        self.assertEqual(self.FROM_A_VARIABLE.findall(code), ['{ "$@'], "nothing is run from a variable, apart from the test's own as_user running what it was given")
        handed = re.findall(r"\bas_user\s+(\S+)", code)
        self.assertGreaterEqual(len(handed), 14)
        self.assertEqual([program for program in handed if not re.fullmatch(r"/(usr/)?bin/[a-z]+", program)], [], "what is handed to as_user is a system program by its full path")
        self.assertEqual(re.findall(r">(?!/dev/null).{0,20}", code), [], "nothing is written with a redirect: every one goes to /dev/null, whatever it redirects")
        self.assertNotRegex(code, r"`|<", "no second way to run a command, and nothing read from a text block or string: the shell keeps those in a file it writes as root")
        self.assertNotRegex(self.script(), r"\bchown\b|\beval\b|\bsource\b|\bfunction\b|\balias\b|\bexec\b|\btrap\b|\bcommand\b|\bbuiltin\b|^\s*\. ")
        # Ways of setting a variable that are not "NAME=", which the pin on who is acted as would not see;
        # and the settings the test above relies on to hear what was run.
        self.assertEqual(set(re.findall(r"\bfor\s+(\w+\s+in\s+[^;]*)", code)), {"i in 0 1 2"}, "a loop sets its variable too")
        self.assertNotRegex(code, r":=|(?<!\$)\(\(|\bPS4\b|BASH_XTRACEFD|\bSUDO_")
        # Where the files come from and go to, as written: no part of a path here is "..", or empty.
        for line in ('sources=("Kaitoi.py" "Generators/Kaitoi.setting" "Effects/Kaitoi.setting")', 'folders=("Scripts/Edit" "Templates/Edit/Generators" "Templates/Edit/Effects")', 'names=("Kaitoi.py" "Kaitoi.setting" "Kaitoi.setting")',
                     '    [ -n "$rest" ] || return 1', '        [ -n "$part" ] || return 1', '    [ -n "$listing" ] || not_added "$3 could not be checked"'):
            self.assertEqual(code.count("\n" + line + "\n"), 1, line)
        self.assertNotIn("..", code)

    def test_an_install_started_from_terminal_names_whoever_is_at_the_screen(self):
        said = self.install(USER="")  # nobody named: the owner of the screen is asked for
        if "nobody is logged in" not in said and "below 501" not in said and "belongs to another account" not in said:  # a machine with someone else, or nobody, at its screen stops there
            self.assertIn("added to DaVinci Resolve for", said)

    def test_as_root_the_user_is_always_dropped_to_and_the_test_overrides_are_ignored(self):
        script = self.script()
        self.assertEqual(len(re.findall(r"\bas_user\s*\(\s*\)", script)), 2, "defined twice, once for each side of the branch, and nowhere else")
        branch = re.search(r'if \[ "\$\(/usr/bin/id -u\)" != "0" \]; then\n(.*?)\nelse\n(.*?)\nfi\n', script, re.S)
        self.assertIsNotNone(branch, "who it runs as is asked of the system, by a full path: a variable can be set by whoever starts the script")
        not_root, root = branch.group(1), branch.group(2)
        self.assertEqual(root.strip(), 'as_user() { /usr/bin/sudo -u "$user" "$@"; }', "as root, every as_user step is run as the person installing")
        self.assertEqual(script.count("KAITOI_TEST_"), not_root.count("KAITOI_TEST_"), "the overrides exist only where the script is not root")
        self.assertEqual(not_root.count("KAITOI_TEST_"), 2)
        self.assertNotIn("sudo", script.replace(root, ""), "and nothing else in it raises or changes who it runs as")
        self.assertNotRegex(script, r"\bEUID\b|\bUID\b")
        # The account sudo drops to is never root under any name, and is settled before the first step that uses it.
        settled = script.index('[ "$uid" -ge 501 ] 2>/dev/null || not_added')
        self.assertIn('uid="$(/usr/bin/id -u "$user" 2>/dev/null || true)"', script)
        uses = [found.start() for found in re.finditer(r"as_user /", script)]
        self.assertTrue(uses and min(uses) > settled, "no step runs as the user before the user has been checked")
        # The first of them only finds out whether the user can be acted as at all, so that a later failure is not put down to their folders.
        self.assertEqual(min(uses), script.index('as_user /usr/bin/true 2>/dev/null || not_added "the installer could not act as $user"'))
        self.assertIn("/usr/bin/dscl -plist /Search -read ", script, "an account kept in a network directory has a home folder too")
        # And nothing is made or placed before the Library is known to be private. What comes
        # before that line in the file is only said, as functions, and run after it.
        private = script.index('only_theirs "$library" "*00" "the Library folder of $user" "is open to other accounts of this Mac"')
        body = script[: script.index("\nfolder_for() {")]
        self.assertGreater(body.index("only_theirs() {"), settled)
        self.assertEqual(re.findall(r"as_user /(?:usr/)?bin/(?!test\b|true\b|stat\b|ls\b)\w+", body), [], "before the walk down begins, as_user only looks")
        self.assertGreater(script.index("\nfolder_for() {"), private)
        self.assertGreater(private, script.index('only_theirs "$home" "*[0145][0145]" "the home folder of $user" "can be changed by other accounts of this Mac"'))


class RegisteredEffects(unittest.TestCase):
    def session(self, effects):
        resolve = mock.Mock()
        if isinstance(effects, Exception):
            resolve.GetEffectsList.side_effect = effects
        else:
            resolve.GetEffectsList.return_value = effects
        return mock.Mock(resolve=resolve)

    def test_only_fusion_effects_in_the_folder_count_whatever_their_case(self):
        effects = [
            {"name": "Rain ", "category": "fusion", "id": "Fusion/Effects/Kaitoi/Rain"},
            {"name": "Elsewhere", "category": "fusion", "id": "Fusion/Effects/Other/Elsewhere"},
            {"name": "Root", "category": "fusion", "id": "Fusion/Fusion Effects/Root"},
            {"name": "Lookalike", "category": "openfx", "id": "OFX/Kaitoi/Lookalike"},
            {"name": None, "category": "fusion", "id": "Fusion/Effects/Kaitoi/"},
        ]
        self.assertEqual(resolve_bridge.registered_effects(self.session(effects), "Kaitoi") - {""}, {"rain"})

    def test_none_when_resolve_cannot_say(self):
        self.assertIsNone(resolve_bridge.registered_effects(self.session(AttributeError("no such call")), "Kaitoi"))
        self.assertIsNone(resolve_bridge.registered_effects(self.session(None), "Kaitoi"))
        self.assertEqual(resolve_bridge.registered_effects(self.session([]), "Kaitoi"), set(), "an empty list is an answer: nothing is loaded")


class Checks(unittest.TestCase):
    def test_a_matching_result_raises_nothing(self):
        info = {"kind": "video", "fps": 24.0, "width": 1920, "height": 1080, "seconds": 6.0}
        self.assertEqual(checks.result_warnings(info, timeline_fps=24.0, timeline_size=(1920, 1080), source_frames=144, sent_kind="video"), [])

    def test_rate_size_shape_and_length_are_each_named(self):
        info = {"kind": "video", "fps": 30.0, "width": 720, "height": 720, "seconds": 5.0}
        notes = checks.result_warnings(info, timeline_fps=23.976, timeline_size=(1920, 1080), source_frames=144, sent_kind="video")
        self.assertEqual([note.split(":")[0] for note in notes], ["Frame rate", "Size", "Shape", "Length"])
        self.assertIn("23.976", notes[0])

    def test_length_is_not_compared_when_nothing_was_sent(self):
        info = {"kind": "video", "fps": 24.0, "width": 1920, "height": 1080, "seconds": 5.0}
        self.assertEqual(checks.result_warnings(info, timeline_fps=24.0, timeline_size=(1920, 1080), source_frames=144, sent_kind=None), [])


class Busy(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(config, "APP_DIR", self.dir.name)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_one_run_at_a_time(self):
        self.assertTrue(busy.acquire("a", "Rain"))
        self.assertFalse(busy.acquire("b", "Card"))
        self.assertEqual(busy.held_by_other("b"), "Rain")
        self.assertEqual(busy.held_by_other("a"), "")
        busy.release("b")
        self.assertEqual(busy.held_by_other("b"), "Rain", "only the holder can release")
        busy.release("a")
        self.assertTrue(busy.acquire("b", "Card"))

    def test_a_marker_left_by_a_crash_expires(self):
        busy.acquire("a", "Rain")
        with mock.patch.object(busy.time, "time", return_value=busy.time.time() + busy.STALE_SECONDS + 1):
            self.assertEqual(busy.held_by_other("b"), "")


class FakeTimeline:
    def __init__(self, marks, start=86400):
        self.marks, self.start, self.calls = marks, start, []

    def GetMarkInOut(self):
        return self.marks

    def GetStartFrame(self):
        return self.start

    def ClearMarkInOut(self, kind):
        self.calls.append(("clear", kind)); return True

    def SetMarkInOut(self, mark_in, mark_out, kind):
        self.calls.append(("set", mark_in, mark_out, kind)); return kind == "all"


class ScriptingModule(unittest.TestCase):
    """Where Resolve's scripting module is taken from: never the loader in the folder every account can write to."""

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    class Module:
        def scriptapp(self, name):
            return f"the {name} this module reaches"

        def UIDispatcher(self, ui):
            return ("dispatcher", ui)

    def setUp(self):
        import sys

        self.sys = sys
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        # Blackmagic's loader as another account of the computer could leave it: it says so when it is run.
        self.planted = os.path.join(self.dir.name, "Developer", "Scripting")
        os.makedirs(os.path.join(self.planted, "Modules"))
        self.ran = os.path.join(self.dir.name, "the planted loader ran")
        with open(os.path.join(self.planted, "Modules", "DaVinciResolveScript.py"), "w", encoding="utf-8") as fh:
            fh.write(f"open({self.ran!r}, 'w').close()\nscriptapp = UIDispatcher = lambda *a: None\n")
        for patcher in (mock.patch.dict(os.environ, {"RESOLVE_SCRIPT_API": self.planted, "RESOLVE_SCRIPT_LIB": os.path.join(self.dir.name, "no such library")}),
                        mock.patch.object(sys, "path", sys.path + [os.path.join(self.planted, "Modules")]),  # even with it importable by name
                        mock.patch.dict(sys.modules)):
            patcher.start(); self.addCleanup(patcher.stop)
        sys.modules.pop("fusionscript", None)
        sys.modules.pop("DaVinciResolveScript", None)
        self.host = sys.modules["__main__"]

    def untouched(self):
        self.assertFalse(os.path.exists(self.ran), "the loader in the shared folder was run")
        self.assertNotIn("DaVinciResolveScript", self.sys.modules)

    def test_inside_resolve_the_module_resolve_hands_over_is_used(self):
        handed, hosts, loaded = self.Module(), self.Module(), self.Module()
        self.assertIs(resolve_bridge._scripting_module(handed), handed)
        with mock.patch.object(self.host, "bmd", hosts, create=True):
            self.assertIs(resolve_bridge._scripting_module(), hosts, "what the host gave the script")
            self.assertIs(resolve_bridge._scripting_module(handed), handed)
            self.assertIs(resolve_bridge._scripting_module(object()), hosts, "something handed over that is not the module is passed by")
            for missing in ("scriptapp", "UIDispatcher"):
                half = self.Module()
                setattr(half, missing, None)  # as on Resolve's own handles, which answer None for a name they do not have
                self.assertIs(resolve_bridge._scripting_module(half), hosts, f"and so is one without {missing}: the window would fail on it later")
        self.sys.modules["fusionscript"] = loaded
        self.assertIs(resolve_bridge._scripting_module(), loaded, "or what the host had already loaded for itself")
        self.untouched()

    def test_from_a_terminal_it_is_loaded_from_resolves_own_program_folder(self):
        asked = []

        class Loader:
            def __init__(self, name, path):
                asked.append((name, path))

            def create_module(self, spec):
                return None

            def exec_module(self, module):
                module.scriptapp, module.UIDispatcher = (lambda name: "resolve"), (lambda ui: "dispatcher")

        with mock.patch.object(resolve_bridge.importlib.machinery, "ExtensionFileLoader", Loader):
            module = resolve_bridge._scripting_module()
            self.assertEqual(asked, [("fusionscript", os.path.join(self.dir.name, "no such library"))], "the library the user pointed at")
            self.assertIs(resolve_bridge._scripting_module(), module, "and it is loaded once")
            self.assertEqual(len(asked), 1)
            self.sys.modules.pop("fusionscript")
            with mock.patch.dict(os.environ):
                del os.environ["RESOLVE_SCRIPT_LIB"]
                resolve_bridge._scripting_module()
            self.assertEqual(asked[-1], ("fusionscript", resolve_bridge._LIB_PATHS[resolve_bridge._platform_key()]))
        self.untouched()
        self.assertEqual(self.sys.path.count(os.path.join(self.planted, "Modules")), 1, "nothing is added to where Python looks for modules")

    def test_a_library_that_is_not_there_is_an_error_the_user_can_read_and_nothing_else_is_tried(self):
        with self.assertRaises(resolve_bridge.ResolveError) as raised:
            resolve_bridge._scripting_module()
        self.assertIn("Could not load the DaVinci Resolve scripting module", str(raised.exception))
        self.untouched()

    def test_connecting_uses_what_resolve_handed_over_for_the_window_too(self):
        handed = self.Module()
        session = resolve_bridge.connect(resolve="the Resolve handle", module=handed)
        self.assertEqual((session.resolve, session.dvr), ("the Resolve handle", handed))
        session = resolve_bridge.connect(module=handed)
        self.assertEqual(session.resolve, "the Resolve this module reaches")
        self.untouched()

    def test_no_folder_another_account_can_write_is_named_for_it(self):
        with open(os.path.join(self.REPO, "kaitoi_resolve", "resolve_bridge.py"), "r", encoding="utf-8") as fh:
            source = fh.read()
        code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
        self.assertNotRegex(code, r"import\s+DaVinciResolveScript|import_module\(|__import__\(|sys\.path\.(append|insert)|RESOLVE_SCRIPT_API|PROGRAMDATA|Developer/Scripting(?!/Modules\), which)")
        self.assertEqual(resolve_bridge._LIB_PATHS["darwin"], "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so")
        self.assertTrue(resolve_bridge._LIB_PATHS["win32"].startswith("C:\\Program Files\\"), "Program Files, not ProgramData")
        with open(os.path.join(self.REPO, "Kaitoi.py"), "r", encoding="utf-8") as fh:
            self.assertIn('resolve_bridge.connect(resolve=globals().get("resolve"), module=globals().get("bmd"))', fh.read())


class BridgePure(unittest.TestCase):
    def test_timecode_round_trip_and_drop_frame(self):
        self.assertEqual(resolve_bridge.timecode_to_frame("01:00:00:00", 24), 86400)
        self.assertEqual(resolve_bridge.frame_to_timecode(86424, 24), "01:00:01:00")
        self.assertEqual(resolve_bridge.timecode_to_frame("00:01:00;02", 29.97), 1800)

    def test_marks_are_offsets_with_an_inclusive_out(self):
        self.assertEqual(resolve_bridge.read_marks(FakeTimeline({"video": {"in": 24, "out": 71}, "audio": {}})), (86424, 86472))
        self.assertIsNone(resolve_bridge.read_marks(FakeTimeline({"video": {"in": 24}, "audio": {}})))

    def test_marks_are_put_back_as_one_pair(self):
        timeline = FakeTimeline({})
        resolve_bridge._restore_marks(timeline, {"video": {"in": 10, "out": 200}, "audio": {"in": 10, "out": 200}})
        self.assertEqual(timeline.calls, [("clear", "all"), ("set", 10, 200, "all")])

    def test_a_lone_mark_is_cleared_not_replaced_by_the_export_range(self):
        timeline = FakeTimeline({})
        with mock.patch.object(config, "log"):
            resolve_bridge._restore_marks(timeline, {"video": {"in": 10}, "audio": {"in": 10}})
        self.assertEqual(timeline.calls, [("clear", "all")])

    def test_media_info_reads_resolves_clip_properties(self):
        class Item:
            def __init__(self, props): self.props = props
            def GetClipProperty(self): return self.props

        video = resolve_bridge.media_info(Item({"Type": "Video + Audio", "FPS": 30.0, "Frames": "150", "Resolution": "1280x720", "Audio Ch": "2"}), 24.0)
        self.assertEqual((video["kind"], video["seconds"], video["width"], video["audio_channels"]), ("video", 5.0, 1280, 2))
        still = resolve_bridge.media_info(Item({"Type": "Still", "FPS": 24.0, "Frames": "1", "Resolution": "1920x1080"}), 24.0)
        self.assertEqual((still["kind"], still["seconds"]), ("image", 0.0))
        sound = resolve_bridge.media_info(Item({"Type": "Audio", "FPS": 24.0, "Frames": "", "Duration": "00:00:02:00"}), 24.0)
        self.assertEqual((sound["kind"], sound["seconds"]), ("audio", 2.0))


if __name__ == "__main__":
    unittest.main()
