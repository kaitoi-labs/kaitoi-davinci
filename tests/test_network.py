"""The Kaitoi side, against a stand-in server: listing, uploads, runs, node graphs, and the job with no timeline involved."""

from __future__ import annotations

from . import HOME  # noqa: F401 - first: outside the tests package, where home is not redirected, this file does not load

import os
import tempfile
import time
import unittest
from unittest import mock

import threading

from kaitoi_resolve import api, config, graphs, pipeline, templates, tools

from .fake_kaitoi import NODE_DETAILS, FakeKaitoi, fake_jpeg, fake_png

VIDEO_PIN = {"name": "Clip", "fieldName": "video", "dataType": "video", "kind": "file"}


class Base(unittest.TestCase):
    def setUp(self):
        self.server = FakeKaitoi().start()
        self.addCleanup(self.server.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(config, "APP_DIR", os.path.join(self.dir.name, "app"))
        patcher.start(); self.addCleanup(patcher.stop)
        quiet = mock.patch.object(config, "log")
        quiet.start(); self.addCleanup(quiet.stop)
        self.cfg = api.Config("key-123", self.server.url, timeout=10)
        self.mark = tools.account_mark("key-123")  # the account the lists are loaded and stored for
        self.new_session()

    def new_session(self):
        """As if the plugin had just been started."""
        tools._contracts_asked.clear()
        tools._renditions_refused = 0
        tools._thumbnails_refused.clear()
        tools._tiles_tried.clear()
        tools._tiles_refused = 0

    def paths(self, method=None):
        return [entry["path"].split("?")[0] for entry in self.server.seen if method in (None, entry["method"])]

    def temp_file(self, name, data=b"x" * 2048):
        path = os.path.join(self.dir.name, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path


class Listing(Base):
    def test_templates_follow_the_cursor_to_the_end(self):
        found = api.list_templates(self.cfg, page_size=1)
        self.assertEqual([t["name"] for t in found], ["Rain", "Looks"])
        self.assertTrue(all(entry["auth"] == "Bearer key-123" for entry in self.server.seen))

    def test_workflows_load_their_contracts_once(self):
        found, snapshot = tools.load_workflow_tools(self.cfg)
        self.assertEqual(sorted(t.name for t in found), ["Looks: Cold", "Looks: Warm", "Rain"])
        self.assertEqual(tools.task_label(found[0]), "Video → Video")
        self.assertEqual(sum(path.endswith("/docs") for path in self.paths()), 3)
        self.server.seen.clear()
        again, _ = tools.load_workflow_tools(self.cfg, snapshot)
        self.assertEqual(sum(path.endswith("/docs") for path in self.paths()), 0, "an unchanged endpoint reuses its stored contract")
        self.assertEqual([t.id for t in again], [t.id for t in found])
        self.assertEqual([t.id for t in tools.tools_from_snapshot(snapshot)], [t.id for t in found])

    def test_a_list_that_carries_contracts_needs_no_extra_calls(self):
        self.server.inline_contracts = True
        found, _ = tools.load_workflow_tools(self.cfg)
        self.assertEqual(len(found), 3)
        self.assertEqual(sum(path.endswith("/docs") for path in self.paths()), 0)

    def test_node_catalog_pages_and_leaves_out_bare_loaders(self):
        self.server.lists_pins = False
        nodes = tools.load_node_catalog(self.cfg)
        self.assertEqual(sorted(n.name for n in nodes), ["Sparkle", "Video Reverse & Speed"])
        self.assertEqual({n.origin for n in nodes}, {"builtin", "community"})
        self.assertFalse(any(n.ready for n in nodes))
        loaded = tools.load_node(self.cfg, next(n for n in nodes if n.name == "Sparkle"))
        self.assertTrue(loaded.ready)
        self.assertEqual(tools.task_label(loaded), "Image → Video")
        tools.write_nodes(self.cfg.base_url, self.mark, nodes)
        self.assertEqual(len(tools.read_nodes(self.cfg.base_url, self.mark)), 2)
        self.assertEqual(tools.read_nodes("http://elsewhere", self.mark), [], "a catalog from another server is not reused")
        self.assertEqual(tools.read_nodes(self.cfg.base_url, tools.account_mark("another-key")), [], "nor one another account loaded: it holds that account's own nodes")
        self.assertEqual(tools.read_nodes(self.cfg.base_url, ""), [], "and with nobody signed in nothing is shown")

    def test_pins_of_nodes_opened_one_by_one_are_not_kept_between_sessions(self):
        self.server.lists_pins = False
        nodes = tools.load_node_catalog(self.cfg)
        opened = [tools.load_node(self.cfg, node) if node.name == "Sparkle" else node for node in nodes]
        tools.write_nodes(self.cfg.base_url, self.mark, opened, with_pins=False)
        self.assertFalse(any(node.ready for node in tools.read_nodes(self.cfg.base_url, self.mark)), "its author may change it; it is read again when next opened")
        tools.write_nodes(self.cfg.base_url, self.mark, opened)
        self.assertEqual([node.name for node in tools.read_nodes(self.cfg.base_url, self.mark) if node.ready], ["Sparkle"])

    def test_a_server_that_lists_pins_but_not_the_two_marks_still_gives_the_pins(self):
        self.server.lists_kinds = False
        nodes = api.list_node_types(self.cfg, page_size=2)
        listed = [entry["path"] for entry in self.server.seen if entry["path"].startswith("/api/v1/node-types?")]
        self.assertEqual(sum("session" in path for path in listed), 1, "refused once, and not asked for again on the pages after")
        self.assertTrue(all("inputs" in path for path in listed))
        self.assertTrue(all(isinstance(node.get("inputs"), list) for node in nodes))
        tool = tools.node_from_summary(nodes[0])
        self.assertTrue(tool.ready)
        self.assertEqual((tool.session, tool.agent), (False, False), "a server that does not say leaves both off")

    def test_a_current_server_is_asked_for_everything_at_once(self):
        api.list_node_types(self.cfg, page_size=2)
        listed = [entry["path"] for entry in self.server.seen if entry["path"].startswith("/api/v1/node-types?")]
        self.assertTrue(listed and all("session" in path and "agent" in path and "inputs" in path for path in listed))

    def test_an_older_server_is_asked_for_pins_only_once(self):
        self.server.lists_pins = False
        tools.load_node_catalog(self.cfg)
        listed = [entry["path"] for entry in self.server.seen if entry["path"].startswith("/api/v1/node-types?")]
        self.assertEqual(sum("inputs" in path for path in listed), 2, "refused with the two marks, refused without them, then every page is asked for with neither")
        self.assertEqual(sum("session" in path for path in listed), 1)
        self.assertEqual(len(listed), 4, "the two refused requests, then two pages")

    def test_a_current_server_lists_every_node_ready_with_its_choices_and_ranges(self):
        nodes = {n.name: n for n in tools.load_node_catalog(self.cfg)}
        self.assertTrue(all(n.ready for n in nodes.values()))
        self.assertFalse(any("/node-types/" in path for path in self.paths()), "no request per node")
        speed = next(f for f in nodes["Video Reverse & Speed"].inputs if f.name == "speedFactor")
        self.assertEqual((speed.minimum, speed.maximum, speed.step), (0.1, 10.0, 0.1))
        style = next(f for f in nodes["Sparkle"].inputs if f.name == "style")
        self.assertEqual(style.choices, (("soft", "Soft glow"), ("hard", "hard")))
        self.assertEqual(style.widget, "choice")
        self.assertEqual(tools.task_label(nodes["Sparkle"]), "Image → Video")
        # The stored catalog opens ready too, with nothing lost.
        tools.write_nodes(self.cfg.base_url, self.mark, list(nodes.values()))
        stored = {n.name: n for n in tools.read_nodes(self.cfg.base_url, self.mark)}
        self.assertEqual(stored["Sparkle"], nodes["Sparkle"])
        self.assertEqual(stored["Video Reverse & Speed"], nodes["Video Reverse & Speed"])


class Transfers(Base):
    def test_upload_goes_to_storage_then_completes(self):
        record = api.upload_file(self.cfg, self.temp_file("clip.mp4", b"CLIP"))
        self.assertEqual(record["fileId"], "file-clip.mp4")
        self.assertEqual(self.server.uploads["clip.mp4"], b"CLIP")
        put = next(entry for entry in self.server.seen if entry["method"] == "PUT")
        self.assertIsNone(put["auth"], "the API key is never sent to storage")

    def test_download_is_whole_or_absent(self):
        target = os.path.join(self.dir.name, "out.mp4")
        api.download_to(target, f"{self.server.url}/storage/out1.mp4", config=self.cfg)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"RESULT-BYTES")
        missing = os.path.join(self.dir.name, "missing.mp4")
        with self.assertRaises(api.KaitoiAPIError):
            api.download_to(missing, f"{self.server.url}/storage/nope.mp4", config=self.cfg)
        self.assertFalse(os.path.exists(missing))
        self.assertFalse(os.path.exists(missing + ".part"))

    def test_the_key_stays_with_the_api_host(self):
        self.assertTrue(api._same_origin(f"{self.server.url}/x", self.cfg.base_url))
        self.assertFalse(api._same_origin("http://127.0.0.1:1/x", self.cfg.base_url))
        self.assertFalse(api._same_origin("https://storage.example.com/x", self.cfg.base_url))

    def test_server_errors_carry_their_code(self):
        with self.assertRaises(api.KaitoiAPIError) as caught:
            api.get_node_type(self.cfg, "builtin/none")
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "NODE_TYPE_NOT_FOUND"))


class WorkflowsThatCannotBeRead(Base):
    def setUp(self):
        super().setUp()
        root = mock.patch.object(templates, "templates_root", lambda: os.path.join(self.dir.name, "Templates"))
        root.start(); self.addCleanup(root.stop)

    def test_an_edited_workflow_whose_contract_fails_keeps_working_and_stays_installed(self):
        found, snapshot = tools.load_workflow_tools(self.cfg)
        templates.sync(found)
        self.assertEqual(sorted(templates.installed()), ["workflow:e1", "workflow:e2", "workflow:e3"])
        self.server.stamps["e2"] = "2026-02-02T00:00:00Z"  # edited in Studio
        self.server.docs_fail.add("e2")  # and its contract cannot be read just now
        again, later = tools.load_workflow_tools(self.cfg, snapshot)
        self.assertEqual(sorted(t.id for t in again), ["workflow:e1", "workflow:e2", "workflow:e3"], "it runs from the contract stored before")
        self.assertEqual(later["skipped"], ["e2"])
        self.assertEqual(later["contracts"]["e2"]["updatedAt"], "", "stored without a date, so it is never taken for up to date")
        self.assertEqual(len(later["contracts"]["e2"]["inputs"]), 3, "with the inputs it had")
        templates.sync(again, keep={f"workflow:{endpoint_id}" for endpoint_id in later["skipped"]})
        self.assertEqual(sorted(templates.installed()), ["workflow:e1", "workflow:e2", "workflow:e3"])
        self.server.docs_fail.clear()
        self.server.seen.clear()
        tools.load_workflow_tools(self.cfg, later)
        self.assertEqual(sum(path.endswith("/docs") for path in self.paths()), 1, "only the one that failed is asked for again")

    def test_a_workflow_never_read_before_is_left_out_but_not_uninstalled(self):
        found, _ = tools.load_workflow_tools(self.cfg)
        templates.sync(found)
        self.server.docs_fail.add("e2")
        again, later = tools.load_workflow_tools(self.cfg)  # nothing stored to fall back on
        self.assertEqual(sorted(t.id for t in again), ["workflow:e1", "workflow:e3"])
        self.assertEqual(later["skipped"], ["e2"])
        templates.sync(again, keep={"workflow:e2"})
        self.assertIn("workflow:e2", templates.installed(), "published, only unreadable: its template stays in Resolve")
        templates.sync(again)
        self.assertNotIn("workflow:e2", templates.installed(), "without that, it is what an unpublished workflow looks like")

    def docs_calls(self, known):
        self.server.seen.clear()
        found, snapshot = tools.load_workflow_tools(self.cfg, known)
        return sum(path.endswith("/docs") for path in self.paths()), found, snapshot

    def test_contracts_from_an_older_server_gain_their_choices_once_the_server_can_say(self):
        self.server.describes_inputs = False
        calls, found, snapshot = self.docs_calls(None)
        self.assertEqual(calls, 3)
        self.assertEqual([f.name for f in found[0].inputs], ["video", "prompt"])
        self.assertFalse(snapshot["contracts"]["e1"]["detailed"])
        self.assertEqual(self.docs_calls(snapshot)[0], 0, "not asked again on every reload")
        self.new_session()
        self.assertEqual(self.docs_calls(snapshot)[0], 3, "asked once more each time the plugin starts")
        # The server is updated. Nothing about the endpoints changed, so their dates did not move.
        self.server.describes_inputs = True
        self.new_session()
        calls, found, upgraded = self.docs_calls(snapshot)
        self.assertEqual(calls, 3)
        style = next(f for f in found[0].inputs if f.name == "style")
        self.assertEqual((style.choices, style.default), ((("warm", "Warm"), ("cold", "Cold")), "warm"))
        self.assertTrue(upgraded["contracts"]["e1"]["detailed"])
        self.new_session()
        self.assertEqual(self.docs_calls(upgraded)[0], 0, "a contract that describes its inputs is kept until the endpoint changes")

    def test_a_contract_that_fails_while_the_server_is_being_updated_is_asked_for_again(self):
        self.server.describes_inputs = False
        _, _, old = self.docs_calls(None)
        self.server.describes_inputs = True
        self.server.docs_fail.add("e2")
        self.new_session()
        calls, found, partly = self.docs_calls(old)
        self.assertEqual((calls, partly["skipped"]), (3, ["e2"]))
        self.assertEqual(len(found), 3, "it still runs, from the old contract")
        self.server.docs_fail.clear()
        calls, found, whole = self.docs_calls(partly)  # same session: the failure must not count as having asked
        self.assertEqual(calls, 1)
        self.assertTrue(all(contract["detailed"] for contract in whole["contracts"].values()))
        self.assertTrue(all(any(f.name == "style" for f in tool.inputs) for tool in found))

    def test_a_template_saved_again_refreshes_its_saved_values_and_its_thumbnail_name(self):
        calls, found, snapshot = self.docs_calls(None)
        rain = next(tool for tool in found if tool.name == "Rain")
        self.assertEqual(next(f for f in rain.inputs if f.name == "style").default, "warm")
        # In Studio the saved style is changed and the template saved. Its endpoint is not touched.
        self.server.saved_style = "cold"
        self.server.template_stamps["t1"] = "2026-03-03T00:00:00Z"
        self.new_session()
        calls, found, later = self.docs_calls(snapshot)
        self.assertEqual(calls, 1, "only the template that changed is read again")
        changed = next(tool for tool in found if tool.name == "Rain")
        self.assertEqual(next(f for f in changed.inputs if f.name == "style").default, "cold")
        self.assertNotEqual(tools.thumbnail_path(changed), tools.thumbnail_path(rain), "its thumbnail is fetched afresh too")
        self.assertEqual(self.docs_calls(later)[0], 0)

    def test_a_contract_written_by_the_previous_plugin_version_still_loads(self):
        _, _, snapshot = self.docs_calls(None)
        legacy = {"base_url": snapshot["base_url"], "templates": snapshot["templates"], "contract_version": 2,
                  "contracts": {key: {"updatedAt": value["updatedAt"], "inputs": [dict(VIDEO_PIN)], "outputs": value["outputs"]} for key, value in snapshot["contracts"].items()}}
        self.assertEqual(len(tools.tools_from_snapshot(legacy)), 3, "the window opens from it before the network answers")
        self.new_session()
        self.assertEqual(self.docs_calls(legacy)[0], 3, "and it is replaced by what the server says now")


class NodesThatFillInTheirOwnLists(Base):
    DETAIL = {"type": "builtin/voice/speak", "title": "Speak", "source": "builtin", "outputs": [{"name": "audio", "type": "audio"}], "inputs": [
        {"name": "prompt", "type": "string", "default": "Hello"},
        {"name": "selectedVoice", "type": "string", "label": "Voice", "default": None, "options": None, "dynamic": True},
        {"name": "selectedModel", "type": "string", "label": "Model", "default": "selectedModel.value"},
    ]}

    def test_the_lists_a_node_made_are_put_on_its_inputs(self):
        node = tools.node_from_detail(self.DETAIL)
        self.assertTrue(tools.needs_initial_state(node))
        state = api.get_node_initial_state(self.cfg, node.node_type)
        self.assertIn("/api/v1/node-type-initial-state/builtin%2Fvoice%2Fspeak", self.server.seen[-1]["path"])
        filled = tools.apply_initial_state(node, state)
        prompt, voice, model = filled.inputs
        self.assertEqual((voice.choices, voice.default, voice.widget), ((("v1", "Aria"), ("v2", "Blade")), "v1", "choice"))
        self.assertEqual(tools.default_label(voice), "Aria")
        self.assertEqual((prompt.options, prompt.default), ((), "Hello"), "an input that is not the node's to fill in is left as it was")
        self.assertEqual((model.options, model.default, model.dynamic), ((), None, True), "one the node said nothing about stays a plain box")
        self.assertTrue(tools.needs_initial_state(filled), "and is still wanting")
        only_voice = tools.node_from_detail({**self.DETAIL, "inputs": self.DETAIL["inputs"][:2]})
        self.assertFalse(tools.needs_initial_state(tools.apply_initial_state(only_voice, state)))
        self.assertEqual(tools.node_from_summary(tools._node_row(filled)), node, "what the node filled in is not stored: a list of voices goes stale")
        self.assertEqual(tools.apply_initial_state(node, {"inputs": "nonsense"}), node)
        self.assertEqual(tools.apply_initial_state(node, None), node)

    def test_an_input_the_node_filled_in_and_left_alone_sends_what_the_node_filled_in(self):
        detail = {**NODE_DETAILS["community/acme/sparkle"], "inputs": NODE_DETAILS["community/acme/sparkle"]["inputs"] + [
            {"name": "selectedVoice", "type": "string", "label": "Voice", "default": None, "options": None, "dynamic": True}]}
        node = tools.apply_initial_state(tools.node_from_detail(detail), api.get_node_initial_state(self.cfg, "community/acme/sparkle"))
        with mock.patch.object(api, "get_node_type", lambda cfg, node_type: {**detail, "inputs": detail["inputs"]} if node_type == "community/acme/sparkle" else NODE_DETAILS[node_type]):
            left_alone = graphs.build_node_graph(self.cfg, node, {"selectedVoice": None, "prompt": "hi"}, {})
            picked = graphs.build_node_graph(self.cfg, node, {"selectedVoice": "v2", "prompt": "hi"}, {})
        sent = {n["id"]: n for n in left_alone["graph"]["nodes"]}["node"]["inputs"]
        self.assertEqual(sent["selectedVoice"], {"type": "string", "value": "v1"}, "the entry reads 'Node default (Aria)', so Aria is what runs")
        self.assertEqual({n["id"]: n for n in picked["graph"]["nodes"]}["node"]["inputs"]["selectedVoice"]["value"], "v2")
        self.assertEqual(sent["style"], {"type": "string", "value": "soft"}, "an ordinary input left alone still takes the catalog's default")

    def test_a_plain_box_the_node_only_wrote_a_sample_into_is_left_out_when_emptied(self):
        detail = {**NODE_DETAILS["community/acme/sparkle"], "inputs": NODE_DETAILS["community/acme/sparkle"]["inputs"] + [
            {"name": "query", "type": "string", "label": "Search", "default": None, "options": None, "dynamic": True}]}
        node = tools.apply_initial_state(tools.node_from_detail(detail), {"inputs": {"query": {"options": None, "value": "mountain landscape"}}})
        query = next(f for f in node.inputs if f.name == "query")
        self.assertEqual((query.default, query.options, query.widget), ("mountain landscape", (), "text"))
        with mock.patch.object(api, "get_node_type", lambda cfg, node_type: detail if node_type == "community/acme/sparkle" else NODE_DETAILS[node_type]):
            emptied = graphs.build_node_graph(self.cfg, node, {"query": None, "prompt": "hi"}, {})
            typed = graphs.build_node_graph(self.cfg, node, {"query": "rivers", "prompt": "hi"}, {})
        sent = {n["id"]: n for n in emptied["graph"]["nodes"]}["node"]["inputs"]["query"]
        self.assertNotEqual(sent.get("value"), "mountain landscape", "the sample is not put back into a box the user emptied")
        self.assertEqual({n["id"]: n for n in typed["graph"]["nodes"]}["node"]["inputs"]["query"]["value"], "rivers")

    def test_a_prompt_the_node_writes_an_example_into_is_no_reason_to_start_it(self):
        only_prompt = tools.node_from_detail({"type": "builtin/img/gen", "title": "Gen", "source": "builtin", "outputs": [], "inputs": [
            {"name": "prompt", "type": "string", "default": "prompt.value"}, {"name": "steps", "type": "int", "default": 20}]})
        self.assertTrue(only_prompt.inputs[0].dynamic)
        self.assertFalse(tools.needs_initial_state(only_prompt))
        with_list = tools.node_from_detail(self.DETAIL)
        self.assertTrue(tools.needs_initial_state(with_list))

    def test_a_node_that_cannot_start_and_an_older_server_are_told_apart(self):
        self.server.initial_state = "fails"
        with self.assertRaises(api.KaitoiAPIError) as caught:
            api.get_node_initial_state(self.cfg, "builtin/voice/speak")
        self.assertEqual((caught.exception.status, caught.exception.code), (422, "NODE_TYPE_INIT_FAILED"))
        self.server.initial_state = "missing"
        with self.assertRaises(api.KaitoiAPIError) as caught:
            api.get_node_initial_state(self.cfg, "builtin/voice/speak")
        self.assertEqual(caught.exception.status, 404)

    def test_nodes_with_nothing_to_fill_in_are_not_asked_about(self):
        plain = tools.node_from_detail({**self.DETAIL, "inputs": self.DETAIL["inputs"][:1]})
        self.assertFalse(tools.needs_initial_state(plain))
        unloaded = tools.node_from_summary({"type": "builtin/voice/speak", "title": "Speak"})
        self.assertFalse(tools.needs_initial_state(unloaded), "its inputs are not known yet")
        workflow, _ = tools.load_workflow_tools(self.cfg)
        self.assertFalse(any(tools.needs_initial_state(tool) for tool in workflow))


class Thumbnails(Base):
    def rain(self):
        found, _ = tools.load_workflow_tools(self.cfg)
        return next(tool for tool in found if tool.name == "Rain")

    def test_a_workflow_thumbnail_is_fetched_small_once_and_without_the_api_key(self):
        rain = self.rain()
        self.assertIn("/template-thumbnails/t1?token=signed", rain.thumbnail_url)
        self.server.seen.clear()
        path = tools.fetch_thumbnail(self.cfg, rain)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), self.server.thumbnail_bytes)
        asked = [entry for entry in self.server.seen if "-thumbnails/" in entry["path"]]
        self.assertEqual(len(asked), 1)
        self.assertEqual(tools.thumbnail_tile(rain), path, "already 16:9: shown as it is")
        self.assertIn("size=256&format=jpeg", asked[0]["path"])
        self.assertIsNone(asked[0]["auth"], "the signed link is the permission; the key stays home")
        self.assertEqual(tools.fetch_thumbnail(self.cfg, rain), path)
        self.assertEqual(len([entry for entry in self.server.seen if "-thumbnails/" in entry["path"]]), 1, "kept on disk, not fetched twice")
        self.assertEqual([name for name in os.listdir(os.path.dirname(path)) if not name.endswith(".jpg")], [], "nothing half-fetched left behind")

    def pictures(self):
        return [entry for entry in self.server.seen if "-thumbnails/" in entry["path"]]

    def test_a_thumbnail_of_another_shape_is_also_kept_cut_to_16_by_9(self):
        self.server.thumbnail_bytes = fake_jpeg(256, 256)
        self.server.thumbnail_png = fake_png(64, 64, 3, lambda x, y: (x * 4, y * 4, 7))
        rain = self.rain()
        self.server.seen.clear()
        path = tools.fetch_thumbnail(self.cfg, rain)
        asked = self.pictures()
        self.assertEqual(len(asked), 2)
        self.assertIn("size=256&format=png", asked[1]["path"])
        self.assertIsNone(asked[1]["auth"], "the signed link is the permission here too")
        with open(tools.tile_path(rain), "rb") as fh:
            width, height, channels, rows = tools._png_rows(fh.read())
        self.assertEqual((width, height, channels), (64, 36, 3))
        self.assertEqual(tools.thumbnail_tile(rain), tools.tile_path(rain), "what the list and the card show")
        self.assertEqual(rows[0][:3], bytes((0, 14 * 4, 7)), "the middle rows: 14 are cut from the top")
        self.assertEqual(tools.fetch_thumbnail(self.cfg, rain), path)
        self.assertEqual(len(self.pictures()), 2, "both are kept on disk")
        self.assertEqual(sorted(name.rsplit(".", 2)[-2:] != ["tile", "png"] and not name.endswith(".jpg") for name in os.listdir(os.path.dirname(path))), [False, False], "nothing half-fetched left behind")

    def square(self, png=None):
        """A workflow whose thumbnail is square, fetched; ``png`` is what the server sends for its cut."""
        self.server.thumbnail_bytes = fake_jpeg(256, 256)
        self.server.thumbnail_png = fake_png(32, 48, 4, lambda x, y: (200, 100, 50, 255)) if png is None else png
        rain = self.rain()
        return rain, tools.fetch_thumbnail(self.cfg, rain)

    def names(self):
        return sorted(os.listdir(tools.thumbnail_dir()))

    def test_a_thumbnail_kept_from_before_gets_its_cut_and_a_cut_on_disk_is_not_fetched_again(self):
        rain, path = self.square()
        os.remove(tools.tile_path(rain))  # as it was before lists could show a picture of this shape
        self.new_session()
        self.server.seen.clear()
        self.assertEqual(tools.fetch_thumbnail(self.cfg, rain), path)
        asked = self.pictures()
        self.assertEqual(len(asked), 1, "only the cut: the thumbnail itself is already there")
        self.assertIn("format=png", asked[0]["path"])
        with open(tools.tile_path(rain), "rb") as fh:
            self.assertEqual(tools._png_rows(fh.read())[:3], (32, 18, 3))
        self.server.seen.clear()
        self.new_session()
        tools.fetch_thumbnail(self.cfg, rain)
        self.assertEqual(self.pictures(), [], "a cut on disk is not fetched again")

    def test_a_cut_the_server_will_not_make_is_not_asked_for_at_every_start(self):
        for refusal in ("not a picture", 415):
            with self.subTest(refusal=refusal):
                for name in self.names():
                    os.remove(os.path.join(tools.thumbnail_dir(), name))
                self.new_session()
                self.server.png_status = refusal if isinstance(refusal, int) else None
                rain, path = self.square(png=b"not a picture")
                self.assertIsNone(tools.thumbnail_tile(rain), "no cut: the list and the card both keep the stand-in")
                self.assertEqual(self.names(), sorted([os.path.basename(path), os.path.basename(tools.no_tile_path(rain))]), "the thumbnail and a note that it has no cut")
                self.server.png_status = None
                self.server.thumbnail_png = fake_png(32, 48, 3, lambda x, y: (1, 2, 3))
                for _ in range(2):  # this session, then the next
                    self.server.seen.clear()
                    self.assertEqual(tools.fetch_thumbnail(self.cfg, rain), path)
                    self.assertEqual(self.pictures(), [], "the note is believed")
                    self.new_session()
                old = time.time() - tools.THUMBNAIL_KEEP_SECONDS - 60
                os.utime(tools.no_tile_path(rain), (old, old))
                tools.fetch_thumbnail(self.cfg, rain)
                self.assertEqual(len(self.pictures()), 1, "after a week it is asked for once more")
                self.assertEqual(self.names(), sorted([os.path.basename(path), os.path.basename(tools.tile_path(rain))]), "and the note goes when the cut arrives")

    def test_a_cut_that_failed_for_a_passing_reason_is_asked_for_next_time(self):
        for status in (429, 503, 401, 403, 408):  # busy, down, or a link that has run out
            with self.subTest(status=status):
                for name in self.names():
                    os.remove(os.path.join(tools.thumbnail_dir(), name))
                self.new_session()
                self.server.png_status = status
                rain, path = self.square()
                self.assertEqual(self.names(), [os.path.basename(path)], "no note: nothing was decided")
                self.server.seen.clear()
                tools.fetch_thumbnail(self.cfg, rain)
                self.assertEqual(self.pictures(), [], "not again while the plugin runs")
                self.new_session()
                self.server.png_status = None
                tools.fetch_thumbnail(self.cfg, rain)
                self.assertEqual(tools.thumbnail_tile(rain), tools.tile_path(rain))

    def test_a_reply_that_is_not_a_picture_never_stops_the_loading(self):
        from .fake_kaitoi import fake_png as png

        good = png(64, 64, 3, lambda x, y: (x, y, 0))
        header_at = good.index(b"IHDR") - 4
        for name, sent in {
            "cut inside its header": good[:25],
            "a header that says it is empty": good[:header_at] + b"\x00\x00\x00\x00" + good[header_at + 4 :],
            "nothing": b"",
        }.items():
            with self.subTest(name):
                for left in self.names():
                    os.remove(os.path.join(tools.thumbnail_dir(), left))
                self.new_session()
                rain, path = self.square(png=sent)
                self.assertTrue(path.endswith(".jpg"), "the thumbnail itself is still kept")
                self.assertIsNone(tools.thumbnail_tile(rain))
        for left in self.names():
            os.remove(os.path.join(tools.thumbnail_dir(), left))
        self.new_session()
        rain, path = self.square()
        os.remove(tools.tile_path(rain))
        self.new_session()
        self.server.thumbnail_broken = True
        self.assertEqual(tools.fetch_thumbnail(self.cfg, rain), path, "a reply that breaks off part way costs only the cut")
        self.assertEqual(self.names(), [os.path.basename(path)], "and leaves nothing half-written")
        with self.assertRaises(api.KaitoiAPIError) as caught:
            api.download_to(self.temp_file("broken.bin", b""), f"{self.server.url}/api/v1/template-thumbnails/t1?token=x")
        self.assertEqual(caught.exception.code, "NETWORK")
        self.server.thumbnail_broken = False
        for link in ("thumbs/x.jpg?token=t", "not a link at all", "ftp//x"):
            lost = tools.node_from_summary({"type": "builtin/lost", "title": "Lost", "thumbnailUrl": link})
            tools._thumbnails_refused.clear()
            self.assertIsNone(tools.fetch_thumbnail(self.cfg, lost), f"a link that is no web address ({link}) costs one picture, not the loading")
        other = tools.node_from_summary({"type": "builtin/z", "title": "Z", "thumbnailUrl": f"{self.server.url}/api/v1/node-type-thumbnails/builtin/z?token=t"})
        self.server.thumbnail_broken = True
        self.assertIsNone(tools.fetch_thumbnail(self.cfg, other), "the same for the thumbnail itself")

    def test_nothing_that_goes_wrong_while_cutting_reaches_the_loader_or_stays_on_disk(self):
        rain, path = self.square()
        os.remove(tools.tile_path(rain))
        self.new_session()
        with mock.patch.object(tools, "crop_png_to_tile", side_effect=RuntimeError("a fault nobody foresaw")):
            self.assertEqual(tools.fetch_thumbnail(self.cfg, rain), path, "the thumbnail is still there to show")
        self.assertEqual(self.names(), [os.path.basename(path)])
        self.new_session()
        real_replace = os.replace

        def full_disk(source, target):
            if target.endswith(".tile.png"):
                raise OSError(28, "No space left on device")
            return real_replace(source, target)

        with mock.patch.object(tools.os, "replace", side_effect=full_disk):
            self.assertEqual(tools.fetch_thumbnail(self.cfg, rain), path)
        self.assertEqual(self.names(), [os.path.basename(path)], "the half-written cut is cleared away")

    def test_two_loaders_fetching_the_same_picture_do_not_share_their_unfinished_files(self):
        import threading

        rain = self.rain()
        self.server.thumbnail_bytes = fake_jpeg(256, 256)
        self.server.thumbnail_png = fake_png(32, 32, 3, lambda x, y: (1, 2, 3))
        real, unfinished = api.download_to, []

        def watched(path, url, **kwargs):
            unfinished.append((threading.get_ident(), path))
            return real(path, url, **kwargs)

        with mock.patch.object(api, "download_to", side_effect=watched):
            other = threading.Thread(target=tools.fetch_thumbnail, args=(self.cfg, rain))
            other.start(); other.join(10)
            self.new_session()
            for name in self.names():
                os.remove(os.path.join(tools.thumbnail_dir(), name))
            tools.fetch_thumbnail(self.cfg, rain)
        by_thread = {}
        for thread, path in unfinished:
            by_thread.setdefault(thread, set()).add(path)
        self.assertEqual(len(by_thread), 2)
        first, second = by_thread.values()
        self.assertEqual((len(first), len(second), first & second), (2, 2, set()), "the thumbnail and its cut, under names of their own")
        self.assertEqual(len(self.names()), 2, "and only the two pictures are left")

    def test_a_server_that_does_not_make_cuts_is_asked_three_times_and_no_more(self):
        self.server.thumbnail_bytes = fake_jpeg(256, 256)
        self.server.thumbnail_png = self.server.thumbnail_bytes  # the stored file, whatever format is asked for
        link = f"{self.server.url}/api/v1/node-type-thumbnails/builtin/n?token=t"
        nodes = [tools.node_from_summary({"type": f"builtin/s{at}", "title": f"S{at}", "thumbnailUrl": link}) for at in range(5)]
        for node in nodes:
            self.assertIsNotNone(tools.fetch_thumbnail(self.cfg, node))
        self.assertEqual(len([entry for entry in self.pictures() if "format=png" in entry["path"]]), 3)

    def test_a_cut_never_outlives_the_thumbnail_it_was_made_from(self):
        link = f"{self.server.url}/api/v1/node-type-thumbnails/builtin/x?token=t"
        node = tools.node_from_summary({"type": "builtin/x", "title": "X", "thumbnailUrl": link})
        self.server.thumbnail_bytes = fake_jpeg(256, 256)
        self.server.thumbnail_png = fake_png(32, 32, 3, lambda x, y: (255, 0, 0))
        path = tools.fetch_thumbnail(self.cfg, node)
        self.assertEqual(tools.thumbnail_tile(node), tools.tile_path(node))
        old = time.time() - tools.THUMBNAIL_KEEP_SECONDS - 60
        # A newer picture arrives, but its cut cannot be fetched: the old cut must not stand in for it.
        os.utime(path, (old, old))
        self.server.thumbnail_bytes = fake_jpeg(256, 256) + b"newer"
        self.server.png_status = 503
        self.new_session()
        tools.fetch_thumbnail(self.cfg, node)
        self.assertIsNone(tools.thumbnail_tile(node), "the icon, not the picture from before")
        self.assertEqual(self.names(), [os.path.basename(path)])
        # Next time the cut is made; then the picture changes to one that needs none.
        self.server.png_status = None
        self.new_session()
        tools.fetch_thumbnail(self.cfg, node)
        self.assertEqual(tools.thumbnail_tile(node), tools.tile_path(node))
        # A week on, in a window left open: the newer picture gets its own cut in this same session.
        os.utime(path, (old, old))
        self.server.thumbnail_png = fake_png(32, 32, 3, lambda x, y: (0, 0, 255))
        self.server.seen.clear()
        tools.fetch_thumbnail(self.cfg, node)
        self.assertEqual(len(self.pictures()), 2, "the thumbnail and its cut, although a cut was made earlier this session")
        with open(tools.tile_path(node), "rb") as fh:
            self.assertEqual(tools._png_rows(fh.read())[3][0][:3], bytes((0, 0, 255)), "the blue one, not the red one from before")
        os.utime(path, (old, old))
        self.server.thumbnail_bytes = fake_jpeg(256, 144)
        self.new_session()
        tools.fetch_thumbnail(self.cfg, node)
        self.assertEqual(tools.thumbnail_tile(node), path)
        self.assertEqual(self.names(), [os.path.basename(path)], "the cut of the square one is gone")

    def test_pictures_are_written_so_that_only_the_user_can_change_them(self):
        before = os.umask(0)  # as inside Resolve
        self.addCleanup(os.umask, before)
        rain, path = self.square()
        for written in (path, tools.tile_path(rain)):
            self.assertEqual(os.stat(written).st_mode & 0o777, 0o644, written)
        for folder in (config.APP_DIR, tools.thumbnail_dir()):
            self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700, "nobody else can swap the files in it")
        os.remove(tools.tile_path(rain))
        self.new_session()
        self.server.png_status = 415
        tools.fetch_thumbnail(self.cfg, rain)
        self.assertEqual(os.stat(tools.no_tile_path(rain)).st_mode & 0o777, 0o600)

    def test_the_thumbnails_of_a_workflow_as_it_was_before_are_removed(self):
        rain, path = self.square()
        directory = tools.thumbnail_dir()
        current = os.path.basename(path)[: -len(".jpg")]
        template = current.rsplit("-", 1)[0]
        # Real template ids have hyphens in them, and one can begin like another.
        from dataclasses import replace

        hyphened = replace(rain, template_id="Ig6rt-NswVka-6RQ", updated_at="2026-01-01T00:00:00Z")
        undated = replace(rain, template_id="Qq-undated", updated_at="")
        mine = [os.path.basename(tools.thumbnail_path(hyphened)), os.path.basename(tools.tile_path(hyphened)), os.path.basename(tools.thumbnail_path(undated))]
        gone = ["Ig6rt-NswVka-6RQ-2024OLD.jpg", "Qq-undated-2024OLD.tile.png"]
        stays = ["Ig6rt-NswVka-2024OLD.jpg", "Ig6rt-2024OLD.jpg"]  # other templates, whose ids this one's begins with
        for name in mine + gone + stays:
            with open(os.path.join(directory, name), "wb") as fh:
                fh.write(b"x")
        self.assertTrue(mine[2].endswith("-0.jpg"))
        self.assertEqual(tools.drop_replaced_thumbnails([hyphened, undated]), len(gone))
        self.assertEqual(sorted(set(self.names()) & set(mine + gone + stays)), sorted(mine + stays))
        for name in mine + stays:
            os.remove(os.path.join(directory, name))
        others = [f"{template}-2024OLD.jpg", f"{template}-2024OLD.tile.png", f"{template}-2023OLDER.tile.none"]
        kept = [f"{template}-more-2024OLD.jpg", "node_builtin_x-0.jpg", "node_builtin_x-0.tile.png", "t9-2024OLD.jpg", f"{template}-2024OLD.txt", "notes"]
        for name in others + kept:
            with open(os.path.join(directory, name), "wb") as fh:
                fh.write(b"x")
        self.assertEqual(tools.drop_replaced_thumbnails([]), 0, "nothing loaded: nothing is judged")
        # Downloads that were cut off leave files under names nothing reuses.
        old = time.time() - 2 * 3600
        for name, age in (("t9-1.jpg.1a2b.fetched", old), ("t9-1.tile.png.1a2b.part", old), ("t9-1.jpg.ffff.fetched", None)):
            with open(os.path.join(directory, name), "wb") as fh:
                fh.write(b"x")
            if age:
                os.utime(os.path.join(directory, name), (age, age))
        self.assertEqual(tools.drop_replaced_thumbnails([]), 2, "the two that are hours old; one being written now is left")
        self.assertIn("t9-1.jpg.ffff.fetched", self.names())
        os.remove(os.path.join(directory, "t9-1.jpg.ffff.fetched"))
        self.assertEqual(tools.drop_replaced_thumbnails([rain, rain]), len(others))
        self.assertEqual(self.names(), sorted(kept + [current + ".jpg", current + ".tile.png"]))

    def test_a_server_that_sends_the_stored_file_is_noticed_and_left_alone(self):
        self.server.renders_thumbnails = False
        link = f"{self.server.url}/api/v1/node-type-thumbnails/builtin/n?token=t"
        nodes = [tools.node_from_summary({"type": f"builtin/n{at}", "title": f"N{at}", "thumbnailUrl": link}) for at in range(4)]
        self.assertIsNone(tools.fetch_thumbnail(self.cfg, nodes[0]), "a stored PNG or WebP is not kept as the thumbnail")
        self.server.seen.clear()
        self.assertIsNone(tools.fetch_thumbnail(self.cfg, nodes[0]))
        self.assertEqual(self.server.seen, [], "what was refused is not downloaded again this session")
        # A small JPEG now and then does not hide what kind of server this is.
        self.server.stored_thumbnail = self.server.thumbnail_bytes
        self.assertIsNotNone(tools.fetch_thumbnail(self.cfg, nodes[1]))
        self.server.stored_thumbnail = b"\x89PNG\r\n\x1a\n" + b"P" * 4000
        self.assertFalse(tools.renditions_unavailable())
        self.assertIsNone(tools.fetch_thumbnail(self.cfg, nodes[2]))
        self.assertIsNone(tools.fetch_thumbnail(self.cfg, nodes[3]))
        self.assertTrue(tools.renditions_unavailable(), "after a few, the rest are not asked for")
        self.assertEqual(len(os.listdir(tools.thumbnail_dir())), 1)

    def test_a_node_thumbnail_is_looked_at_again_after_a_week_and_kept_if_that_fails(self):
        node = tools.node_from_summary({"type": "builtin/x", "title": "X", "thumbnailUrl": f"{self.server.url}/api/v1/node-type-thumbnails/builtin/x?token=t"})
        path = tools.fetch_thumbnail(self.cfg, node)
        self.server.seen.clear()
        self.assertEqual(tools.fetch_thumbnail(self.cfg, node), path)
        self.assertEqual(self.server.seen, [], "fresh enough: not fetched again")
        old = time.time() - tools.THUMBNAIL_KEEP_SECONDS - 60
        os.utime(path, (old, old))
        self.server.thumbnail_bytes = fake_jpeg(256, 144) + b"N"
        self.assertEqual(tools.fetch_thumbnail(self.cfg, node), path)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), self.server.thumbnail_bytes, "the newer picture replaced it")
        os.utime(path, (old, old))
        self.server.renders_thumbnails = False
        self.assertEqual(tools.fetch_thumbnail(self.cfg, node), path, "when the refresh fails, the picture it has is still shown")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), self.server.thumbnail_bytes)

    def test_a_stored_file_too_big_to_be_a_thumbnail_is_not_downloaded_whole(self):
        self.server.renders_thumbnails = False
        self.server.stored_thumbnail = b"\xff\xd8\xff" + b"J" * (tools.THUMBNAIL_MAX_BYTES + 10)  # a JPEG, but the full-size one
        self.assertIsNone(tools.fetch_thumbnail(self.cfg, self.rain()))
        self.assertEqual(os.listdir(tools.thumbnail_dir()), [])
        with self.assertRaises(api.KaitoiAPIError) as caught:
            api.download_to(self.temp_file("big.bin", b""), f"{self.server.url}/api/v1/template-thumbnails/t1?token=x", max_bytes=100)
        self.assertEqual(caught.exception.code, "TOO_LARGE")

    def test_a_node_keeps_its_thumbnail_link_and_a_tool_without_one_fetches_nothing(self):
        node = tools.node_from_summary({"type": "builtin/x", "title": "X", "thumbnailUrl": f"{self.server.url}/api/v1/node-type-thumbnails/builtin/x?token=t", "inputs": [], "outputs": []})
        self.assertTrue(node.ready)
        self.assertTrue(tools.fetch_thumbnail(self.cfg, node).endswith(".jpg"))
        bare = tools.node_from_summary({"type": "builtin/y", "title": "Y"})
        self.server.seen.clear()
        self.assertIsNone(tools.fetch_thumbnail(self.cfg, bare))
        self.assertEqual(self.server.seen, [])


class InstallMany(Base):
    def setUp(self):
        super().setUp()
        root = mock.patch.object(templates, "templates_root", lambda: os.path.join(self.dir.name, "Templates"))
        root.start(); self.addCleanup(root.stop)

    def unloaded(self):
        self.server.lists_pins = False
        return sorted(tools.load_node_catalog(self.cfg), key=lambda node: node.name)

    def test_it_loads_and_installs_each_node_and_reports(self):
        nodes = self.unloaded()
        gone = tools.node_from_summary({"type": "builtin/none", "title": "Gone"})
        self.server.rate_limited = 1
        progress, waits = {}, []
        with mock.patch.object(tools.time, "sleep", waits.append):
            templates.install_many(self.cfg, [nodes[0], gone, nodes[1]], progress, threading.Event(), pause=0)
        self.assertEqual((progress["done"], progress["failed"], progress["finished"], progress["waiting"]), (3, 1, True, 0))
        self.assertEqual([tool.name for tool in progress["loaded"]], ["Sparkle", "Video Reverse & Speed"])
        self.assertTrue(all(tool.ready for tool in progress["loaded"]))
        self.assertEqual(sorted(templates.installed()), ["node:builtin/video/video_reverse", "node:community/acme/sparkle"])
        self.assertEqual(round(sum(waits)), 7, "it waited out the one refusal instead of failing that node")

    def test_a_node_that_already_has_its_pins_costs_no_request(self):
        ready = list(tools.load_node_catalog(self.cfg))
        self.server.seen.clear()
        progress = {}
        templates.install_many(self.cfg, ready, progress, threading.Event(), pause=30)  # a pause only follows a lookup
        self.assertEqual((progress["done"], progress["failed"]), (2, 0))
        self.assertEqual(self.server.seen, [])

    def test_stop_ends_it_between_nodes(self):
        nodes = self.unloaded()
        stop = threading.Event()
        progress = {}
        original = templates.install

        def install_then_stop(tool, **kwargs):
            stop.set()
            return original(tool, **kwargs)

        with mock.patch.object(templates, "install", install_then_stop):
            templates.install_many(self.cfg, nodes, progress, stop, pause=0)
        self.assertEqual((progress["done"], progress["finished"]), (1, True))
        self.assertEqual(len(templates.installed()), 1)


class RateLimit(Base):
    NODE = {"type": "builtin/video/video_reverse", "title": "Video Reverse & Speed"}

    def test_a_refused_lookup_waits_as_long_as_asked_then_succeeds(self):
        self.server.rate_limited = 2
        slept, told = [], []
        node = tools.load_node_patiently(self.cfg, tools.node_from_summary(self.NODE), sleep=slept.append, on_wait=told.append)
        self.assertTrue(node.ready)
        self.assertEqual(round(sum(slept)), 14, "two refusals, seven seconds asked each time")
        self.assertEqual(told[0], 7)
        self.assertEqual(self.server.rate_limited, 0)

    def test_stopping_during_a_wait_gives_up_without_another_request(self):
        self.server.rate_limited = 5
        slept = []
        node = tools.load_node_patiently(self.cfg, tools.node_from_summary(self.NODE), sleep=slept.append, should_stop=lambda: len(slept) >= 3)
        self.assertIsNone(node)
        self.assertEqual(self.server.rate_limited, 4, "one request was refused; none followed the stop")

    def test_it_gives_up_when_the_server_keeps_refusing(self):
        self.server.rate_limited = 99
        with self.assertRaises(api.KaitoiAPIError) as caught:
            tools.load_node_patiently(self.cfg, tools.node_from_summary(self.NODE), sleep=lambda _: None, attempts=3)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.server.rate_limited, 96)

    def test_the_wait_is_what_was_asked_rounded_up_within_reason(self):
        self.assertEqual(tools._retry_wait({"retryAfterSeconds": 7.2}), 8, "never retry before the server said")
        self.assertEqual(tools._retry_wait({"retryAfterSeconds": 0}), 1, "now means now, not the fallback wait")
        self.assertEqual(tools._retry_wait({"retryAfterSeconds": 100000}), tools.RATE_LIMIT_LONGEST_WAIT_SECONDS)
        for unknown in (None, {}, {"retryAfterSeconds": "soon"}, {"retryAfterSeconds": True}):
            self.assertEqual(tools._retry_wait(unknown), tools.RATE_LIMIT_WAIT_SECONDS)

    def test_another_error_is_not_retried(self):
        with self.assertRaises(api.KaitoiAPIError) as caught:
            tools.load_node_patiently(self.cfg, tools.node_from_summary({"type": "builtin/none", "title": "None"}), sleep=self.fail)
        self.assertEqual(caught.exception.status, 404)


class NodeGraphs(Base):
    def test_a_loader_in_front_of_each_file_and_typed_values(self):
        node = tools.load_node(self.cfg, tools.node_from_summary({"type": "builtin/video/video_reverse", "title": "Video Reverse & Speed"}))
        built = graphs.build_node_graph(self.cfg, node, {"speedFactor": 2.0, "keepAudio": None}, {"inputVideo": "file-1"})
        by_id = {n["id"]: n for n in built["graph"]["nodes"]}
        self.assertEqual(by_id["load_inputVideo"]["type"], "builtin/loaders/load_video")
        self.assertEqual(built["graph"]["connections"], [{"from": ["load_inputVideo", "outputVideo"], "to": ["node", "inputVideo"]}])
        self.assertEqual(built["overrides"], {"load_inputVideo": {"inputVideo": {"type": "file", "fileId": "file-1"}}})
        inputs = by_id["node"]["inputs"]
        self.assertEqual(inputs["speedFactor"], {"type": "number", "value": 2.0})
        self.assertEqual(inputs["keepAudio"], {"type": "boolean", "value": True}, "a value left alone takes the catalog default")
        self.assertEqual(inputs["model"], {"type": "null", "value": None}, "every declared pin is present, or the node fails on a missing key")
        self.assertNotIn("inputVideo", inputs)

    def test_a_node_with_no_file_needs_no_loader(self):
        node = tools.load_node(self.cfg, tools.node_from_summary({"type": "community/acme/sparkle", "title": "Sparkle"}))
        built = graphs.build_node_graph(self.cfg, node, {"prompt": "hi"}, {})
        self.assertEqual([n["id"] for n in built["graph"]["nodes"]], ["node"])
        self.assertEqual(built["overrides"], {})


class Jobs(Base):
    """The job with nothing from the timeline: upload what was picked, run, download."""

    def job(self, tool, **extra):
        settings = dict(config.DEFAULTS, api_key="key-123", base_url=self.server.url, work_dir=self.dir.name, poll_interval_seconds=0.05, run_timeout_seconds=5)
        source = {"origin": "empty", "start": 86400, "end": 86400, "duration": 0, "playhead": 86400, "name": "Nothing selected", "timeline_name": "T"}
        with mock.patch.object(config, "work_dir", lambda: self.dir.name):
            job = pipeline.Job(None, source, tool, settings=settings, **extra)
            job.start()
            deadline = time.time() + 10
            while job.running and time.time() < deadline:
                time.sleep(0.02)
        return job

    def workflow(self):
        found, _ = tools.load_workflow_tools(self.cfg)
        return next(t for t in found if t.name == "Rain")

    def test_a_workflow_run_end_to_end(self):
        clip = self.temp_file("picked.mp4")
        job = self.job(self.workflow(), values={"prompt": "storm"}, files={"video": clip})
        self.assertIsNone(job.error)
        submit = next(entry for entry in self.server.seen if entry["method"] == "POST" and entry["path"].endswith("/runs"))
        self.assertEqual(submit["json"]["inputs"], {"video": {"type": "file", "fileId": "file-picked.mp4"}, "prompt": "storm"})
        self.assertEqual([r["label"] for r in job.results], ["Result"])
        with open(job.results[0]["path"], "rb") as fh:
            self.assertEqual(fh.read(), b"RESULT-BYTES")
        self.assertEqual(job.texts, {"note": "ok"})
        self.assertEqual(job.record_frame, 86400)

    def test_a_failed_run_says_why(self):
        self.server.run_status = "failed"
        job = self.job(self.workflow(), values={"prompt": "storm"}, files={})
        self.assertIn("boom", job.error)
        self.assertEqual(job.results, [])

    def test_stopping_a_workflow_run_is_honest_about_what_it_can_do(self):
        self.server.polls_before_done = 10_000
        settings = dict(config.DEFAULTS, api_key="key-123", base_url=self.server.url, work_dir=self.dir.name, poll_interval_seconds=0.05)
        source = {"origin": "empty", "start": 0, "end": 0, "duration": 0, "playhead": 0, "name": "", "timeline_name": ""}
        job = pipeline.Job(None, source, self.workflow(), settings=settings)
        job.start()
        deadline = time.time() + 5
        while job.run_id is None and time.time() < deadline:
            time.sleep(0.02)
        job.cancel()
        while job.running and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(job.error, pipeline.NO_CANCEL_NOTE)
        self.assertFalse(any(path.endswith("/cancel") for path in self.paths()))

    def test_a_node_run_end_to_end_and_its_cancel_reaches_the_server(self):
        node = tools.load_node(self.cfg, tools.node_from_summary({"type": "builtin/video/video_reverse", "title": "Video Reverse & Speed"}))
        job = self.job(node, values={"speedFactor": 2.0}, files={"inputVideo": self.temp_file("picked.mp4")})
        self.assertIsNone(job.error)
        submit = next(entry for entry in self.server.seen if entry["method"] == "POST" and entry["path"] == "/api/v1/runs")
        self.assertEqual(submit["json"]["targetNodeIds"], ["node"])
        self.assertIn("load_inputVideo", submit["json"]["inputOverrides"])
        self.assertEqual([r["label"] for r in job.results], ["Output Video"])

        self.server.seen.clear(); self.server._polls.clear(); self.server.polls_before_done = 10_000
        settings = dict(config.DEFAULTS, api_key="key-123", base_url=self.server.url, work_dir=self.dir.name, poll_interval_seconds=0.05)
        source = {"origin": "empty", "start": 0, "end": 0, "duration": 0, "playhead": 0, "name": "", "timeline_name": ""}
        job = pipeline.Job(None, source, node, values={}, files={"inputVideo": self.temp_file("again.mp4")}, settings=settings)
        job.start()
        deadline = time.time() + 5
        while job.run_id is None and time.time() < deadline:
            time.sleep(0.02)
        job.cancel()
        while job.running and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(job.error, "Cancelled.")
        self.assertTrue(any(path.endswith("/cancel") for path in self.paths("POST")))

    def events(self, *pairs):
        return [{"id": f"e{at}", "runId": "r", "type": kind, "nodeId": node, "progress": progress, "message": message}
                for at, (kind, node, progress, message) in enumerate(pairs)]

    def test_how_far_a_run_has_got_is_read_from_its_events(self):
        self.server.run_events = self.events(
            ("run.running", None, None, "Run started."), ("node.progress", "load_inputVideo", 1.0, ""), ("node.progress", "node", 0.0, ""),
            ("run.log", None, None, "Running script: x.py"), ("node.log", "node", None, " Developing your film... "), ("node.progress", "node", 0.47, ""),
        )
        def far(*args, **more):
            seen = api.run_progress(self.cfg, *args, **more)
            return {"fraction": seen["fraction"], "message": seen["message"]}

        self.assertEqual(far("run_1", "node"), {"fraction": 0.47, "message": "Developing your film..."},
                         "the node that was asked for, not the loader in front of it; and what the node said, not the server's own log")
        self.assertEqual(far("run_1", "load_inputVideo"), {"fraction": 1.0, "message": ""})
        self.assertEqual(api.run_progress(self.cfg, "run_1")["fraction"], 0.47, "with no node named: the last report of any")
        self.server.run_events = self.events(("node.log", "node", None, "Developing your film..."), ("run.log", None, None, "Running script: x.py"), ("node.started", "node", 0.0, "Node started."))
        self.assertEqual(api.run_progress(self.cfg, "run_1")["message"], "Developing your film...", "what a node said: never the server's own log, nor its lifecycle lines")
        self.server.run_events = self.events(("node.progress", "node", 7, ""))
        self.assertEqual(api.run_progress(self.cfg, "run_1", "node")["fraction"], 1.0, "held between 0 and 1")
        self.server.run_events = self.events(("node.progress", "node", True, ""), ("node.progress", "node", "half", ""), ("node.started", "node", 0.9, ""))
        self.assertEqual(far("run_1", "node"), {"fraction": None, "message": ""}, "only a number on a progress event counts")
        self.server.run_events = []
        self.assertEqual(far("run_1", "node"), {"fraction": None, "message": ""})
        self.server.run_events = None
        with self.assertRaises(api.KaitoiAPIError):
            api.run_progress(self.cfg, "run_1", "node")

    def running_node_job(self):
        node = tools.load_node(self.cfg, tools.node_from_summary({"type": "builtin/video/video_reverse", "title": "Video Reverse & Speed"}))
        self.server.seen.clear(); self.server._polls.clear(); self.server.polls_before_done = 10_000
        settings = dict(config.DEFAULTS, api_key="key-123", base_url=self.server.url, work_dir=self.dir.name, poll_interval_seconds=0.05)
        source = {"origin": "empty", "start": 0, "end": 0, "duration": 0, "playhead": 0, "name": "", "timeline_name": ""}
        job = pipeline.Job(None, source, node, values={}, files={"inputVideo": self.temp_file("progress.mp4")}, settings=settings)
        job.start()

        def stop():  # before the test server goes: a job left polling would meet a closed door
            job.cancel()
            deadline = time.time() + 5
            while job.running and time.time() < deadline:
                time.sleep(0.02)

        self.addCleanup(stop)
        return job

    def wait(self, test, seconds=5):
        deadline = time.time() + seconds
        while not test() and time.time() < deadline:
            time.sleep(0.02)
        return test()

    def test_a_node_run_reports_how_far_it_is_and_what_the_node_says(self):
        self.server.run_events = self.events(("node.progress", "load_inputVideo", 1.0, ""), ("node.log", "node", None, "Reversing"), ("node.progress", "node", 0.4, ""))
        job = self.running_node_job()
        self.assertTrue(self.wait(lambda: job.poll()["fraction"] == 0.4), job.poll())
        seen = job.poll()
        self.assertEqual(seen["stage"], 3)
        self.assertIn("Reversing 40%", seen["status"])
        self.server.run_events = self.events(("node.progress", "node", 0.9, ""))
        self.assertTrue(self.wait(lambda: job.poll()["fraction"] == 0.9), "it follows the run as it goes")
        self.assertIn("Reversing 90%", job.poll()["status"], "what the node last said stays until it says something else")
        self.server.run_events = self.events(("node.log", "node", None, "Writing the file"), ("node.progress", "node", 0.95, ""))
        self.assertTrue(self.wait(lambda: "Writing the file 95%" in job.poll()["status"]), "the last thing it said, not the first")

    def test_with_nothing_said_by_the_node_the_status_says_where_the_run_is(self):
        self.server.run_events = self.events(("node.progress", "node", 0.9, ""))
        job = self.running_node_job()
        self.assertTrue(self.wait(lambda: job.poll()["fraction"] == 0.9))
        self.assertIn("running 90%", job.poll()["status"])

    def test_a_long_run_is_read_on_from_where_the_last_poll_stopped(self):
        """Not again from its first event at every poll: that is a request for every 200 events a run has ever had, every three seconds."""
        self.server.events_page = 2
        self.server.run_events = self.events(*[("node.progress", "node", at / 10, "") for at in range(1, 8)])  # seven events, in pages of two
        self.server.seen.clear()
        first = api.run_progress(self.cfg, "run_1", "node")
        self.assertEqual((first["fraction"], first["cursor"]), (0.7, "6"), "read to the end, and the place of the last page kept")
        self.assertEqual(len(self.paths()), 4)
        self.server.run_events += self.events(("node.progress", "node", 0.8, ""))
        self.server.seen.clear()
        second = api.run_progress(self.cfg, "run_1", "node", first)
        self.assertEqual(second["fraction"], 0.8)
        self.assertEqual([entry["path"].split("cursor=")[-1] for entry in self.server.seen], ["6"], "one request: the page it stopped at, for what was added")
        self.server.seen.clear()
        self.assertEqual(api.run_progress(self.cfg, "run_1", "node", second)["fraction"], 0.8, "nothing new: what was known stays")
        self.assertEqual(len(self.paths()), 1)

    def test_a_run_far_ahead_is_caught_up_with_a_few_pages_at_a_poll(self):
        self.server.events_page = 1
        self.server.run_events = self.events(*[("node.progress", "node", at / 100, "") for at in range(1, 21)])
        self.server.seen.clear()
        first = api.run_progress(self.cfg, "run_1", "node")
        self.assertEqual(len(self.paths()), api.PROGRESS_PAGES, "no more than this at one poll, however many events there are")
        self.assertEqual(first["fraction"], api.PROGRESS_PAGES / 100)
        second = api.run_progress(self.cfg, "run_1", "node", first)
        self.assertEqual(second["fraction"], 2 * api.PROGRESS_PAGES / 100, "and on from there at the next")
        self.server.seen.clear()
        api.run_progress(self.cfg, "run_1", "node", second, should_stop=lambda: True)
        self.assertEqual(self.paths(), [], "a run being cancelled is not read at all")

    def test_a_job_reads_on_from_poll_to_poll(self):
        self.server.events_page = 1
        self.server.run_events = self.events(*[("node.progress", "node", at / 100, "") for at in range(1, 13)])  # more than one poll reads
        job = self.running_node_job()
        self.assertTrue(self.wait(lambda: job.poll()["fraction"] == 0.12), f"stuck at {job.poll()['fraction']}: each poll began again at the first event")

    def test_what_a_node_says_is_shown_as_one_short_plain_line(self):
        self.server.run_events = self.events(("node.log", "node", None, "\n  Developing <b>your</b> film\nsecond line\n"))
        self.assertEqual(api.run_progress(self.cfg, "run_1", "node")["message"], "Developing b your /b film", "its first line, and nothing the window would read as markup")
        self.server.run_events = self.events(("node.log", "node", None, "word " * 200))
        said = api.run_progress(self.cfg, "run_1", "node")["message"]
        self.assertLessEqual(len(said), api.SAID_LETTERS)
        self.assertTrue(said.endswith("…"))
        self.server.run_events = self.events(("node.log", "node", None, "Kept"), ("node.log", "node", None, "  \n \n"))
        self.assertEqual(api.run_progress(self.cfg, "run_1", "node")["message"], "Kept", "an empty line said later does not wipe it")

    def test_whatever_goes_wrong_reading_the_progress_the_run_goes_on(self):
        for fault in ("drop", "junk"):
            with self.subTest(fault):
                self.server.events_fault = fault
                self.server.run_events = self.events(("node.progress", "node", 0.4, ""))
                job = self.running_node_job()
                self.assertTrue(self.wait(lambda: sum("/events" in entry["path"] for entry in self.server.seen) >= 3, seconds=8), "it keeps asking")
                seen = job.poll()
                self.assertTrue(seen["running"], seen)
                self.assertIsNone(seen["error"])
                self.assertFalse(any(path.endswith("/cancel") for path in self.paths("POST")))
                self.server.events_fault = ""
                self.assertTrue(self.wait(lambda: job.poll()["fraction"] == 0.4, seconds=8), "and shows it again when it can be read")
                self.server.events_fault = fault
                time.sleep(0.2)
                self.assertEqual(job.poll()["fraction"], 0.4, "a read that fails keeps what was last known: the bar does not jump back")
                job.cancel()
                self.wait(lambda: not job.running)

    def test_a_refused_place_in_the_events_is_let_go_and_reading_begins_again(self):
        self.server.events_page = 2
        self.server.run_events = self.events(*[("node.progress", "node", at / 10, "") for at in range(1, 6)])
        job = self.running_node_job()
        self.assertTrue(self.wait(lambda: job.poll()["fraction"] == 0.5))
        self.server.events_key = "k2:"  # the server's signing key changed in the middle of a run: the place the job holds is refused
        self.server.run_events = self.events(*[("node.progress", "node", at / 10, "") for at in range(1, 8)])
        self.assertTrue(self.wait(lambda: job.poll()["fraction"] == 0.7, seconds=8), f"frozen at {job.poll()['fraction']}: the refused place was asked for again and again")

    def test_a_slow_answer_about_progress_is_not_waited_for_long_and_not_asked_for_at_every_poll(self):
        self.assertLessEqual(pipeline.PROGRESS_TIMEOUT_SECONDS, 10)
        self.server.events_fault = "junk"
        self.server.run_events = self.events(("node.progress", "node", 0.4, ""))
        self.running_node_job()
        self.assertTrue(self.wait(lambda: sum("/api/v1/runs/" in entry["path"] and "/events" not in entry["path"] for entry in self.server.seen) >= 9))
        status_polls = sum("/api/v1/runs/" in entry["path"] and "/events" not in entry["path"] and entry["method"] == "GET" for entry in self.server.seen)
        event_reads = sum("/events" in entry["path"] for entry in self.server.seen)
        self.assertLessEqual(event_reads, status_polls // (pipeline.PROGRESS_REST_POLLS + 1) + 1, "after a read that failed, a few polls pass before the next")
        self.assertGreaterEqual(event_reads, 2, "but it is asked for again")

    def test_progress_is_asked_for_with_little_patience(self):
        asked = []

        def run_progress(config, run_id, node_id="", seen=None, should_stop=None):
            asked.append(config.timeout)
            return {"fraction": 0.3, "message": "", "cursor": None}

        with mock.patch.object(api, "run_progress", side_effect=run_progress):
            job = self.running_node_job()
            self.assertTrue(self.wait(lambda: len(asked) >= 2))
            job.cancel()
            self.wait(lambda: not job.running)
        self.assertEqual(set(asked), {pipeline.PROGRESS_TIMEOUT_SECONDS}, "not the two minutes other requests are given: a slow answer here would hold up noticing the run has ended")

    def test_the_events_are_not_asked_for_once_the_run_has_ended(self):
        node = tools.load_node(self.cfg, tools.node_from_summary({"type": "builtin/video/video_reverse", "title": "Video Reverse & Speed"}))
        self.server.seen.clear(); self.server._polls.clear(); self.server.polls_before_done = 3
        job = self.job(node, values={}, files={"inputVideo": self.temp_file("ended.mp4")})
        self.assertIsNone(job.error)
        polls = [entry["path"] for entry in self.server.seen if entry["method"] == "GET" and "/api/v1/runs/" in entry["path"]]
        self.assertEqual(sum("/events" in path for path in polls), 2, "once for each poll that found it running, none for the one that found it done")

    def test_a_server_that_cannot_say_how_far_a_run_is_does_not_fail_it(self):
        self.server.run_events = None
        job = self.running_node_job()
        self.assertTrue(self.wait(lambda: any("/events" in entry["path"] for entry in self.server.seen)))
        time.sleep(0.2)
        seen = job.poll()
        self.assertTrue(seen["running"])
        self.assertIsNone(seen["error"])
        self.assertIsNone(seen["fraction"])
        self.assertIn("running", seen["status"])

    def test_the_progress_of_one_step_is_not_carried_into_the_next(self):
        job = pipeline.Job(None, {"origin": "empty"}, self.workflow(), settings=dict(config.DEFAULTS))
        job._set_stage(3, "submitting")
        job._set_fraction(0.8)
        self.assertEqual(job.poll()["fraction"], 0.8)
        job._set_stage(4, "fetching")
        self.assertIsNone(job.poll()["fraction"])

    def test_a_workflow_run_reports_the_percent_kaitoi_gives(self):
        job = pipeline.Job(None, {"origin": "empty"}, self.workflow(), settings=dict(config.DEFAULTS))
        states = iter([{"status": "running", "progress": 35, "progressMessage": "Rendering"}, {"status": "running", "progress": None}, {"status": "succeeded", "outputs": {}}])
        seen = []

        def wait_for(fetch, *, on_progress, **_):
            for state in states:
                on_progress(state)
                seen.append((job.poll()["fraction"], job.poll()["status"]))
            return {"status": "succeeded", "outputs": {}}

        with mock.patch.object(api, "run_endpoint", return_value={"taskId": "t1"}), mock.patch.object(api, "wait_for", side_effect=wait_for), \
                mock.patch.object(api, "output_files", return_value=[]), mock.patch.object(api, "output_texts", return_value={}):
            job._run_workflow(self.cfg, {}, "x")
        self.assertEqual(seen[0][0], 0.35)
        self.assertIn("Rendering 35%", seen[0][1])
        self.assertIsNone(seen[1][0], "no percent given: none shown")


if __name__ == "__main__":
    unittest.main()
