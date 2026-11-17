"""The window's own decisions, driven without Resolve (see fake_panel)."""

from __future__ import annotations

from . import HOME  # noqa: F401 - first: outside the tests package, where home is not redirected, this file does not load

import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from kaitoi_resolve import api, busy, config, tools, ui, uninstall

from .fake_kaitoi import fake_jpeg
from .fake_panel import FinishedLoader, Tree, Widget, boxes, panel, row_of

LISTS = {"inputs": {
    "voice": {"options": [{"value": "v1", "label": "Aria"}, {"value": "v2", "label": "Blade"}], "value": "v1"},
    "greeting": {"options": None, "value": "Good morning"},
}}
OTHER_ACCOUNT = {"inputs": {"voice": {"options": [{"value": "o1", "label": "Someone else's voice"}], "value": "o1"}}}


def speak() -> tools.Tool:
    """A node that fills in its voices and a sample greeting when it starts."""
    return tools.node_from_detail({"type": "builtin/voice/speak", "title": "Speak", "category": "Audio", "inputs": [
        {"name": "voice", "type": "string", "dynamic": True, "default": "selectedVoice.value"},
        {"name": "greeting", "type": "string", "dynamic": True},
        {"name": "speed", "type": "string"},
    ], "outputs": [{"name": "audio", "type": "audio"}]})


class Base(unittest.TestCase):
    def setUp(self):
        quiet = mock.patch.object(config, "log")
        quiet.start(); self.addCleanup(quiet.stop)

    def open_form(self, made, tool):
        made.tool = tool
        made.view = ui.VIEW_RUN
        made._show_run()

    def arrive(self, made, tool_id, lists=LISTS):
        """The node's lists come back from Kaitoi."""
        made._state_tried.add(tool_id)
        made._state_fetch[tool_id] = {"state": lists, "error": None, "account": made._state_account}
        made._collect_states()

    def voice_options(self, tool):
        return next(field for field in tool.inputs if field.name == "voice").options


class RememberedChoice(Base):
    SAVED = {"voice": "Blade", "greeting": "", "speed": "", ui.PICKED: {"voice": 2}}

    def with_saved(self):
        node = speak()
        return node, panel(settings={"last_values": {node.id: dict(self.SAVED)}}, node_tools=[node])

    def test_before_the_list_is_there_the_entry_is_neither_shown_nor_sent(self):
        node, made = self.with_saved()
        self.open_form(made, node)
        self.assertEqual(boxes(made)["voice"][:2], ("text", ""), "the words on a picker entry are not a value to type")
        self.assertEqual(made._form_text(to_send=True)["voice"], "")
        self.assertEqual(made._form_text(to_send=True)[ui.PICKED], {})

    def test_it_is_put_back_on_the_picker_when_the_list_arrives(self):
        node, made = self.with_saved()
        self.open_form(made, node)
        self.arrive(made, node.id)
        self.assertEqual(boxes(made)["voice"], ("choice", "Blade", 2))

    def test_it_is_still_remembered_after_a_visit_that_ended_before_the_list_came(self):
        node, made = self.with_saved()
        self.open_form(made, node)
        made._remember_values()
        kept = made.settings["last_values"][node.id]
        self.assertEqual((kept["voice"], kept[ui.PICKED]), ("Blade", {"voice": 2}))
        made._show_run()  # the form is drawn again, still without the list
        self.arrive(made, node.id)
        self.assertEqual(boxes(made)["voice"], ("choice", "Blade", 2))

    def test_what_the_user_types_over_it_wins(self):
        node, made = self.with_saved()
        self.open_form(made, node)
        made._row("RowEdit", row_of(made, "voice")).Text = "v1"
        self.assertEqual(made._form_text()["voice"], "v1")
        self.assertNotIn("voice", made._form_text()[ui.PICKED])
        self.assertEqual(made._form_text(to_send=True)["voice"], "v1")
        self.arrive(made, node.id)
        self.assertEqual(boxes(made)["voice"], ("choice", "Aria", 1), "typed as the option's value: that option")

    def test_a_run_started_before_the_list_came_sends_the_nodes_own_value_not_the_entry(self):
        node, made = self.with_saved()
        self.open_form(made, node)
        made._row("RowEdit", row_of(made, "speed")).Text = "1.5"
        values, files = made._read_form(node, None)
        self.assertEqual((values["voice"], files), (None, {}), "\"Blade\" is the name on an entry; sent, it would be no voice at all")
        self.assertEqual(values["speed"], "1.5")
        self.arrive(made, node.id)
        self.assertEqual(made._read_form(made.tool, None)[0]["voice"], "v2", "once the picker is there, the pick is sent as its value")

    def test_a_pick_is_held_behind_a_number_box_or_a_slider_too(self):
        steps = [{"value": 20, "label": "Draft (20 steps)"}, {"value": 40, "label": "High (40 steps)"}]
        for name, extra in (("a number box", {}), ("a slider", {"min": 1, "max": 100, "step": 1})):
            with self.subTest(name):
                node = tools.node_from_detail({"type": "builtin/image/make", "title": "Make", "category": "Image", "inputs": [
                    {"name": "quality", "type": "int", "dynamic": True, **extra}], "outputs": [{"name": "image", "type": "image"}]})
                saved = {"quality": "High (40 steps)", ui.PICKED: {"quality": 2}}
                made = panel(settings={"last_values": {node.id: saved}}, node_tools=[node])
                self.open_form(made, node)
                self.assertEqual(boxes(made)["quality"][1], "", "not typed into a box that takes a number")
                self.assertIsNone(made._read_form(node, None)[0]["quality"], "and a run does not stop on it")
                made._remember_values()
                self.assertEqual(made.settings["last_values"][node.id]["quality"], "High (40 steps)")
                self.arrive(made, node.id, {"inputs": {"quality": {"options": steps, "value": 20}}})
                self.assertEqual(boxes(made)["quality"], ("choice", "High (40 steps)", 2))

    def test_the_large_box_standing_in_for_a_picker_holds_a_pick_too(self):
        # An input the node lists itself, named like a prompt, is drawn as the large box until its list comes.
        styles = [{"value": "s1", "label": "Plain white"}, {"value": "s2", "label": "Bold yellow"}]
        node = tools.node_from_detail({"type": "builtin/video/subtitle", "title": "Subtitle", "category": "Video to Video", "inputs": [
            {"name": "caption", "type": "string", "dynamic": True}], "outputs": [{"name": "video", "type": "video"}]})
        saved = {"caption": "Bold yellow", ui.PICKED: {"caption": 2}}
        made = panel(settings={"last_values": {node.id: saved}}, node_tools=[node])
        self.open_form(made, node)
        self.assertIsNotNone(made._prompt_field, "this test is about the large box")
        self.assertEqual(made.items["Prompt"].PlainText, "")
        self.assertIsNone(made._read_form(node, None)[0]["caption"], "the name of an entry is not sent as the caption")
        self.assertEqual(made._form_text()["caption"], "Bold yellow")
        self.assertIn("comes back when its list has loaded", str(made._shown.get(("Status", "Text"), "")), "and the window says a choice is waiting")
        self.arrive(made, node.id, {"inputs": {"caption": {"options": styles, "value": "s1"}}})
        self.assertEqual(boxes(made)["caption"], ("choice", "Bold yellow", 2))

    def test_a_held_pick_is_not_pushed_out_by_the_nodes_own_sample(self):
        # The node shows a sample in the box until its list comes. A choice remembered from the picker outranks it.
        for name in ("voice", "caption"):  # a row's box, and the large box
            with self.subTest(name):
                node = tools.node_from_detail({"type": "builtin/voice/say", "title": "Say", "category": "Audio", "inputs": [
                    {"name": name, "type": "string", "dynamic": True, "default": "A sample"}], "outputs": [{"name": "audio", "type": "audio"}]})
                saved = {name: "Blade", ui.PICKED: {name: 2}}
                made = panel(settings={"last_values": {node.id: saved}}, node_tools=[node])
                self.open_form(made, node)
                shown = made.items["Prompt"].PlainText if made._prompt_field is not None else boxes(made)[name][1]
                self.assertEqual(shown, "", "not the sample: it would be remembered in place of the choice")
                self.assertEqual((made._form_text()[name], made._form_text()[ui.PICKED]), ("Blade", {name: 2}))
                self.arrive(made, node.id, {"inputs": {name: {"options": [{"value": "v1", "label": "Aria"}, {"value": "v2", "label": "Blade"}], "value": "v1"}}})
                self.assertEqual(boxes(made)[name], ("choice", "Blade", 2))
        # With nothing remembered, the sample is shown as before.
        node = tools.node_from_detail({"type": "builtin/voice/say", "title": "Say", "category": "Audio", "inputs": [
            {"name": "voice", "type": "string", "dynamic": True, "default": "A sample"}], "outputs": [{"name": "audio", "type": "audio"}]})
        made = panel(node_tools=[node])
        self.open_form(made, node)
        self.assertEqual(boxes(made)["voice"][1], "A sample")

    def test_when_the_node_answers_without_a_list_the_pick_is_let_go_and_its_sample_is_shown(self):
        # The node has stopped listing this input. No list is coming for the pick to go back on.
        for name in ("voice", "caption"):  # a row's box, and the large box
            with self.subTest(name):
                node = tools.node_from_detail({"type": "builtin/voice/say", "title": "Say", "category": "Audio", "inputs": [
                    {"name": name, "type": "string", "dynamic": True}], "outputs": [{"name": "audio", "type": "audio"}]})
                saved = {name: "Blade", ui.PICKED: {name: 2}}
                answer = {"inputs": {name: {"options": None, "value": "A sample"}}}

                def shown(made):
                    return made.items["Prompt"].PlainText if made._prompt_field is not None else boxes(made)[name][1]

                made = panel(settings={"last_values": {node.id: dict(saved)}}, node_tools=[node])
                self.open_form(made, node)
                self.assertEqual(made._held_picks, {name: ("Blade", 2)}, "held while the answer is still on its way")
                self.arrive(made, node.id, answer)
                self.assertEqual(made._held_picks, {})
                self.assertEqual(shown(made), "A sample", "not an empty box kept empty for a list that will never come")
                self.assertNotIn(name, made._form_text()[ui.PICKED])
                # And opened on another day, with the answer already known: no promise that cannot be kept.
                later = panel(settings={"last_values": {node.id: dict(saved)}}, node_tools=[tools.apply_initial_state(node, answer)])
                later._states[node.id] = answer
                self.open_form(later, later.node_tools[0])
                self.assertEqual((later._held_picks, shown(later)), ({}, "A sample"))
                self.assertNotIn("comes back when its list has loaded", str(later._shown.get(("Status", "Text"), "")))

    def test_a_picker_left_on_its_first_entry_holds_nothing_back(self):
        # "Node default (Aria)" was what the picker showed, not a choice: nothing
        # to hold, nothing to type, and a sample the node sends later is shown.
        node = speak()
        saved = {"voice": "Node default (Aria)", "greeting": "Node default (Good morning)", ui.PICKED: {"voice": 0, "greeting": 0}}
        made = panel(settings={"last_values": {node.id: saved}}, node_tools=[node])
        self.open_form(made, node)
        self.assertEqual((boxes(made)["voice"][1], boxes(made)["greeting"][1]), ("", ""))
        self.assertEqual(made._held_picks, {})
        self.arrive(made, node.id)
        self.assertEqual(boxes(made)["voice"], ("choice", tools.leave_label("node", "Aria"), 0))
        self.assertEqual(boxes(made)["greeting"][1], "Good morning", "the sample is not hidden by what was remembered")

    def test_with_the_list_already_loaded_it_is_simply_restored(self):
        node = tools.apply_initial_state(speak(), LISTS)
        made = panel(settings={"last_values": {node.id: dict(self.SAVED)}}, node_tools=[node])
        self.open_form(made, node)
        self.assertEqual(boxes(made)["voice"], ("choice", "Blade", 2))


class ListsThatArriveLate(Base):
    def test_a_sample_that_arrives_with_the_form_open_fills_the_box_that_had_nothing(self):
        node = speak(); made = panel(node_tools=[node])
        self.open_form(made, node)
        self.assertEqual(boxes(made)["greeting"][1], "")
        made._row("RowEdit", row_of(made, "speed")).Text = "1.2"
        self.arrive(made, node.id)
        self.assertEqual(boxes(made)["greeting"][1], "Good morning")
        self.assertEqual(boxes(made)["speed"][1], "1.2", "what was typed meanwhile stays")
        self.assertEqual(boxes(made)["voice"][0], "choice")
        made._row("RowEdit", row_of(made, "greeting")).Text = ""
        made._show_run()
        self.assertEqual(boxes(made)["greeting"][1], "", "emptied by the user: it stays empty")

    def test_a_sample_that_arrives_while_the_user_is_back_in_the_list_is_there_on_return(self):
        node = speak(); made = panel(node_tools=[node])
        self.open_form(made, node)
        made._set_view(ui.VIEW_BROWSE)
        self.arrive(made, node.id)
        made._open_run()
        self.assertEqual(boxes(made)["greeting"][1], "Good morning")
        made._remember_values()
        self.assertEqual(made.settings["last_values"][node.id]["greeting"], "Good morning")

    def test_lists_that_came_off_screen_and_were_never_looked_at_do_not_turn_empty_into_emptied(self):
        node = speak(); other = tools.node_from_detail({"type": "builtin/other", "title": "Other", "category": "Text", "inputs": [{"name": "speed", "type": "string"}], "outputs": []})
        made = panel(node_tools=[node, other])
        self.open_form(made, node)
        made._set_view(ui.VIEW_BROWSE)
        self.arrive(made, node.id)  # while the user is back in the list
        made.tool = other
        made._open_run()  # and then opens another node: the first form is put away as it was
        self.assertNotIn("greeting", made.settings["last_values"][node.id], "it never showed a sample, so it was not emptied")
        made.tool = next(tool for tool in made.node_tools if tool.id == node.id)
        made._open_run()
        self.assertEqual(boxes(made)["greeting"][1], "Good morning")

    def test_a_box_whose_sample_never_came_is_not_remembered_as_emptied(self):
        node = speak(); made = panel(node_tools=[node])
        self.open_form(made, node)
        made._remember_values()
        self.assertNotIn("greeting", made.settings["last_values"][node.id])
        self.assertEqual(made.settings["last_values"][node.id]["speed"], "")

    def test_what_was_typed_while_waiting_becomes_that_option_or_is_said_not_to_be_one(self):
        for typed, expected in (("v2", ("choice", "Blade", 2)), ("blade", ("choice", "Blade", 2)), ("  V1 ", ("choice", "Aria", 1))):
            node = speak(); made = panel(node_tools=[node])
            self.open_form(made, node)
            made._row("RowEdit", row_of(made, "voice")).Text = typed
            self.arrive(made, node.id)
            self.assertEqual(boxes(made)["voice"], expected, typed)
            self.assertNotIn("not in the list", str(made._shown.get(("Status", "Text"), "")))
        node = speak(); made = panel(node_tools=[node])
        self.open_form(made, node)
        made._row("RowEdit", row_of(made, "voice")).Text = "Rachel"
        self.arrive(made, node.id)
        self.assertEqual(boxes(made)["voice"][2], 0)
        self.assertIn('"Rachel" is not in the list', str(made._shown.get(("Status", "Text"), "")))

    def test_a_busy_server_leaves_a_short_hint_and_the_way_to_retry_in_its_tooltip(self):
        node = speak(); made = panel(node_tools=[node])
        made._state_tried.add(node.id); made._state_retry.add(node.id)
        self.open_form(made, node)
        box = made._row("RowEdit", row_of(made, "voice"))
        self.assertLessEqual(len(box.PlaceholderText), 40, "it has to fit the box")
        self.assertIn("did not load", box.PlaceholderText)
        refused = panel(node_tools=[node])
        refused._state_tried.add(node.id)  # asked, and the node could not start: no retry will help
        self.open_form(refused, node)
        box = refused._row("RowEdit", row_of(refused, "voice"))
        self.assertLessEqual(len(box.PlaceholderText), 40)
        self.assertIn("Studio", box.ToolTip)
        box = made._row("RowEdit", row_of(made, "voice"))
        self.assertIn("open the node again", box.ToolTip)
        self.assertEqual(made._row("RowEdit", row_of(made, "speed")).ToolTip, "", "only on the box it is about")
        made._state_retry.clear()
        self.arrive(made, node.id, {"inputs": {}})  # asked again, and the node had no list for it after all
        box = made._row("RowEdit", row_of(made, "voice"))
        self.assertEqual((box.ToolTip, "did not load" in box.PlaceholderText), ("", False), "the hint goes when it no longer holds")


class OwnLists(Base):
    def test_an_install_all_does_not_take_a_nodes_lists_away(self):
        node = speak(); made = panel(node_tools=[node]); made.tool = node
        self.arrive(made, node.id)
        made._bulk = {"total": 1, "stop": threading.Event(), "started": time.monotonic(), "finished": True, "failed": 0, "loaded": [speak()]}
        with mock.patch.object(ui.Panel, "_refresh_installed", lambda self, redraw=True: None):
            made._collect_bulk()
        self.assertEqual(self.voice_options(made.node_tools[0]), ("v1", "v2"))
        self.assertEqual(self.voice_options(made.tool), ("v1", "v2"))

    def test_without_a_key_the_node_is_asked_again_when_next_picked(self):
        node = speak(); made = panel(settings={"api_key": ""}, node_tools=[node]); made.tool = node
        made._fetch_state(node)
        self.assertIn(node.id, made._state_retry)
        self.assertNotIn(node.id, made._state_fetch)
        made._retry_state_on_pick(node)
        made._want_state(node)
        self.assertIsNotNone(made._state_due)

    def change_key(self, made, fresh_nodes):
        """Save another key on the Account page, with nothing written and nothing loaded."""
        made.items["ApiKey"].Text = "another-key"
        made.items["BaseUrl"].Text = made.settings["base_url"]
        made.items["MaxHeight"].Text = "720"
        made.items["RunTimeout"].Text = "900"

        def update(**changes):
            made.settings.update(changes)
            return dict(made.settings)

        with mock.patch.object(config, "update", side_effect=update), \
                mock.patch.object(tools, "read_snapshot", return_value=None), \
                mock.patch.object(tools, "read_nodes", return_value=list(fresh_nodes)), \
                mock.patch.object(ui.Panel, "reload_workflows", lambda self, quiet=False: None), \
                mock.patch.object(ui.Panel, "_rebuild_list", lambda self: None), \
                mock.patch.object(ui.Panel, "_rebuild_show_filter", lambda self: None), \
                mock.patch.object(ui.Panel, "_account_info", lambda self: ""), \
                mock.patch.object(ui.Panel, "placement", "new_track", create=True):
            made._save_settings()

    def test_another_key_starts_over_with_no_lists_from_the_account_before(self):
        node = speak(); made = panel(node_tools=[node]); made.tool = node
        self.arrive(made, node.id)
        self.assertEqual(self.voice_options(made.tool), ("v1", "v2"))
        still_loading = made.loader = FinishedLoader()
        self.change_key(made, [speak()])
        self.assertTrue(still_loading.stopped, "a loader still working for the other account is stopped")
        self.assertIsNone(made.loader)
        self.assertEqual((made._states, made._state_tried, made._state_retry), ({}, set(), set()))
        self.assertFalse(self.voice_options(made.node_tools[0]), "the nodes are as the catalog has them, with no voices")
        self.assertFalse(self.voice_options(made.tool))
        self.assertTrue(tools.needs_initial_state(made.tool), "so its lists are asked for again, with the new key")

    def test_an_answer_asked_for_with_the_old_key_is_never_applied(self):
        node = speak(); made = panel(node_tools=[node]); made.tool = node
        gate, calls = threading.Event(), []

        def answer(cfg, node_type):
            calls.append(cfg.api_key)
            if cfg.api_key == "k":
                gate.wait(5)  # the old key's request is slow
                return OTHER_ACCOUNT
            return LISTS

        with mock.patch.object(api, "get_node_initial_state", side_effect=answer):
            made._fetch_state(node)
            self.change_key(made, [node])
            made.tool = made.node_tools[0]
            made._fetch_state(made.tool)
            deadline = time.time() + 5
            while (made._state_fetch.get(node.id) or {}).get("state") is None and time.time() < deadline:
                time.sleep(0.01)
            gate.set()  # now the old one lands, after the new one and before the window looks
            for thread in threading.enumerate():
                if thread.name == "kaitoi-node-state":
                    thread.join(5)
            made._collect_states()
        self.assertEqual(calls, ["k", "another-key"])
        self.assertEqual(made._states.get(node.id), LISTS, "the new key's answer, not wiped out by the old one")
        self.assertEqual(self.voice_options(made.tool), ("v1", "v2"))

    def test_an_old_answer_already_waiting_when_the_key_changes_is_dropped(self):
        node = speak(); made = panel(node_tools=[node]); made.tool = node
        made._state_account = 3
        made._state_fetch[node.id] = {"state": OTHER_ACCOUNT, "error": None, "account": 2}
        made._collect_states()
        self.assertEqual(made._states, {})
        self.assertFalse(self.voice_options(made.tool))


class ColumnsOnTheList(Base):
    def at(self, made, width):
        made.win.Geometry = {1: 0, 2: 0, 3: width, 4: 700}
        made._columns_checked = 0.0
        made._refit_columns()
        tree = made.items["Tools"]
        return tree.ColumnWidth[0], tree.ColumnWidth[1]

    def test_narrowing_to_fit_never_changes_what_the_user_asked_for(self):
        made = panel(width=440)
        made._draw_columns()
        tree = made.items["Tools"]
        self.assertEqual((tree.ColumnWidth[0], tree.ColumnWidth[1]), (199, 99))
        made._read_columns()  # as every redraw of the list does
        self.assertEqual(made._columns[:2], [204, 116])
        self.assertEqual(self.at(made, 700), (204, 116), "a wider window gets them back")
        for _ in range(5):
            self.at(made, 440); self.at(made, 700)
        self.assertEqual(made._columns[:2], [204, 116], "nothing drifts")

    def test_a_dragged_column_is_taken_and_the_other_is_left(self):
        made = panel(width=300)
        made._draw_columns()
        tree = made.items["Tools"]
        self.assertEqual((tree.ColumnWidth[0], tree.ColumnWidth[1]), (120, 80))
        tree.ColumnWidth[1] = 95  # the user drags only the second
        made._read_columns()
        self.assertEqual(made._columns[:2], [204, 95], "the first was only drawn narrow, never asked to be")
        self.assertEqual(self.at(made, 700), (204, 95))

    def test_the_fit_is_looked_at_once_a_second_and_only_when_the_width_changed(self):
        made = panel(width=440)
        made._draw_columns()
        tree = made.items["Tools"]
        tree.ColumnWidth[0] = 150  # a drag in progress
        made._columns_checked = time.monotonic()
        made.win.Geometry = {1: 0, 2: 0, 3: 700, 4: 700}
        made._refit_columns()
        self.assertEqual(tree.ColumnWidth[0], 150, "not looked at yet")
        made._columns_checked = 0.0
        made.win.Geometry = {1: 0, 2: 0, 3: 440, 4: 700}
        made._refit_columns()
        self.assertEqual(tree.ColumnWidth[0], 150, "same width as when drawn: the drag is not fought")
        self.assertEqual(made._columns_drawn, (199, 99), "and nothing was drawn again")


class PicturesOnTheList(Base):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        home = mock.patch.object(config, "APP_DIR", os.path.join(self.dir.name, "app"))
        home.start(); self.addCleanup(home.stop)

    def test_a_row_and_the_card_follow_the_picture_as_it_arrives_changes_and_goes(self):
        node = tools.node_from_summary({"type": "builtin/x", "title": "X", "category": "Image", "thumbnailUrl": "http://127.0.0.1:9/t?token=t"})
        made = panel(node_tools=[node])
        row = made._tool_items[node.id] = Widget()
        icon = ui._placeholder(node)
        self.assertEqual(made._picture(node), icon, "nothing fetched yet")
        stored = tools.thumbnail_path(node)
        with open(stored, "wb") as fh:
            fh.write(fake_jpeg(256, 144))
        made.loader = FinishedLoader([(node.id, stored)])
        made._collect_loader()
        self.assertEqual(made._picture(node), stored, "the answer from before the picture came is forgotten")
        self.assertEqual(row.Icon[0], stored)
        # A refresh brings a square picture whose cut could not be made: back to the icon, in both places.
        with open(stored, "wb") as fh:
            fh.write(fake_jpeg(256, 256))
        made.loader = FinishedLoader([(node.id, stored)])
        made._collect_loader()
        self.assertEqual(made._picture(node), icon)
        self.assertEqual(row.Icon[0], icon, "the row does not keep a picture that is gone")
        with open(tools.tile_path(node), "wb") as fh:
            fh.write(b"cut")
        made.loader = FinishedLoader([(node.id, stored)])
        made._collect_loader()
        self.assertEqual((made._picture(node), row.Icon[0]), (tools.tile_path(node), tools.tile_path(node)))


class NotesUnderARun(Base):
    """The yellow lines a finished run leaves above its steps."""

    NOT_PLACED = "Not placed: Primitive: Resolve could not import kaitoi_Create_3D_Primitive_primitiveModel_20261115_185911.glb"

    def height(self, made):
        return made.items["Notes"].MinimumSize[1]

    def test_a_note_gets_the_room_all_of_its_lines_take(self):
        made = panel(width=490)
        made.notes([self.NOT_PLACED])
        self.assertTrue(made.items["Notes"].Visible)
        self.assertEqual(self.height(made), 15 * 2 + 2, "two lines at this width: left one line high, both are cut through")
        self.assertEqual(made.items["Notes"].MaximumSize[1], self.height(made))
        made.notes([self.NOT_PLACED, "Length: the result is 5.0 s, the range was 6.0 s."])
        self.assertEqual(self.height(made), 15 * 3 + 2, "and it grows with what it is given")
        made.notes(["Seed: 1337"])
        self.assertEqual(self.height(made), 15 + 2, "and shrinks again")
        made.notes([])
        self.assertFalse(made.items["Notes"].Visible)
        self.assertEqual(self.height(made), 0, "with nothing to say it takes no room")

    def test_a_long_file_name_that_does_not_fit_the_rest_of_a_line_counts_as_a_line_of_its_own(self):
        made = panel(width=490)  # about 76 letters to a line
        self.assertEqual(made._text_height("x" * 76), 15 + 2)
        self.assertEqual(made._text_height("a few short words"), 15 + 2)
        self.assertEqual(made._text_height("w" * 50 + " " + "n" * 50), 15 * 2 + 2)
        self.assertEqual(made._text_height("w" * 50 + " " + "n" * 50 + " " + "m" * 50), 15 * 3 + 2, "by letters alone this would be two lines")
        self.assertEqual(made._text_height("n" * 160), 15 * 3 + 2, "a word longer than a line runs on")
        self.assertEqual(made._text_height("one\ntwo"), 15 * 2 + 2)
        self.assertEqual(made._text_height(""), 0)


def makes(name, *kinds, inputs=(), source="node", ready=True, category="Things"):
    """A tool that makes results of these kinds ("image", "video", "audio", "string", "model"...)."""
    made = tools.Tool(
        id=f"{source}:{name}", source=source, name=name, description="", category=category, tags=(),
        inputs=tuple(inputs), outputs=tuple(tools.Output(f"out{at}", kind.title(), tools.normalize_type(kind)) for at, kind in enumerate(kinds)),
        node_type=f"builtin/x/{name}" if source == "node" else "", origin="builtin" if source == "node" else "", ready=ready,
    )
    return made


class WhatResolveCanPlaceAndFavorites(Base):
    """The two filters the Nodes and Workflows tabs share."""

    def setUp(self):
        super().setUp()
        self.saved = {}  # the settings file: an update writes what it is given and reads the rest back from here

        def update(**changes):
            self.saved.update(changes)
            return dict(self.saved)

        patcher = mock.patch.object(config, "update", side_effect=update)
        patcher.start(); self.addCleanup(patcher.stop)
        self.nodes = [makes("Picture", "image"), makes("Film", "video"), makes("Sound", "audio"), makes("Words", "string"), makes("Model", "model", category="3D"),
                      makes("Film and words", "video", "string"), makes("Nothing", category="Odd"), makes("Not loaded", ready=False),
                      makes("Web page", "web", category="Web"), makes("Web editor that also makes a film", "video", "web", category="Web")]
        self.flows = [makes("Grade", "video", source="workflow", category="Workflows"), makes("Describe", "string", source="workflow", category="Workflows")]
        self.made = panel(node_tools=self.nodes, workflow_tools=self.flows)
        self.saved.update(self.made.settings)
        self.made._chosen = {}
        for box in ("OriginBuiltin", "OriginCommunity", "OriginUser"):
            self.made.items[box].Checked = True

    def names(self, catalog=ui.NODES):
        self.made.catalog = catalog
        return sorted(tool.name for tool in self.made._listed())

    def test_only_what_makes_a_picture_a_video_or_sound(self):
        everything = sorted(tool.name for tool in self.nodes)
        self.assertEqual(self.names(), everything, "off: every node")
        self.made.items["MediaOnly"].Checked = True
        self.assertEqual(self.names(), ["Film", "Film and words", "Not loaded", "Picture", "Sound"],
                         "text alone, a 3D model and a node that makes nothing are left out; one whose results are not known yet is not")
        self.assertNotIn("Web editor that also makes a film", self.names(),
                         "a node worked in a web page of its own is left out even though it makes a film: there is no such page in Resolve")
        self.assertEqual(self.names(ui.WORKFLOWS), ["Grade"], "and the same on the Workflows tab")
        self.made.items["MediaOnly"].Checked = False
        self.assertEqual(self.names(ui.WORKFLOWS), ["Describe", "Grade"])

    def test_it_narrows_within_the_other_filters_not_instead_of_them(self):
        self.made.items["MediaOnly"].Checked = True
        self.made.items["Search"].Text = "film"
        self.assertEqual(self.names(), ["Film", "Film and words"])
        self.made.items["Search"].Text = "words"
        self.assertEqual(self.names(), ["Film and words"])
        self.made.items["Search"].Text = ""
        self.made.items["OriginBuiltin"].Checked = False
        self.assertEqual(self.names(), [])

    def test_the_window_opens_with_the_filter_on_and_favorites_off_until_the_user_says_otherwise(self):
        with open(ui.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('items["MediaOnly"].Checked = bool(self.settings.get("media_only", True))', source)
        self.assertIn('items["FavoritesOnly"].Checked = bool(self.settings.get("favorites_only", False))', source)

    def test_a_favorite_is_marked_saved_at_once_and_shown_in_the_list(self):
        made = self.made
        made.tool = self.nodes[1]  # Film
        made._show_detail()
        self.assertEqual((made.items["Favorite"].Text, made.items["Favorite"].Enabled), (ui.FAVORITE_OFF, True))
        with mock.patch.object(ui.Panel, "_rebuild_list", lambda self: None), mock.patch.object(ui.Panel, "_rebuild_show_filter", lambda self: None):
            made._toggle_favorite()
            self.assertEqual(self.saved["favorites"], ["node:Film"], "written to the settings when pressed, not when the window closes")
            self.assertEqual(made.favorites, {"node:Film"})
            self.assertEqual(made.items["Favorite"].Text, ui.FAVORITE_ON)
            self.assertEqual(made._row_name(self.nodes[1], made.favorites), ui.FAVORITE_MARK + "Film")
            self.assertEqual(made._row_name(self.nodes[0], made.favorites), "Picture")
            made.tool = self.flows[0]
            made._toggle_favorite()
            self.assertEqual(self.saved["favorites"], ["node:Film", "workflow:Grade"], "nodes and workflows alike")
            made._toggle_favorite()
            self.assertEqual(self.saved["favorites"], ["node:Film"], "pressed again, it is taken out")
            self.assertEqual(made.items["Favorite"].Text, ui.FAVORITE_OFF)
            made.tool = None
            made._toggle_favorite()  # nothing picked: nothing to mark
            self.assertEqual(self.saved["favorites"], ["node:Film"])
        made._show_detail()
        self.assertFalse(made.items["Favorite"].Enabled)

    def test_what_was_typed_into_a_form_survives_marking_a_favorite(self):
        made = self.made
        made.settings["last_values"] = {"node:Film": {"prompt": "typed, not yet saved"}}
        made.tool = self.nodes[1]
        with mock.patch.object(ui.Panel, "_rebuild_list", lambda self: None), mock.patch.object(ui.Panel, "_rebuild_show_filter", lambda self: None):
            made._on_kept_changed()  # first, while the file knows nothing of what was typed
            self.assertEqual(made.settings["last_values"], {"node:Film": {"prompt": "typed, not yet saved"}})
            self.saved.pop("last_values")
            made.settings["last_values"] = {"node:Film": {"prompt": "typed again, not yet saved"}}
            made._toggle_favorite()
            self.assertEqual(made.settings["last_values"], {"node:Film": {"prompt": "typed again, not yet saved"}})

    def test_favorites_only_lists_the_marked_ones_on_each_tab(self):
        made = self.made
        made.settings["favorites"] = ["node:Words", "node:Film", "workflow:Describe", "node:gone since"]
        self.assertEqual(len(self.names()), 10, "off: every node")
        made.items["FavoritesOnly"].Checked = True
        self.assertEqual(self.names(), ["Film", "Words"])
        self.assertEqual(self.names(ui.WORKFLOWS), ["Describe"])
        made.items["MediaOnly"].Checked = True
        self.assertEqual(self.names(), ["Film"], "both filters at once: a favorite Resolve can place")
        self.assertEqual(self.names(ui.WORKFLOWS), [])

    def test_switching_a_filter_is_remembered_and_the_categories_on_offer_follow(self):
        made = self.made
        made.catalog = ui.NODES
        with mock.patch.object(ui.Panel, "_rebuild_list", lambda self: None), mock.patch.object(ui.Panel, "_relayout", lambda self: None):
            made._rebuild_show_filter()
            self.assertEqual(made._show_keys, ["cat:", "cat:3D", "cat:Odd", "cat:Things", "cat:Web"])
            made.items["MediaOnly"].Checked = True
            made._on_kept_changed()
            self.assertEqual((self.saved["media_only"], self.saved["favorites_only"]), (True, False))
            self.assertEqual(made._show_keys, ["cat:", "cat:Things"], "a category with nothing left in it is not offered")
            made.settings["favorites"] = self.saved["favorites"] = ["node:Model"]  # marked, and so saved
            made.items["MediaOnly"].Checked = False
            made.items["FavoritesOnly"].Checked = True
            made._on_kept_changed()
            self.assertEqual((self.saved["media_only"], self.saved["favorites_only"]), (False, True))
            self.assertEqual(made._show_keys, ["cat:", "cat:3D"])
            made.catalog = ui.WORKFLOWS
            made.items["FavoritesOnly"].Checked = False
            made.items["MediaOnly"].Checked = True
            made._rebuild_show_filter()
            self.assertEqual(len(made._show_keys), 2, "every task, and the one task a workflow Resolve can place has")

    def test_with_favorites_only_on_and_none_marked_the_list_says_how_to_mark_one(self):
        made = self.made
        made.balance, made.load_error = "", ""
        made._matched = 0
        made.items["FavoritesOnly"].Checked = True
        with mock.patch.object(ui.Panel, "_update_install_all", lambda self: None):
            made._update_list_info(0)
            self.assertIn("No favorite nodes yet", made._shown[("ListInfo", "Text")])
            self.assertIn("switch Favorites only off", made._shown[("ListInfo", "Text")])
            made.settings["favorites"] = ["node:Film"]
            made._update_list_info(1)
            self.assertNotIn("No favorite", made._shown[("ListInfo", "Text")])
            made.catalog = ui.WORKFLOWS  # a favorite node is no favorite workflow: this tab is still empty, and says why
            made._update_list_info(0)
            self.assertIn("No favorite workflows yet", made._shown[("ListInfo", "Text")])
            made.settings["favorites"] = ["node:Film", "workflow:Grade"]
            made._update_list_info(1)
            self.assertNotIn("No favorite", made._shown[("ListInfo", "Text")])
            made.settings["favorites"] = []
            made.items["FavoritesOnly"].Checked = False
            made._update_list_info(2)
            self.assertNotIn("No favorite", made._shown[("ListInfo", "Text")], "with the filter off, having none is nothing to say")

    def test_a_settings_file_with_something_else_under_favorites_is_no_favorites(self):
        for odd in (None, "node:Film", {"node:Film": True}, 7):
            self.made.settings["favorites"] = odd
            self.assertEqual(self.made.favorites, set())


class HowManyAreListed(Base):
    """The line under the list leads with what the search and the filters leave."""

    def line(self, made, shown, matched):
        made._matched = matched
        with mock.patch.object(ui.Panel, "_update_install_all", lambda self: None):
            made._update_list_info(shown)
        return made._shown[("ListInfo", "Text")]

    def setUp(self):
        super().setUp()
        self.nodes = [makes(f"Node {at}", "image") for at in range(1500)]
        self.made = panel(node_tools=self.nodes, workflow_tools=[makes("Grade", "video", source="workflow"), makes("Tint", "image", source="workflow")])
        self.made.balance, self.made.load_error = "", ""
        for box in ("OriginBuiltin", "OriginCommunity", "OriginUser"):
            self.made.items[box].Checked = True

    def test_filtered_down_it_says_how_many_are_left_and_out_of_how_many(self):
        self.assertEqual(self.line(self.made, 12, 12), "12 nodes of 1,500")
        self.assertEqual(self.line(self.made, 1, 1), "1 node of 1,500")
        self.assertEqual(self.line(self.made, 0, 0), "0 nodes of 1,500")

    def test_more_than_the_list_draws_it_still_says_how_many_match(self):
        self.assertEqual(self.line(self.made, 100, 1234), "1,234 nodes, first 100 shown")

    def test_with_nothing_filtered_out_it_is_just_the_count(self):
        self.made.node_tools = self.nodes[:40]
        self.assertEqual(self.line(self.made, 40, 40), "40 nodes")

    def test_the_workflows_tab_counts_the_same_way(self):
        self.made.catalog = ui.WORKFLOWS
        self.assertEqual(self.line(self.made, 2, 2), "2 workflows")
        self.assertEqual(self.line(self.made, 1, 1), "1 workflow of 2")

    def test_the_balance_still_follows_it(self):
        self.made.balance = "6,417 credits"
        self.assertEqual(self.line(self.made, 12, 12), "12 nodes of 1,500  ·  6,417 credits")


class ProgressOfARun(Base):
    """The bar above the status line, under every view."""

    def test_how_much_of_a_run_is_behind_it(self):
        self.assertEqual(len(ui.STAGE_SHARES), len(ui.pipeline.STAGES_SHORT))
        self.assertAlmostEqual(sum(ui.STAGE_SHARES), 1.0)
        self.assertGreaterEqual(ui.STAGE_SHARES[3], 0.5, "the run itself is most of the wait, so it is most of the bar")
        self.assertEqual(ui.overall_progress(0, None), 0.0)
        self.assertAlmostEqual(ui.overall_progress(3, None), 0.25, msg="export, upload and prepare behind it")
        self.assertAlmostEqual(ui.overall_progress(3, 0.5), 0.55, msg="and half of the run, as Kaitoi reports it")
        self.assertAlmostEqual(ui.overall_progress(3, 1.0), 0.85)
        self.assertAlmostEqual(ui.overall_progress(5, 1.0), 1.0)
        self.assertEqual(ui.overall_progress(-1, None), 0.0, "before the first step")
        self.assertAlmostEqual(ui.overall_progress(3, 7.0), 0.85, msg="a report out of range is held to the step it belongs to")
        self.assertAlmostEqual(ui.overall_progress(3, -2.0), 0.25)
        self.assertEqual(ui.overall_progress(9, None), 1.0)

    def test_the_bar_is_painted_as_far_as_the_run_has_got(self):
        self.assertIn("stop:0.420 " + ui.C_ACCENT, ui.bar_style(0.42))
        self.assertIn("stop:0.421 " + ui.C_LINE, ui.bar_style(0.42))
        self.assertNotIn("qlineargradient", ui.bar_style(0.0))
        self.assertIn(ui.C_LINE, ui.bar_style(0.0))
        self.assertNotIn("qlineargradient", ui.bar_style(1.0))
        self.assertIn(ui.C_ACCENT, ui.bar_style(1.0))
        self.assertEqual(ui.bar_style(3.0), ui.bar_style(1.0))
        self.assertEqual(ui.bar_style(-1.0), ui.bar_style(0.0))

    def test_it_shows_while_a_run_is_on_whatever_the_view_and_goes_when_it_ends(self):
        made = panel(width=490)
        made.spinner_index = 0
        bar = made.items["Progress"]
        for view in (ui.VIEW_BROWSE, ui.VIEW_RUN):  # the list as well as the form: one bar under both
            made.view = view
            made._render_progress({"stage": 3, "fraction": 0.5, "running": True, "status": "Run: Krea 2 Turbo: running 50% (12s)", "elapsed": 12})
            self.assertTrue(bar.Visible)
            self.assertEqual(bar.MinimumSize[1], ui.BAR_HEIGHT)
            self.assertEqual(bar.StyleSheet, ui.bar_style(0.55))
            self.assertIn("Run: Krea 2 Turbo: running 50%", made._shown[("Status", "Text")])
        made._render_progress({"stage": 3, "fraction": None, "running": True, "status": "Run: submitting", "elapsed": 1})
        self.assertEqual(bar.StyleSheet, ui.bar_style(0.25), "nothing reported for the step: the steps before it")
        made._render_progress({"stage": 5, "fraction": None, "running": False, "status": "done", "elapsed": 20})
        self.assertFalse(bar.Visible, "a finished run leaves no bar behind")
        self.assertEqual(bar.MaximumSize[1], 0, "and no gap where it was")
        made._render_progress({"stage": 1, "fraction": None, "running": True, "status": "Upload", "elapsed": 2})
        self.assertTrue(bar.Visible)
        made._render_progress(None)
        self.assertFalse(bar.Visible)


class CreditsAndTheAccountLine(Base):
    def test_the_balance_is_given_in_credits_not_in_dollars(self):
        answer = {"balanceCents": 6275, "balanceMicrocredits": "6275841515", "balanceDollars": 62.75}
        self.assertEqual(api.balance_text(answer), "6,275 credits", "6,275 credits are 62.75 dollars: the number of dollars is not the number of credits")
        self.assertEqual(api.balance_text({"balanceCents": 1}), "1 credit")
        self.assertEqual(api.balance_text({"balanceCents": 0}), "0 credits")
        self.assertEqual(api.balance_text({"balanceCents": 1234567}), "1,234,567 credits")
        for nothing in ({}, {"balanceCents": None}, {"balanceCents": "6275"}, {"balanceCents": True}, None, []):
            self.assertEqual(api.balance_text(nothing), "")

    def test_the_window_shows_that_number_wherever_it_shows_a_balance(self):
        with open(ui.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotRegex(source, r"/ ?100[^0-9.]*credits|balanceCents", "the balance is worded in one place, api.balance_text")
        self.assertEqual(source.count("api.balance_text(api.account_credits("), 2, "the list's line and Test connection")

    def test_the_first_line_of_the_account_tab_gets_the_room_its_two_lines_take(self):
        made = panel(width=424)
        made.balance = "6,275 credits"
        with mock.patch.object(ui.credentials, "source", return_value="/Users/someone/.kaitoi/credentials.json"):
            made._show_account_info()
            text = made._shown[("AccountInfo", "Text")]
            self.assertEqual(text, "Using the key from /Users/someone/.kaitoi/credentials.json.  6,275 credits.")
            self.assertEqual(made._shown[("AccountInfo", "MinimumSize")], [0, 15 * 2 + 2], "two lines at this width: left one line high, both are cut through")
            made.balance = ""
            made._show_account_info()
            self.assertEqual(made._shown[("AccountInfo", "MinimumSize")], [0, 15 + 2], "and one line when it is one")


class NameOnTheCard(Base):
    """Beside the picture and the Favorite button the card's name has little width."""

    def card(self, name, width=424, category="Image to Image"):
        made = panel(width=width, node_tools=[])
        made._chosen = {}
        made.tool = makes(name, "image", inputs=(), category=category)
        with mock.patch.object(ui.Panel, "_want_state", lambda self, tool: None), mock.patch.object(ui.Panel, "_show_thumbnail", lambda self, tool: None), \
                mock.patch.object(ui.Panel, "_sends_line", lambda self: "Sends the frame under the playhead."):
            made._show_detail()
        return made

    def heights(self, made):
        return made._shown[("DetailName", "MinimumSize")][1], made._shown[("DetailTask", "MinimumSize")][1]

    def test_a_name_that_runs_on_to_a_second_line_gets_the_room_for_it(self):
        self.assertEqual(self.heights(self.card("Krea 2 Turbo"))[0], 20 + 2)
        self.assertEqual(self.heights(self.card("FLUX SRPO Image to Image"))[0], 20 * 2 + 2, "two lines in a window this narrow, as seen on screen: left one line high, both are cut through")
        self.assertEqual(self.heights(self.card("FLUX SRPO Image to Image", width=440))[0], 20 + 2, "sixteen points wider it is one line, as seen on screen too")
        self.assertEqual(self.heights(self.card("FLUX SRPO Image to Image", width=700))[0], 20 + 2)
        self.assertEqual(self.heights(self.card("Topaz Generative Video Upscale With A Very Long Name Indeed"))[0], 20 * 3 + 2)

    def test_so_does_the_line_under_it(self):
        self.assertEqual(self.heights(self.card("Krea", category="Image"))[1], 15 + 2)
        self.assertEqual(self.heights(self.card("Krea", category="A category with a long name of its own"))[1], 15 * 2 + 2)

    def test_it_is_fitted_again_when_the_window_is_made_wider_or_narrower(self):
        made = self.card("FLUX SRPO Image to Image")
        self.assertEqual(self.heights(made)[0], 42)
        made.win.Geometry = {1: 0, 2: 0, 3: 700, 4: 700}
        made._columns_set, made._columns_checked, made._columns_width = True, 0.0, 424
        with mock.patch.object(ui.Panel, "_read_columns", lambda self: None), mock.patch.object(ui.Panel, "_draw_columns", lambda self: None):
            made._refit_columns()
        self.assertEqual(self.heights(made)[0], 22)

    def test_with_nothing_picked_the_card_keeps_no_room_for_a_name(self):
        made = self.card("FLUX SRPO Image to Image")
        made.tool = None
        with mock.patch.object(ui.Panel, "_want_state", lambda self, tool: None), mock.patch.object(ui.Panel, "_show_thumbnail", lambda self, tool: None):
            made._show_detail()
        self.assertEqual(self.heights(made), (0, 0))

    def test_the_small_text_elsewhere_is_measured_as_before(self):
        made = panel(width=490)
        self.assertEqual(made._text_height("x" * 76), 17)
        self.assertEqual(made._text_height("x" * 77), 32)


class Tooltips(Base):
    """A tooltip is a hint read at a glance. The card under the list is where a description is read."""

    def test_a_rows_tooltip_is_the_first_sentence_of_the_description_cut_to_a_line(self):
        long = "Transform images using FLUX SRPO (Self-Rewarding Policy Optimization) image-to-image via the FAL REST API. Supports many sizes. And more."
        tip = ui.short_tip(long)
        self.assertLessEqual(len(tip), ui.TIP_LETTERS)
        self.assertTrue(tip.endswith("…"))
        self.assertTrue(long.startswith(tip[:-1]), "nothing reworded")
        self.assertEqual(long[len(tip) - 1], " ", "cut between two words, never through one")
        self.assertFalse(tip[:-1].endswith(" "))
        self.assertEqual(ui.short_tip("word " * 40), ("word " * 14 + "word") + "…")
        self.assertEqual(ui.short_tip("Reverse a video. It can also change its speed."), "Reverse a video.")
        self.assertEqual(ui.short_tip("No full stop here"), "No full stop here")
        self.assertEqual(ui.short_tip("  spread\nover   lines.  More."), "spread over lines.")
        self.assertEqual(ui.short_tip(""), "")
        self.assertEqual(ui.short_tip(None), "")

    def test_no_tooltip_written_in_the_window_is_longer_than_a_line(self):
        with open(ui.__file__, encoding="utf-8") as fh:
            source = fh.read()
        written = re.findall(r'ToolTip"?\]?(?:\s*[:=]\s*|",\s*)f?"([^"]+)"', source) + re.findall(r'if tool\.id in self\.favorites else "([^"]+)"', source)
        self.assertGreater(len(written), 8)
        filled = [re.sub(r"\{[^}]*\}", "a value", tip) for tip in written]  # what stands in braces is a number or a name
        self.assertEqual([tip for tip in filled if len(tip) > ui.TIP_LETTERS], [])


class Redraws(Base):
    """This UI kit lays a window out again only when told to, and a layout or a repaint at every tick is what flicker is."""

    def counted(self, made):
        calls = []
        patcher = mock.patch.object(ui.Panel, "_relayout", lambda self: calls.append(1))
        patcher.start(); self.addCleanup(patcher.stop)
        return calls

    def test_the_bar_is_laid_out_when_it_comes_and_goes_and_not_in_between(self):
        made = panel(width=490)
        made.spinner_index = 0
        calls = self.counted(made)
        running = {"stage": 3, "fraction": 0.5, "running": True, "status": "Run: X: running 50% (1s)", "elapsed": 1}
        made._render_progress(running)
        appeared = len(calls)
        self.assertGreaterEqual(appeared, 1, "shown without a layout it keeps the no room it had while hidden")
        for _ in range(200):
            made._render_progress(running)
        self.assertEqual(len(calls), appeared, "the same state again: nothing to lay out")
        sets = []
        real_put = ui.Panel._put

        def put(self, item_id, prop, value):
            before = self._shown.get((item_id, prop), self)
            real_put(self, item_id, prop, value)
            if item_id == "Progress" and before != value:
                sets.append(value)

        with mock.patch.object(ui.Panel, "_put", put):
            for at in range(200):
                made._render_progress({**running, "fraction": 0.5 + at / 100000})
        self.assertEqual(sets, [], "a change too small to see is not painted")
        made._render_progress({**running, "running": False})
        self.assertEqual(len(calls), appeared + 1, "and once more when it goes")

    def test_the_account_line_is_laid_out_when_its_height_changes_and_only_then(self):
        made = panel(width=424)
        calls = self.counted(made)
        with mock.patch.object(ui.credentials, "source", return_value="/Users/someone/.kaitoi/credentials.json"):
            made.balance = ""
            made._show_account_info()
            self.assertEqual(len(calls), 1)
            made._show_account_info()
            self.assertEqual(len(calls), 1)
            made.balance = "6,275 credits"  # the balance arrives: the line is two lines now
            made._show_account_info()
            self.assertEqual(len(calls), 2, "grown without a layout, the second line is cut off")
            made._show_account_info()
            self.assertEqual(len(calls), 2)

    def test_the_balance_arriving_refits_the_account_line(self):
        made = panel(width=424)
        made.balance, made.load_error, made.loader = "", "", FinishedLoader()
        made.loader.take = lambda: {"tools": None, "snapshot": None, "error": None, "balance": "6,275 credits", "nodes": None, "thumbnails": []}
        with mock.patch.object(ui.credentials, "source", return_value="/Users/someone/.kaitoi/credentials.json"), \
                mock.patch.object(ui.Panel, "_update_list_info", lambda self, shown: None):
            made._show_account_info()
            self.assertEqual(made._shown[("AccountInfo", "MinimumSize")], [0, 17])
            made._collect_loader() if hasattr(made, "_collect_loader") else made._take_loaded()
            self.assertIn("6,275 credits", made._shown[("AccountInfo", "Text")])
            self.assertEqual(made._shown[("AccountInfo", "MinimumSize")], [0, 32])

    def test_the_card_is_laid_out_when_a_name_needs_another_height_and_not_at_every_draw(self):
        made = panel(width=424)
        made._chosen = {}
        calls = self.counted(made)
        with mock.patch.object(ui.Panel, "_want_state", lambda self, tool: None), mock.patch.object(ui.Panel, "_show_thumbnail", lambda self, tool: None), \
                mock.patch.object(ui.Panel, "_sends_line", lambda self: ""):
            made.tool = makes("Krea", "image")
            made._show_detail()
            first = len(calls)
            for _ in range(50):
                made._show_detail()
            self.assertEqual(len(calls), first)
            made.tool = makes("FLUX SRPO Image to Image", "image")
            made._show_detail()
            self.assertEqual(len(calls), first + 1)


class TheListAsDrawn(Base):
    """The list rebuilt for real, into rows that can be read back."""

    def setUp(self):
        super().setUp()
        self.saved = {}

        def update(**changes):
            self.saved.update(changes)
            return dict(self.saved)

        for patcher in (mock.patch.object(config, "update", side_effect=update), mock.patch.object(ui.Panel, "_picture", lambda self, tool: ""),
                        mock.patch.object(ui.Panel, "_update_install_all", lambda self: None), mock.patch.object(ui.Panel, "_relayout", lambda self: None),
                        mock.patch.object(ui.Panel, "_want_state", lambda self, tool: None), mock.patch.object(ui.Panel, "_show_thumbnail", lambda self, tool: None),
                        mock.patch.object(ui.Panel, "_sends_line", lambda self: "")):
            patcher.start(); self.addCleanup(patcher.stop)
        self.nodes = [makes("Film", "video", category="Video"), makes("Picture", "image", category="Image"), makes("Words", "string", category="Text")]
        self.made = made = panel(node_tools=self.nodes, width=440)
        self.saved.update(made.settings)
        made._chosen, made.rebuilds, made.balance, made.load_error = {}, 0, "", ""
        made.items["Tools"] = self.tree = Tree()
        for box in ("OriginBuiltin", "OriginCommunity", "OriginUser"):
            made.items[box].Checked = True
        made.tool = self.nodes[0]
        made._rebuild_show_filter()
        made._rebuild_list()

    def test_a_row_carries_one_line_of_the_description_as_its_tooltip(self):
        long = "Reverse a video and change how fast it plays, with or without its sound, frame by frame if need be. More. And more."
        self.made.node_tools = [tools.Tool(id="node:Long", source="node", name="Long", description=long, category="Video", tags=(), inputs=(),
                                           outputs=(tools.Output("out", "Out", "video"),), node_type="builtin/x/long", origin="builtin")]
        self.made._rebuild_list()
        tips = [child.ToolTip.get(0) for heading in self.tree.top for child in heading.children]
        self.assertEqual(tips, [ui.short_tip(long)])
        self.assertLessEqual(len(tips[0]), ui.TIP_LETTERS)

    def test_a_node_that_arrives_unable_to_work_here_but_is_not_on_show_redraws_nothing(self):
        made = self.made
        made.items["MediaOnly"].Checked = True
        made._on_kept_changed()
        before = made.rebuilds
        refilled = []
        real = ui.Panel._rebuild_show_filter
        with mock.patch.object(ui.Panel, "_rebuild_show_filter", lambda self: (refilled.append(1), real(self))[1]):
            made._node_fetch["node:Elsewhere"] = {"tool": makes("Elsewhere", "string"), "error": None}  # loaded for another reason, and not a row of this list
            made._collect_nodes()
        self.assertEqual((made.rebuilds, refilled), (before, []), "neither the list nor the categories above it")
        unknown_a, unknown_b = makes("Mystery A", ready=False, category="Text"), makes("Mystery B", ready=False, category="Text")
        made.node_tools = self.nodes + [unknown_a, unknown_b]
        made._rebuild_list()
        before = made.rebuilds
        shown = []
        real = ui.Panel._rebuild_show_filter
        with mock.patch.object(ui.Panel, "_rebuild_show_filter", lambda self: (shown.append(1), real(self))[1]):
            made._node_fetch[unknown_a.id] = {"tool": makes("Mystery A", "string", category="Text"), "error": None}
            made._node_fetch[unknown_b.id] = {"tool": makes("Mystery B", "string", category="Text"), "error": None}
            made._collect_nodes()
        self.assertEqual((made.rebuilds - before, len(shown)), (1, 1), "two that arrive together: the list and the categories are redrawn once")
        self.assertEqual(self.tree.rows(), ["Picture", "Film"])

    def test_marking_a_favorite_puts_the_star_on_its_row_at_once(self):
        made = self.made
        self.assertEqual(self.tree.rows(), ["Picture", "Words", "Film"])
        made._toggle_favorite()
        self.assertEqual(self.tree.rows(), ["Picture", "Words", ui.FAVORITE_MARK + "Film"], "the list is drawn again: the same tools, one of them marked now")
        made._toggle_favorite()
        self.assertEqual(self.tree.rows(), ["Picture", "Words", "Film"])

    def test_taking_a_favorite_out_under_favorites_only_removes_its_row_and_its_category(self):
        made = self.made
        made._toggle_favorite()
        made.items["FavoritesOnly"].Checked = True
        made._on_kept_changed()
        self.assertEqual((self.tree.rows(), made._show_keys), ([ui.FAVORITE_MARK + "Film"], ["cat:", "cat:Video"]))
        made._toggle_favorite()
        self.assertEqual(self.tree.rows(), [])
        self.assertEqual(made._show_keys, ["cat:"], "a category that has nothing left in it is not offered")
        self.assertIn("No favorite nodes yet", made._shown[("ListInfo", "Text")])

    def test_favorites_only_with_none_marked_lists_nothing(self):
        made = self.made
        made.items["FavoritesOnly"].Checked = True
        made._on_kept_changed()
        self.assertEqual(self.tree.rows(), [], "none marked is none to list, not a filter that is off")
        self.assertEqual(made._listed(), [])

    def test_a_node_whose_results_were_not_known_leaves_the_list_once_they_are(self):
        """A server that lists nodes without their pins: what a node makes is learned when it is picked."""
        made = self.made
        unknown = makes("Mystery", ready=False, category="Text")
        made.node_tools = self.nodes + [unknown]
        made.items["MediaOnly"].Checked = True
        made._on_kept_changed()
        self.assertEqual(self.tree.rows(), ["Picture", "Mystery", "Film"], "not ruled out while nothing is known against it")
        loaded = makes("Mystery", "string", category="Text")
        made.tool = unknown
        made._node_fetch[unknown.id] = {"tool": loaded, "error": None}
        made._collect_nodes()
        self.assertEqual(self.tree.rows(), ["Picture", "Film"], "it makes only text: the filter that is on leaves it out from here")
        self.assertEqual(made._show_keys, ["cat:", "cat:Image", "cat:Video"])
        self.assertEqual(made.tool.id, unknown.id, "the card still shows what was picked")

    def test_the_window_opens_with_the_two_filters_as_they_were_left(self):
        made = self.made
        for stored, shown in (({}, (True, False)), ({"media_only": False, "favorites_only": True}, (False, True))):
            made.settings = {**self.saved, **stored}
            made.settings.pop("media_only", None) if "media_only" not in stored else None
            made._load_settings_into_ui()
            self.assertEqual((made.items["MediaOnly"].Checked, made.items["FavoritesOnly"].Checked), shown)


class WhatAPinHolds(Base):
    """Kaitoi's own names for what a pin holds, however a node spelled it."""

    def test_the_spellings_kaitoi_takes_for_a_picture_a_video_sound_and_a_web_page(self):
        for spelled, known in (("tiff", "image"), ("TIF", "image"), ("exr", "image"), ("heic", "image"), ("image_file", "image"), ("Image File", "image"),
                               ("png[]", "image"), ("image?", "image"), ("ogg", "audio"), ("aac", "audio"), ("audio-file", "audio"), ("mov", "video"),
                               ("html", "web"), ("htm", "web"), ("web_file", "web"), ("glb", "3d"), ("report_file", "file"), ("string", "string"), ("text", "string"),
                               ("model_3d", "3d"), ("Model 3D", "3d"), ("", ""), (None, "")):  # written apart, known as one word
            self.assertEqual(tools.normalize_type(spelled), known, spelled)

    def test_a_result_spelled_another_way_is_still_what_resolve_can_place(self):
        self.assertTrue(tools.places_in_resolve(makes("Scan", "tiff")))
        self.assertTrue(tools.places_in_resolve(makes("Voice", "ogg")))
        self.assertTrue(tools.places_in_resolve(makes("Pick", "image_file")))
        self.assertFalse(tools.places_in_resolve(makes("Page", "video", "html")), "a web page under its other name is a web page")

    def test_a_workflow_with_a_web_preview_is_kept_and_a_node_with_one_is_not(self):
        self.assertFalse(tools.places_in_resolve(makes("Color Wheels", "web", "image")))
        self.assertTrue(tools.places_in_resolve(makes("Grade", "web", "image", source="workflow")), "published to run with nobody at a page, and it makes a picture")
        self.assertFalse(tools.places_in_resolve(makes("Page only", "web", source="workflow")), "it still has to make something Resolve can place")


class MoreOnHeights(Base):
    def test_never_fewer_lines_than_the_letters_alone_would_fill(self):
        made = panel(width=440)  # 67 letters to a line
        self.assertEqual(made._text_height("x" * 67 + " " + "y" * 67), 15 * 3 + 2, "two words that each fill a line: with letters wider than the average, a third")
        self.assertEqual(made._text_height("x" * 30 + " " + "y" * 30), 15 + 2)
        import random

        rng = random.Random(7)
        for _ in range(3000):
            text = " ".join("w" * rng.randint(1, 80) for _ in range(rng.randint(1, 12)))
            for width in (300, 440, 700):
                at = panel(width=width)
                per_line = max(8, int((width - 40) / 5.9))
                self.assertGreaterEqual(at._text_height(text), 15 * -(-len(text) // per_line) + 2)

    def test_values_typed_into_a_form_survive_the_other_saves_too(self):
        made = panel()
        made.settings["last_values"] = {"node:Film": {"prompt": "typed, not yet saved"}}
        disk = {key: value for key, value in made.settings.items() if key != "last_values"}

        def update(**changes):
            disk.update(changes)
            return dict(disk)

        with mock.patch.object(config, "update", side_effect=update):
            made._remember_use(makes("Film", "video"))
        self.assertEqual(made.settings["last_values"], {"node:Film": {"prompt": "typed, not yet saved"}})

    def test_saving_the_settings_keeps_what_was_typed_into_a_form(self):
        made = panel(width=440)
        made.settings["last_values"] = {"node:Film": {"prompt": "typed, not yet saved"}}
        made.balance = ""
        disk = {key: value for key, value in made.settings.items() if key != "last_values"}
        made.items["ApiKey"].Text, made.items["BaseUrl"].Text = disk["api_key"], disk["base_url"]
        made.items["MaxHeight"].Text, made.items["RunTimeout"].Text = "720", "900"

        def update(**changes):
            disk.update(changes)
            return dict(disk)

        with mock.patch.object(config, "update", side_effect=update), mock.patch.object(ui.credentials, "source", return_value="a file"), \
                mock.patch.object(ui.Panel, "_base_url", lambda self: disk["base_url"]):
            made._save_settings()
        self.assertEqual(made._shown[("Status", "Text")], "Saved.")
        self.assertEqual(made.settings["last_values"], {"node:Film": {"prompt": "typed, not yet saved"}})


class InputsResolveCannotSupply(Base):
    """A node that makes a picture is no use here if it cannot run without a 3D model."""

    def node(self, *pins, outputs=("image",)):
        return tools.node_from_detail({"type": "builtin/x/render", "title": "Render", "category": "3D", "source": "builtin",
                                       "inputs": [{"name": name, "label": label, "type": kind, "requirement": requirement} for name, label, kind, requirement in pins],
                                       "outputs": [{"name": f"out{at}", "type": kind} for at, kind in enumerate(outputs)]})

    def test_a_node_that_cannot_run_without_one_is_left_out_of_what_works_in_resolve(self):
        needs_model = self.node(("inputModel", "Input Model", "glb", "required"), ("prompt", "Prompt", "string", "required"))
        self.assertEqual(needs_model.needs, ("a 3D model (Input Model)",))
        self.assertFalse(tools.places_in_resolve(needs_model), "it makes a picture, from something Resolve has no way to hand it")
        self.assertEqual([field.name for field in needs_model.inputs], ["prompt"], "the form still holds what it can")
        older = self.node(("inputPDF", "", "pdf", "unspecified"))
        self.assertEqual(older.needs, ("a PDF (inputPDF)",), "an older node that does not say whether it can do without: it promises nothing")
        self.assertFalse(tools.places_in_resolve(older))
        two = self.node(("a", "Scene", "usdz", "required"), ("b", "Look", "file", "required"), ("c", "Scene", "usdz", "required"))
        self.assertEqual(two.needs, ("a 3D model (Scene)", "a file (Look)"), "each once")
        self.assertEqual(self.node(("x", "Cloud", "rerun", "required")).needs, ("rerun input (Cloud)",), "a kind with no name of its own is named as Kaitoi names it")

    def test_one_it_can_do_without_does_not_count(self):
        for requirement in ("optional", "defaulted"):
            kept = self.node(("loras", "LoRAs", "object", requirement), ("prompt", "Prompt", "string", "required"))
            self.assertEqual(kept.needs, (), requirement)
            self.assertTrue(tools.places_in_resolve(kept), requirement)

    def test_what_the_form_or_the_timeline_can_give_is_never_a_reason(self):
        for kind in ("string", "int", "float", "bool", "color", "image", "video", "audio", "png", "mov", "image_file"):
            self.assertEqual(self.node(("x", "X", kind, "required")).needs, (), kind)

    def test_a_server_that_does_not_say_what_is_required_is_not_held_against_the_node(self):
        silent = tools.node_from_detail({"type": "builtin/x/render", "title": "Render", "source": "builtin",
                                         "inputs": [{"name": "inputModel", "type": "glb"}], "outputs": [{"name": "out", "type": "image"}]})
        self.assertEqual(silent.needs, ())
        self.assertTrue(tools.places_in_resolve(silent))

    def test_it_is_remembered_with_the_node_list_between_sessions(self):
        needs_model = self.node(("inputModel", "Input Model", "glb", "required"), ("prompt", "Prompt", "string", "required"))
        back = tools.node_from_summary(tools._node_row(needs_model))
        self.assertEqual(back.needs, ("a 3D model (Input Model)",), "the stored list keeps no pin the form cannot hold, so it keeps this beside them")
        self.assertFalse(tools.places_in_resolve(back))
        self.assertEqual(tools.node_from_summary(tools._node_row(back)).needs, back.needs, "and it does not grow each time it is stored")

    def test_a_workflow_is_judged_by_what_it_makes_only(self):
        self.assertTrue(tools.places_in_resolve(makes("Grade", "video", source="workflow")))

    def test_with_the_filter_off_the_card_says_what_such_a_node_needs(self):
        made = panel(width=440)
        made._chosen = {}
        made.tool = self.node(("inputModel", "Input Model", "glb", "required"))
        with mock.patch.object(ui.Panel, "_want_state", lambda self, tool: None), mock.patch.object(ui.Panel, "_show_thumbnail", lambda self, tool: None), \
                mock.patch.object(ui.Panel, "_sends_line", lambda self: "Takes nothing from the timeline."):
            made._show_detail()
        self.assertIn("Needs a 3D model (Input Model), which cannot be supplied from Resolve.", made._shown[("DetailText", "Text")])
        self.assertNotIn("Takes nothing from the timeline", made._shown[("DetailText", "Text")], "which would be true and beside the point")

    def test_the_filter_is_named_for_what_it_means(self):
        with open(ui.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('{"ID": "MediaOnly", "Text": "Works in Resolve",', source)
        self.assertNotIn("Image, video or sound results", source)


class SessionAndAgentNodes(Base):
    """A node a person takes part in live, and a node that works with others around it, are for Studio."""

    def node(self, **marks):
        return tools.node_from_summary({"type": "builtin/audio/talk", "title": "Talk", "category": "Audio", "source": "builtin",
                                        "inputs": [], "outputs": [{"name": "audio", "type": "audio"}], **marks})

    def test_neither_works_in_resolve_whatever_it_makes(self):
        self.assertTrue(tools.places_in_resolve(self.node()))
        self.assertFalse(tools.places_in_resolve(self.node(session=True)), "it makes sound, in a conversation held in Studio")
        self.assertFalse(tools.places_in_resolve(self.node(agent=True)))
        self.assertEqual((self.node(session=True).session, self.node(agent=True).agent), (True, True))

    def test_only_a_plain_yes_from_the_server_counts(self):
        for said in (None, False, "true", 1, {}, "yes"):
            node = self.node(session=said, agent=said)
            self.assertEqual((node.session, node.agent), (False, False), repr(said))
            self.assertTrue(tools.places_in_resolve(node))

    def test_it_is_known_before_the_nodes_pins_are(self):
        unloaded = tools.node_from_summary({"type": "builtin/audio/talk", "title": "Talk", "source": "builtin", "session": True})
        self.assertFalse(unloaded.ready)
        self.assertFalse(tools.places_in_resolve(unloaded), "a server that lists no pins still says what kind of node it is")
        self.assertTrue(tools.places_in_resolve(tools.node_from_summary({"type": "builtin/audio/talk", "title": "Talk", "source": "builtin"})))

    def test_a_workflow_is_not_judged_by_marks_it_cannot_have(self):
        flow = tools.Tool(id="workflow:x", source="workflow", name="X", description="", category="W", tags=(), inputs=(),
                          outputs=(tools.Output("out", "Out", "video"),), session=True, agent=True)
        self.assertTrue(tools.places_in_resolve(flow))

    def test_it_is_remembered_with_the_node_list(self):
        for marks in ({"session": True}, {"agent": True}, {"session": True, "agent": True}, {}):
            node = self.node(**marks)
            back = tools.node_from_summary(tools._node_row(node))
            self.assertEqual((back.session, back.agent), (node.session, node.agent), marks)
        self.assertNotIn("session", tools._node_row(self.node()), "nothing is written for a node that is neither")

    def test_with_the_filter_off_the_card_says_where_such_a_node_is_used(self):
        for marks, said in (({"session": True}, "A live session node, made for Kaitoi Studio. It may not work from Resolve."),
                            ({"agent": True}, "An agent node, made to work with other nodes in Kaitoi Studio. It may not work from Resolve.")):
            made = panel(width=440)
            made._chosen = {}
            made.tool = self.node(**marks)
            with mock.patch.object(ui.Panel, "_want_state", lambda self, tool: None), mock.patch.object(ui.Panel, "_show_thumbnail", lambda self, tool: None), \
                    mock.patch.object(ui.Panel, "_sends_line", lambda self: "Takes nothing from the timeline."):
                made._show_detail()
            self.assertIn(said, made._shown[("DetailText", "Text")])


class FakeSignIn:
    """A sign-in whose answer the test decides."""

    made: list = []

    def __init__(self, base_url, device=""):
        self.base_url = base_url
        self.device = device
        self.answer = {"state": ui.signin.WAITING, "message": "", "result": {}, "opened": False}
        self.cancelled = False
        self.starts = True
        FakeSignIn.made.append(self)

    def start(self):
        return self.starts

    def cancel(self):
        self.cancelled = True
        self.answer = {"state": ui.signin.CANCELLED, "message": "Sign-in cancelled.", "result": {}}

    def poll(self):
        return dict(self.answer)


class SigningIn(Base):
    def setUp(self):
        super().setUp()
        FakeSignIn.made = []
        self.disk = {}
        self.switched = []

        def update(**changes):
            self.disk.update(changes)
            return dict(self.disk)

        # What is in Resolve's Effects Library, and what was done to it.
        self.effects = {}
        self.synced = []
        self.reread = []

        def sync(given, *, icons=None, prefix="workflow:", keep=()):
            self.synced.append((list(given), prefix, set(keep)))
            for tool_id in [tool_id for tool_id in self.effects if tool_id.startswith(prefix) and tool_id not in self.stuck]:
                del self.effects[tool_id]
            return 0

        self.stuck = set()
        for patcher in (mock.patch.object(config, "update", side_effect=update), mock.patch.object(ui.signin, "SignIn", FakeSignIn),
                        mock.patch.object(ui.Panel, "_account_switched", lambda panel, before: self.switched.append(before)),
                        mock.patch.object(ui.credentials, "env_api_key", lambda: ""),
                        mock.patch.object(ui.templates, "installed", lambda: dict(self.effects)),
                        mock.patch.object(ui.templates, "sync", side_effect=sync),
                        mock.patch.object(ui.Panel, "_refresh_installed", lambda panel, redraw=True: self.reread.append(redraw)),
                        mock.patch.object(ui.signin.platform, "node", return_value="Studio-2.local")):
            patcher.start(); self.addCleanup(patcher.stop)
        self.made = panel(settings={"api_key": "", "base_url": "https://api.example.test"}, width=440)
        self.made.balance = ""
        self.disk.update(self.made.settings)
        self.addCleanup(ui.busy.release, self.made._owner)  # a sign-in left waiting holds the marker a run holds

    def answer(self, state, message="", **result):
        FakeSignIn.made[-1].answer = {"state": state, "message": message, "result": result}

    def test_pressing_the_button_opens_the_sign_in_for_the_server_in_use_and_waits(self):
        made = self.made
        made._sign_in_pressed()
        self.assertEqual([flow.base_url for flow in FakeSignIn.made], ["https://api.example.test"])
        self.assertEqual(made._shown[("SignIn", "Text")], ui.SIGN_IN_WAITING)
        self.assertEqual(made._shown[("Status", "Text")], ui.SIGN_IN_ASKING, "the browser is not open yet: the server is asked first")
        self.assertTrue(made._collect_sign_in(), "still waiting: the window keeps ticking")
        self.assertEqual(made._shown[("Status", "Text")], ui.SIGN_IN_ASKING)
        self.assertEqual(self.disk.get("api_key"), "", "nothing is saved while it waits")

    def test_the_browser_is_said_to_be_open_only_once_it_is(self):
        made = self.made
        made._sign_in_pressed()
        self.assertNotIn("browser has opened", made._shown[("Status", "Text")])
        FakeSignIn.made[-1].answer["opened"] = True
        self.assertTrue(made._collect_sign_in())
        self.assertEqual(made._shown[("Status", "Text")], ui.SIGN_IN_OPENED)
        self.assertIn("Sign in there", ui.SIGN_IN_OPENED)
        made.status("Something else said since.")
        self.assertTrue(made._collect_sign_in())
        self.assertEqual(made._shown[("Status", "Text")], "Something else said since.", "said once, not at every tick")
        # A second sign-in says it again when its own browser opens.
        made._sign_in_pressed(); made._collect_sign_in(); made._sign_in_pressed()
        self.assertEqual(made._shown[("Status", "Text")], ui.SIGN_IN_ASKING)
        FakeSignIn.made[-1].answer["opened"] = True
        made._collect_sign_in()
        self.assertEqual(made._shown[("Status", "Text")], ui.SIGN_IN_OPENED)

    def test_a_server_that_does_not_sign_plugins_in_is_never_said_to_have_opened_the_browser(self):
        made = self.made
        made._sign_in_pressed()
        said = [made._shown[("Status", "Text")]]
        self.answer(ui.signin.FAILED, ui.signin.NOT_OFFERED)
        made._collect_sign_in()
        said.append(made._shown[("Status", "Text")])
        self.assertEqual(said, [ui.SIGN_IN_ASKING, ui.signin.NOT_OFFERED])

    def test_a_finished_sign_in_is_saved_shown_and_starts_the_new_account(self):
        made = self.made
        made.settings["last_values"] = {"node:Film": {"prompt": "typed, not yet saved"}}
        made._sign_in_pressed()
        self.answer(ui.signin.DONE, api_key="key-abcdef123456", user="jo", email="jo@example.test")
        self.assertFalse(made._collect_sign_in())
        self.assertEqual((self.disk["api_key"], self.disk["base_url"], self.disk["signed_in_as"]), ("key-abcdef123456", "https://api.example.test", "jo"))
        self.assertEqual(made.items["ApiKey"].Text, "key-abcdef123456")
        self.assertEqual(made._shown[("SignIn", "Text")], ui.SIGN_IN)
        self.assertEqual(made._shown[("Status", "Text")], "Signed in as jo.")
        self.assertEqual(self.switched, [("", "https://api.example.test")], "the lists are loaded again for the account that signed in")
        self.assertEqual(made.settings["last_values"], {"node:Film": {"prompt": "typed, not yet saved"}})
        self.assertEqual(self.disk["signed_in_key_end"], "123456", "only its last letters, to tell it from a key pasted in later")
        self.assertFalse(made._collect_sign_in(), "taken once")
        with mock.patch.object(ui.credentials, "source", return_value="/Users/someone/.kaitoi/credentials.json"):
            self.assertEqual(made._account_info(), "Signed in as jo.")
            made.balance = "6,275 credits"
            self.assertEqual(made._account_info(), "Signed in as jo.  6,275 credits.")

    def test_the_key_is_saved_for_the_server_that_gave_it(self):
        made = self.made
        made._sign_in_pressed()
        # Another server is saved while the browser is still open on the first one's sign-in page.
        made.settings["base_url"] = self.disk["base_url"] = "http://localhost:8000"
        self.answer(ui.signin.DONE, api_key="key-abcdef123456", user="jo")
        made._collect_sign_in()
        self.assertEqual(self.disk["base_url"], "https://api.example.test", "a key is no use on a server that did not make it")
        self.assertEqual(made.items["BaseUrl"].Text, "https://api.example.test")

    def test_a_key_pasted_in_since_is_not_called_the_persons_sign_in(self):
        made = self.made
        made._sign_in_pressed()
        self.answer(ui.signin.DONE, api_key="key-abcdef123456", user="jo")
        made._collect_sign_in()
        with mock.patch.object(ui.credentials, "source", return_value="/Users/someone/.kaitoi/credentials.json"):
            made.settings["api_key"] = "another-key-pasted-in"
            self.assertEqual(made._account_info(), "Using the key from /Users/someone/.kaitoi/credentials.json.")
            made.settings["api_key"] = "key-abcdef123456"
            with mock.patch.object(ui.credentials, "env_api_key", lambda: "a key from the environment"):
                self.assertNotIn("Signed in as", made._account_info(), "the key in use is the environment's then")

    def test_a_sign_in_that_does_not_finish_changes_nothing_and_says_why(self):
        for state, message in ((ui.signin.FAILED, "This account does not have access to Kaitoi yet."), (ui.signin.CANCELLED, "Sign-in cancelled."), (ui.signin.FAILED, "")):
            made = self.made
            made._sign_in_pressed()
            self.answer(state, message)
            self.assertFalse(made._collect_sign_in())
            self.assertEqual(made._shown[("Status", "Text")], message or "Not signed in.")
            self.assertEqual(made._shown[("SignIn", "Text")], ui.SIGN_IN)
            self.assertEqual((self.disk.get("api_key"), self.disk.get("signed_in_as")), ("", None))
            self.assertEqual(self.switched, [])

    def test_pressing_the_button_again_calls_the_waiting_sign_in_off(self):
        made = self.made
        made._sign_in_pressed()
        made._sign_in_pressed()
        self.assertEqual(len(FakeSignIn.made), 1, "not a second one beside it")
        self.assertTrue(FakeSignIn.made[0].cancelled)
        made._collect_sign_in()
        self.assertEqual(made._shown[("Status", "Text")], "Sign-in cancelled.")
        made._sign_in_pressed()
        self.assertEqual(len(FakeSignIn.made), 2, "and then a new one can be started")

    def test_a_browser_that_will_not_open_is_said_at_the_next_tick(self):
        made = self.made
        with mock.patch.object(FakeSignIn, "start", lambda flow: False):
            made._sign_in_pressed()
        self.answer(ui.signin.FAILED, "Could not open a browser on this computer.")
        made._collect_sign_in()
        self.assertEqual(made._shown[("Status", "Text")], "Could not open a browser on this computer.")
        self.assertEqual(made._shown[("SignIn", "Text")], ui.SIGN_IN)

    def test_closing_the_window_calls_a_waiting_sign_in_off(self):
        made = self.made
        made._sign_in_pressed()
        with mock.patch.object(ui.Panel, "_save_geometry", lambda panel: None, create=True):
            try:
                made._on_close()
            except Exception:  # noqa: BLE001 - the rest of closing needs a real window
                pass
        self.assertTrue(FakeSignIn.made[0].cancelled)

    def test_signing_out_forgets_the_key_on_this_computer(self):
        made = self.made
        made._sign_in_pressed()
        self.answer(ui.signin.DONE, api_key="key-abcdef123456", user="jo")
        made._collect_sign_in()
        self.switched.clear()
        made.settings["last_values"] = {"node:Film": {"prompt": "kept"}}
        made._sign_out()
        self.assertEqual((self.disk["api_key"], self.disk["signed_in_as"], self.disk["signed_in_key_end"], self.disk["signed_in_server"]), ("", "", "", ""))
        self.assertEqual(made.items["ApiKey"].Text, "")
        self.assertEqual(made._shown[("Status", "Text")], "Signed out on this computer.")
        self.assertEqual(self.switched, [("key-abcdef123456", "https://api.example.test")])
        self.assertEqual(made.settings["last_values"], {"node:Film": {"prompt": "kept"}})

    def test_signing_out_takes_the_accounts_workflows_out_of_the_effects_library(self):
        made = self.signed_in()
        self.effects.update({"workflow:e1": {}, "workflow:e2": {}, "node:builtin/film": {}})
        made._sign_out()
        self.assertEqual(sorted(self.effects), ["node:builtin/film"], "a node the person installed stays")
        self.assertEqual(self.synced, [([], "workflow:", set())])
        self.assertEqual(self.reread, [False], "what the window shows as installed is read again")
        self.assertEqual(made._shown[("Status", "Text")], "Signed out on this computer. Your 2 workflows were removed from Resolve's Effects Library.")
        self.assertEqual(self.disk["api_key"], "")
        self.assertEqual(self.switched, [("key-abcdef123456", "https://api.example.test")])

    def test_one_workflow_is_said_as_one_and_none_is_not_mentioned(self):
        made = self.signed_in()
        self.effects.update({"workflow:e1": {}, "node:builtin/film": {}})
        made._sign_out()
        self.assertEqual(made._shown[("Status", "Text")], "Signed out on this computer. Your workflow was removed from Resolve's Effects Library.")
        made = self.signed_in()
        self.synced.clear(); self.reread.clear()
        made._sign_out()
        self.assertEqual(made._shown[("Status", "Text")], "Signed out on this computer.")
        self.assertEqual((self.synced, self.reread, sorted(self.effects)), ([], [], ["node:builtin/film"]), "nothing to remove: Resolve's folder is left alone")

    def test_a_workflow_that_will_not_go_is_said_and_does_not_hold_the_sign_out_up(self):
        made = self.signed_in()
        self.effects.update({"workflow:e1": {}, "workflow:e2": {}, "workflow:e3": {}})
        self.stuck = {"workflow:e2"}
        made._sign_out()
        self.assertEqual(made._shown[("Status", "Text")], "Signed out on this computer. 2 of your 3 workflows were removed from Resolve's Effects Library.")
        self.assertEqual(self.disk["api_key"], "")
        made = self.signed_in()
        with mock.patch.object(ui.templates, "sync", side_effect=OSError("the folder is locked")):
            made._sign_out()
        self.assertEqual(made._shown[("Status", "Text")], "Signed out on this computer. Your workflows could not be removed from Resolve's Effects Library.")
        self.assertEqual((self.disk["api_key"], self.disk["signed_in_as"]), ("", ""))
        self.assertEqual(self.switched[-1], ("key-abcdef123456", "https://api.example.test"))

    def test_no_sign_in_is_started_while_something_runs_on_the_key_in_use(self):
        """A new sign-in ends this computer's key on Kaitoi: a run using it would be paid for and its result never fetched."""
        made = self.signed_in()
        count = len(FakeSignIn.made)
        for name, busy_with in (("job", object()), ("_bulk", {"started": 0})):
            setattr(made, name, busy_with)
            made._sign_in_pressed()
            self.assertEqual((len(FakeSignIn.made), made._shown[("Status", "Text")]), (count, ui.SIGN_IN_BUSY), name)
            self.assertNotEqual(made._shown.get(("SignIn", "Text")), ui.SIGN_IN_WAITING)
            setattr(made, name, None)
        with mock.patch.object(ui.busy, "held_by_other", return_value="a run from the Inspector"):
            made._sign_in_pressed()
        self.assertEqual(len(FakeSignIn.made), count, "nor while a clip's Generate is running in another script")
        made._sign_in_pressed()
        self.assertEqual(len(FakeSignIn.made), count + 1, "and with nothing running it starts")

    def test_nothing_is_started_in_this_window_while_a_sign_in_waits(self):
        """Finishing the sign-in in the browser ends the key a run started meanwhile would be using."""
        made = self.signed_in()
        made.tool = makes("Film grain", "video")
        made._listed_all, made._installed = [makes("Upscale", "image")], {}
        started, installs = [], []
        with mock.patch.object(ui.Panel, "_start_job", lambda panel, tool: started.append(tool.name) or True), \
                mock.patch.object(ui.templates, "install_many", lambda *args, **kwargs: installs.append(1)):
            made._sign_in_pressed()
            made._on_generate()
            self.assertEqual((started, made._shown[("Status", "Text")]), ([], ui.SIGN_IN_FIRST))
            made.status("")
            made._install_all()
            self.assertEqual((made._bulk, made._shown[("Status", "Text")]), (None, ui.SIGN_IN_FIRST))
            self.answer(ui.signin.CANCELLED, "Sign-in cancelled.")
            made._collect_sign_in()
            made._on_generate()
            self.assertEqual(started, ["Film grain"], "and once it is over, a run starts as ever")
            ui.busy.release(made._owner)
            made._install_all()
            self.assertIsNotNone(made._bulk)
            made._bulk["stop"].set()
        self.assertIn("Finish it or cancel it", ui.SIGN_IN_FIRST)

    def test_a_waiting_sign_in_is_seen_by_the_other_kaitoi_scripts_as_a_run_is(self):
        """A Generate pressed in the Inspector is another script: all it shares with this window is the marker."""
        made = self.made
        made._sign_in_pressed()
        self.assertEqual((ui.busy.holder() or {}).get("what"), "a sign-in")
        self.assertEqual(ui.busy.held_by_other("a script started from the Inspector"), "a sign-in")
        self.assertFalse(ui.busy.acquire("a script started from the Inspector", "a run"))
        with mock.patch.object(ui.busy, "renew") as renew:
            self.assertTrue(made._collect_sign_in())
            renew.assert_called_once_with(made._owner)  # or the marker would look left behind after ninety seconds
        for state in (ui.signin.DONE, ui.signin.FAILED, ui.signin.CANCELLED):
            if FakeSignIn.made[-1].answer["state"] != ui.signin.WAITING:
                made._sign_in_pressed()
                self.assertIsNotNone(ui.busy.holder())
            self.answer(state, "", api_key="key-abcdef123456", user="jo")
            made._collect_sign_in()
            self.assertIsNone(ui.busy.holder(), state)

    def test_a_sign_in_is_not_started_when_the_marker_cannot_be_had(self):
        made = self.made
        with mock.patch.object(ui.busy, "acquire", return_value=False):  # another script took it this instant
            made._sign_in_pressed()
        self.assertEqual((FakeSignIn.made, made._shown[("Status", "Text")]), ([], ui.SIGN_IN_BUSY))

    def test_a_waiting_sign_in_can_still_be_called_off_while_something_runs(self):
        made = self.made
        made._sign_in_pressed()
        made.job = object()
        made._sign_in_pressed()
        self.assertTrue(FakeSignIn.made[0].cancelled)

    def loads(self, made, error):
        loader = FinishedLoader()
        loader.take = lambda: {"tools": None, "snapshot": None, "error": error, "balance": None, "nodes": None, "thumbnails": []}
        made.loader = loader
        with mock.patch.object(ui.Panel, "_update_list_info", lambda panel, shown: None):
            made._collect_loader()
        return made._shown[("Status", "Text")]

    def test_a_sign_in_kaitoi_no_longer_takes_is_said_as_that(self):
        made = self.signed_in()
        self.assertEqual(self.loads(made, "[401] UNAUTHORIZED Invalid API key"), ui.SIGN_IN_ENDED)
        self.assertIn("Sign in with Kaitoi", ui.SIGN_IN_ENDED)
        self.assertEqual(self.loads(made, "[503] UNAVAILABLE Try later"), "Could not load workflows: [503] UNAVAILABLE Try later")
        # A key the person pasted in is theirs to look after: Kaitoi's own words are shown.
        made.settings["api_key"] = self.disk["api_key"] = "a-key-pasted-from-studio"
        self.assertEqual(self.loads(made, "[401] UNAUTHORIZED Invalid API key"), "Could not load workflows: [401] UNAUTHORIZED Invalid API key")

    def test_a_sign_out_that_is_refused_removes_nothing(self):
        made = self.made
        self.effects.update({"workflow:e1": {}})
        made.settings["api_key"] = self.disk["api_key"] = "a-key-pasted-from-studio"
        made._sign_out()
        made = self.signed_in()
        with mock.patch.object(ui.credentials, "env_api_key", lambda: "from-the-environment"):
            made._sign_out()
        self.assertEqual((sorted(self.effects), self.synced), (["workflow:e1"], []))

    def test_signing_in_does_not_touch_the_effects_library(self):
        self.effects.update({"workflow:e1": {}})
        self.signed_in()
        self.assertEqual((sorted(self.effects), self.synced), (["workflow:e1"], []), "the account's own list decides, once it is loaded")

    def test_the_sign_in_says_what_to_call_this_computers_key(self):
        made = self.made
        made._sign_in_pressed()
        device = FakeSignIn.made[-1].device
        self.assertRegex(device, r"^Studio-2 [0-9a-f]{4}$")
        self.assertEqual(self.disk["computer_mark"], device[-4:], "the mark is kept, so that the next sign-in here names the same key")
        made._sign_in_pressed()  # called off
        made._collect_sign_in()
        made._sign_in_pressed()
        self.assertEqual(FakeSignIn.made[-1].device, device, "the same name every time: that is what a new sign-in replaces")

    def test_the_mark_is_this_installs_own_and_a_spoiled_one_is_made_again(self):
        made = self.made
        made.settings["computer_mark"] = self.disk["computer_mark"] = "01bc"
        made.settings["last_values"] = {"node:Film": {"prompt": "typed, not yet saved"}}
        self.assertEqual(made._computer(), "Studio-2 01bc")
        for spoiled in ("", "01BC", "01bcd", "zz", None, 7):
            made.settings["computer_mark"] = self.disk["computer_mark"] = spoiled
            name = made._computer()
            self.assertRegex(name, r"^Studio-2 [0-9a-f]{4}$", spoiled)
            self.assertEqual(self.disk["computer_mark"], name[-4:])
            self.assertEqual(made._computer(), name)
        self.assertEqual(made.settings["last_values"], {"node:Film": {"prompt": "typed, not yet saved"}}, "making the mark loses nothing typed")
        marks = set()
        for _ in range(20):
            made.settings["computer_mark"] = ""
            marks.add(made._computer())
        self.assertGreater(len(marks), 10, "made by chance, not from anything two computers could share")

    def test_a_mark_that_cannot_be_kept_does_not_stop_the_sign_in(self):
        made = self.made
        with mock.patch.object(config, "update", side_effect=OSError("the disk is full")):
            self.assertRegex(made._computer(), r"^Studio-2 [0-9a-f]{4}$")

    def signed_in(self):
        made = self.made
        made._sign_in_pressed()
        self.answer(ui.signin.DONE, api_key="key-abcdef123456", user="jo")
        made._collect_sign_in()
        self.switched.clear()
        return made

    def test_a_pasted_key_is_not_this_buttons_to_throw_away(self):
        """Kaitoi Studio shows a key once. Sign out only forgets a key that a sign-in here gave."""
        made = self.made
        made.settings["api_key"] = self.disk["api_key"] = "a-key-pasted-from-studio"
        made._sign_out()
        self.assertEqual(self.disk["api_key"], "a-key-pasted-from-studio")
        self.assertIn("clear the API key box and save", made._shown[("Status", "Text")])
        self.assertEqual(self.switched, [])
        made = self.signed_in()
        made.settings["api_key"] = self.disk["api_key"] = "another-key-pasted-since"
        made._sign_out()
        self.assertEqual(self.disk["api_key"], "another-key-pasted-since", "nor one pasted in after signing in")

    def test_a_key_set_in_the_environment_is_used_whatever_is_done_here(self):
        made = self.signed_in()
        with mock.patch.object(ui.credentials, "env_api_key", lambda: "from-the-environment"):
            made._sign_out()
            self.assertEqual(self.disk["api_key"], "key-abcdef123456", "not this window's to remove")
            self.assertEqual(self.switched, [])
            made._sign_in_pressed()
            self.assertEqual(len(FakeSignIn.made), 1, "and no sign-in is started that would change nothing")
            self.assertIn("environment", made._shown[("Status", "Text")])
            self.assertNotEqual(made._shown.get(("SignIn", "Text")), ui.SIGN_IN_WAITING)

    def test_the_same_key_pointed_at_another_server_is_not_a_sign_in(self):
        made = self.signed_in()
        with mock.patch.object(ui.credentials, "source", return_value="/Users/someone/.kaitoi/credentials.json"):
            self.assertEqual(made._account_info(), "Signed in as jo.")
            made.settings["base_url"] = "http://localhost:8000"  # saved on the Account tab since
            self.assertEqual(made._account_info(), "Using the key from /Users/someone/.kaitoi/credentials.json.", "that server never signed anyone in")
            self.assertFalse(made._signed_in())

    def test_sign_out_is_offered_only_to_someone_signed_in_here(self):
        made = self.made
        with mock.patch.object(ui.credentials, "source", return_value="not set"):
            made._show_account_info()
            self.assertEqual(made._shown[("SignOut", "Enabled")], False)
            self.assertIn("Not signed in", made._account_info())
        made.settings["api_key"] = "a-key-pasted-from-studio"
        with mock.patch.object(ui.credentials, "source", return_value="/Users/someone/.kaitoi/credentials.json"):
            made._show_account_info()
            self.assertEqual(made._shown[("SignOut", "Enabled")], False, "a pasted key is cleared in its own box")
        made = self.signed_in()
        with mock.patch.object(ui.credentials, "source", return_value="/Users/someone/.kaitoi/credentials.json"):
            made._show_account_info()
            self.assertEqual(made._shown[("SignOut", "Enabled")], True)
            with mock.patch.object(ui.credentials, "env_api_key", lambda: "k"):
                made._show_account_info()
                self.assertEqual(made._shown[("SignOut", "Enabled")], False)


class Uninstalling(Base):
    """The Uninstall button: what the window does around the uninstaller, which is stood in for here."""

    GONE = "Kaitoi was removed from this Mac.\nRestart DaVinci Resolve to clear it from the menus."

    def setUp(self):
        super().setUp()
        self.addCleanup(config.leave_gone, False)
        self.made = panel()
        self.addCleanup(busy.release, self.made._owner)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # A file that is really there: the window tells an uninstall that happened from one that did not by whether it still is.
        self.script = os.path.join(folder.name, "Uninstall Kaitoi.command")
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write("never run: uninstall.run is stood in for")
        self.made._uninstaller = self.script
        self.made.balance = ""
        self.made.view = ui.VIEW_ACCOUNT
        self.ran = []
        self.hold = threading.Event()  # the password is being asked for until this is set
        self.hold.set()
        self.answer = (uninstall.REMOVED, [])

        def run(path, *, settings):
            self.ran.append((path, settings))
            self.assertTrue(self.hold.wait(10))
            if self.deletes_itself:
                os.remove(path)  # as the real one does: it is in the folder it deletes
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        self.deletes_itself = False

        stood_in = mock.patch.object(uninstall, "run", run)
        stood_in.start(); self.addCleanup(stood_in.stop)
        # Nothing else of the window may move while Kaitoi is being removed, or after.
        for name in ("_collect_loader", "_follow_timeline", "_save_geometry", "_read_columns"):
            still = mock.patch.object(ui.Panel, name, side_effect=AssertionError(name + " ran"))
            still.start(); self.addCleanup(still.stop)

    def finish(self):
        """Let the uninstaller answer, and the window take the answer."""
        state = self.made._uninstall
        self.hold.set()
        self.assertTrue(state["done"].wait(10))
        return self.made._on_tick()

    def text(self):
        return self.made._shown.get(("UninstallText", "Text"))

    def stage(self):
        return self.made.items["UninstallStage"].CurrentIndex

    def nav(self):
        """Whether each tab can be pressed. One that was never switched off can."""
        return [self.made.items[button_id]._values.get("Enabled", True) for button_id, _, _, _ in ui.NAV]

    def on_show(self):
        """The view the window is on, as it believes and as it drew it."""
        self.assertEqual(self.made.items["Views"].CurrentIndex, self.made.view)
        return self.made.view

    def test_the_button_only_asks_and_cancel_goes_back(self):
        made = self.made
        made.items["UninstallSettings"].Checked = True  # left ticked by a question that was cancelled
        made._uninstall_pressed()
        self.assertEqual(self.on_show(), ui.VIEW_UNINSTALL)
        self.assertEqual(self.stage(), ui.STAGE_ASK)
        self.assertTrue(self.text().startswith("Uninstall Kaitoi?"))
        self.assertIn("so it stops working for every account", self.text())
        self.assertIn("Your results and your Kaitoi sign-in stay.", self.text())
        self.assertFalse(made.items["UninstallSettings"].Checked, "settings are kept unless ticked this time")
        self.assertGreater(made._shown[("UninstallText", "MinimumSize")][1], 5 * 15, "room for every line: a wrapping label is one line high by itself")
        self.assertEqual(self.nav(), [True] * len(ui.NAV), "a tab still leads away: nothing has started")
        made._uninstall_cancelled()
        self.assertEqual(self.on_show(), ui.VIEW_ACCOUNT)
        self.assertEqual(self.ran, [], "nothing was removed by asking")
        self.assertFalse(config._quiet)

    def test_it_waits_for_whatever_kaitoi_is_still_doing_and_says_what(self):
        made = self.made

        class Working:
            running = True

        class Landed:
            """A run whose result has arrived and is placed on the next tick: paid for, and not yet on the timeline."""
            running = False

        for name, value, why in (
            ("job", Working(), "A run is still going. Wait for it or cancel it, then uninstall."),
            ("job", Landed(), "A run is still going. Wait for it or cancel it, then uninstall."),
            ("_bulk", {"stop": threading.Event()}, "An install is still running. Stop it first, then uninstall."),
            ("_signin", object(), "A sign-in is still waiting. Finish it first, then uninstall."),
            ("loader", Working(), "Kaitoi is still loading your workflows. Try again in a moment."),
        ):
            setattr(made, name, value)
            made._uninstall_pressed()
            self.assertEqual(made.view, ui.VIEW_ACCOUNT, name)
            self.assertEqual(made.items["Status"].Text, why)
            # And when it started after the question was put:
            setattr(made, name, None)
            made.status("")
            made._uninstall_pressed()
            self.assertEqual(made.view, ui.VIEW_UNINSTALL)
            setattr(made, name, value)
            made._uninstall_confirmed()
            self.assertEqual(self.ran, [], name)
            self.assertIsNone(made._uninstall)
            self.assertEqual(self.on_show(), ui.VIEW_ACCOUNT)
            self.assertEqual(made.items["Status"].Text, why)
            setattr(made, name, None)
            made.status("")
        self.assertFalse(config._quiet)
        self.assertIsNone(busy.holder(), "and nothing was announced to the other scripts")
        self.assertEqual(self.nav(), [True] * len(ui.NAV))

    def test_it_waits_for_a_run_started_from_the_inspector_too(self):
        """That run belongs to another script, which this window only knows of by the marker it leaves."""
        made = self.made
        why = "A run started from the Inspector is still going. Wait for it, then uninstall."
        self.assertTrue(busy.acquire("the script of a Generate in the Inspector", "Rain"))
        self.addCleanup(busy.release, "the script of a Generate in the Inspector")
        made._uninstall_pressed()
        self.assertEqual(made.view, ui.VIEW_ACCOUNT)
        self.assertEqual(made.items["Status"].Text, why)
        made.view = ui.VIEW_UNINSTALL  # the question was put before that run began
        made._uninstall_confirmed()
        self.assertEqual(self.ran, [])
        self.assertIsNone(made._uninstall)
        self.assertEqual(busy.holder()["what"], "Rain", "the run's own marker is left alone")

    def test_no_run_can_start_from_the_inspector_while_kaitoi_is_being_removed(self):
        """The window holds the marker a run holds, and keeps it fresh however long the password takes."""
        made = self.made
        self.hold.clear()
        made._uninstall_pressed()
        made._uninstall_confirmed()
        self.assertEqual(busy.held_by_other("the script of a Generate in the Inspector"), "the uninstaller")
        self.assertFalse(busy.acquire("the script of a Generate in the Inspector", "Rain"), "a Generate pressed now is refused")
        with mock.patch.object(ui.busy, "renew") as renewed:
            self.assertTrue(made._on_tick())
        renewed.assert_called_once_with(made._owner)
        self.answer = (uninstall.NOTHING, [])
        self.finish()
        self.assertIsNone(busy.holder(), "let go when the password was not given")
        self.hold.set()
        self.answer = (uninstall.REMOVED, [])
        made._uninstall_pressed()
        made._uninstall_confirmed()
        self.finish()
        self.assertTrue(made._removed)
        self.assertIsNone(busy.holder(), "and when Kaitoi is gone")

    def test_nothing_starts_unless_the_question_is_on_show(self):
        made = self.made
        for view in (ui.VIEW_ACCOUNT, ui.VIEW_BROWSE, ui.VIEW_RUN, ui.VIEW_HISTORY):
            made.view = view
            made._uninstall_confirmed()
        self.assertEqual(self.ran, [])
        self.assertIsNone(made._uninstall)
        self.assertFalse(config._quiet)

    def test_while_the_password_is_asked_for_the_window_holds_one_message_and_writes_nothing(self):
        made = self.made
        self.hold.clear()
        made._uninstall_pressed()
        made._uninstall_confirmed()
        self.assertEqual(self.on_show(), ui.VIEW_UNINSTALL)
        self.assertEqual(self.text(), "Removing Kaitoi. macOS asks for your password.")
        self.assertEqual(self.stage(), ui.STAGE_WAIT, "no button under it: neither a second Uninstall nor a Close")
        self.assertEqual(self.nav(), [False] * len(ui.NAV), "no tab leads away from it")
        self.assertTrue(config._quiet, "what the uninstaller deletes is not put back behind it")
        self.assertTrue(made._on_tick(), "still waiting; and nothing else of the window ran, or setUp's stand-ins would have said so")
        made._uninstall_confirmed()
        made._uninstall_cancelled()
        self.assertEqual(len(self.ran), 1, "a second press starts no second uninstaller")
        self.assertEqual(self.on_show(), ui.VIEW_UNINSTALL, "and Cancel does not lead away once it has started")
        made.closed = False
        made._on_close()
        self.assertTrue(made.closed, "closed while it waits, the window saves nothing")
        self.finish()

    def test_when_kaitoi_is_gone_the_window_says_so_and_can_only_be_closed(self):
        made = self.made
        made._uninstall_pressed()
        made._uninstall_confirmed()
        self.assertFalse(self.finish())
        self.assertEqual(self.ran, [(self.script, False)])
        self.assertTrue(made._removed)
        self.assertEqual(self.text(), self.GONE)
        self.assertEqual(self.stage(), ui.STAGE_GONE)
        self.assertEqual(self.nav(), [False] * len(ui.NAV))
        self.assertEqual(self.on_show(), ui.VIEW_UNINSTALL)
        self.assertTrue(config._quiet, "and stays quiet: its folder is not made again")
        self.assertFalse(made._on_tick(), "nothing moves afterwards either")
        made._uninstall_confirmed()
        made._uninstall_cancelled()
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(self.on_show(), ui.VIEW_UNINSTALL)
        made.closed = False
        made._on_close()
        self.assertTrue(made.closed)

    def test_the_tick_box_also_removes_settings_and_history(self):
        made = self.made
        made._uninstall_pressed()
        made.items["UninstallSettings"].Checked = True
        made._uninstall_confirmed()
        self.finish()
        self.assertEqual(self.ran, [(self.script, True)])

    def test_what_could_not_be_removed_is_listed(self):
        made = self.made
        self.answer = (uninstall.LEFT, ["/Users/someone/Library/a folder/Kaitoi", "/Users/someone/.kaitoi_resolve"])
        made._uninstall_pressed()
        made._uninstall_confirmed()
        self.finish()
        self.assertTrue(made._removed, "Kaitoi itself is gone")
        self.assertEqual(self.text(), self.GONE + "\n\nThese were left in place:\n/Users/someone/Library/a folder/Kaitoi\n/Users/someone/.kaitoi_resolve")
        self.assertEqual(self.stage(), ui.STAGE_GONE)
        self.assertGreater(made._shown[("UninstallText", "MinimumSize")][1], 5 * 15, "room for each of its lines")

    def test_a_password_that_is_not_given_puts_the_window_back_as_it_was(self):
        for answer in ((uninstall.NOTHING, []), (2, []), (127, []), OSError("it could not be started")):
            made = self.made
            self.answer = answer
            made._uninstall_pressed()
            made._uninstall_confirmed()
            self.assertEqual(self.stage(), ui.STAGE_WAIT)
            self.assertFalse(self.finish())
            self.assertFalse(made._removed, answer)
            self.assertIsNone(made._uninstall)
            self.assertEqual(self.on_show(), ui.VIEW_ACCOUNT)
            self.assertEqual(self.nav(), [True] * len(ui.NAV))
            self.assertEqual(made.items["Status"].Text, "Nothing was removed.")
            self.assertFalse(config._quiet, "the window writes again")
        self.assertEqual(len(self.ran), 4)

    def test_an_answer_lost_on_the_way_does_not_hide_an_uninstall_that_happened(self):
        """The uninstaller is in the folder it deletes. When it is gone, Kaitoi is gone, whatever came back from it."""
        for answer in (UnicodeDecodeError("ascii", b"\xc3", 0, 1, "its words could not be read"), (-9, []), (uninstall.NOTHING, [])):
            with open(self.script, "w", encoding="utf-8") as fh:
                fh.write("put back for the next case")
            made = self.made = panel()
            made._uninstaller, made.balance, made.view = self.script, "", ui.VIEW_ACCOUNT
            self.answer, self.deletes_itself = answer, True
            made._uninstall_pressed()
            made._uninstall_confirmed()
            self.finish()
            self.assertTrue(made._removed, answer)
            self.assertEqual(self.text(), self.GONE)
            self.assertEqual(self.stage(), ui.STAGE_GONE)
            self.assertEqual(self.nav(), [False] * len(ui.NAV))
            self.assertTrue(config._quiet, "the window does not start writing again with its code gone")
            config.leave_gone(False)

    def test_a_copy_of_the_plugin_without_an_uninstaller_has_no_button(self):
        made = self.made
        made._uninstaller = None
        made.view = ui.VIEW_UNINSTALL  # however it got there
        made._uninstall_confirmed()
        self.assertEqual(self.ran, [])
        self.assertIsNone(made._uninstall)
        with open(ui.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('*([ui.Button({"ID": "Uninstall", "Text": "Uninstall Kaitoi", "Weight": 0.0, "StyleSheet": STYLE_QUIET})] if self._uninstaller else []),', source, "the button is drawn only where there is an uninstaller")
        self.assertIn("        if self._uninstaller:\n            events.Uninstall.Clicked = lambda ev: self._uninstall_pressed()\n", source)


class SigningOutOnRealEffectFiles(Base):
    """The same sign-out, on real effect files in a folder of the test's own."""

    def setUp(self):
        super().setUp()
        # The very modules the window uses, not a fresh import of them: a
        # folder redirected on one copy of a module is not redirected on another.
        templates = self.templates = ui.templates
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.disk = {}

        def update(**changes):
            self.disk.update(changes)
            return dict(self.disk)

        for patcher in (mock.patch.object(templates, "templates_root", lambda: os.path.join(self.dir.name, "Templates")),
                        mock.patch.object(templates.config, "APP_DIR", os.path.join(self.dir.name, "app")),
                        mock.patch.object(ui.config, "update", side_effect=update),
                        mock.patch.object(ui.credentials, "env_api_key", lambda: ""),
                        mock.patch.object(ui.Panel, "_account_switched", lambda panel, before: None),
                        mock.patch.object(ui.resolve_bridge, "registered_effects", lambda session, folder: set())):
            patcher.start(); self.addCleanup(patcher.stop)
        # Before anything is written: both the record and the effects are in this test's own folder.
        inside = os.path.realpath(self.dir.name) + os.sep
        for path in (templates._manifest_path(), templates.folder("Effects"), templates.folder("Generators")):
            if not os.path.realpath(path).startswith(inside):
                self.fail(f"{path} is outside the test's folder: nothing was written")
        self.assertEqual(templates.installed(), {}, "and the record starts empty")

    def files(self):
        found = []
        for folder, _, names in os.walk(os.path.join(self.dir.name, "Templates")):
            found += [name for name in names if name.endswith(".setting")]
        return sorted(found)

    def test_the_workflows_files_go_and_an_installed_nodes_stay(self):
        workflows = [makes("Trailer cut", "video", source="workflow"), makes("Poster", "image", source="workflow")]
        node = makes("Film grain", "video")
        self.templates.sync(workflows)
        self.templates.install(node)
        before = self.files()
        self.assertEqual(len(self.templates.installed()), 3)
        self.assertTrue(any("Trailer cut" in name for name in before) and any("Film grain" in name for name in before), before)
        made = panel(settings={"api_key": "key-abcdef123456", "base_url": "https://api.example.test", "signed_in_as": "jo",
                               "signed_in_key_end": "123456", "signed_in_server": "https://api.example.test"}, width=440)
        made.session, made._owner, made._registered, made._installed = None, "test", set(), {}
        self.disk.update(made.settings)
        with mock.patch.object(ui.Panel, "_rebuild_list", lambda panel: None), mock.patch.object(ui.Panel, "_show_detail", lambda panel: None):
            made._sign_out()
        self.assertEqual(sorted(self.templates.installed()), [node.id])
        after = self.files()
        self.assertTrue(after and all("Film grain" in name for name in after), after)
        self.assertEqual(sorted(made._installed), [node.id], "and the window knows what is left")
        self.assertEqual(made._shown[("Status", "Text")], "Signed out on this computer. Your 2 workflows were removed from Resolve's Effects Library.")
        self.assertEqual(self.disk["api_key"], "")

    def test_a_file_that_will_not_go_is_not_said_to_be_gone(self):
        workflows = [makes("Trailer cut", "video", source="workflow"), makes("Poster", "image", source="workflow")]
        self.templates.sync(workflows)
        folders = [self.templates.folder("Effects"), self.templates.folder("Generators")]
        kept = self.files()
        self.assertTrue(kept)
        for folder in folders:
            if os.path.isdir(folder):
                os.chmod(folder, 0o555)  # nothing can be deleted from it
                self.addCleanup(os.chmod, folder, 0o755)
        made = panel(settings={"api_key": "key-abcdef123456", "base_url": "https://api.example.test", "signed_in_as": "jo",
                               "signed_in_key_end": "123456", "signed_in_server": "https://api.example.test"}, width=440)
        made.session, made._owner, made._registered, made._installed = None, "test", set(), {}
        self.disk.update(made.settings)
        with mock.patch.object(ui.Panel, "_rebuild_list", lambda panel: None), mock.patch.object(ui.Panel, "_show_detail", lambda panel: None):
            made._sign_out()
        self.assertEqual(self.files(), kept, "the files are still in the folder")
        self.assertEqual(made._shown[("Status", "Text")], "Signed out on this computer. 0 of your 2 workflows were removed from Resolve's Effects Library.")
        self.assertEqual(self.disk["api_key"], "", "and the sign-out itself went through")


class WhoseListsAreShown(Base):
    """What one account has is not shown to the next one on this computer, or to nobody."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(config, "APP_DIR", os.path.join(self.dir.name, "app"))
        patcher.start(); self.addCleanup(patcher.stop)
        self.server = "https://api.example.test"
        self.alice, self.bob = tools.account_mark("alice-key"), tools.account_mark("bob-key")
        tools.write_nodes(self.server, self.alice, [makes("Alice secret node", "image")])
        tools.write_snapshot({"base_url": self.server, "account": self.alice, "templates": [], "contracts": {}, "skipped": []})

    def test_a_mark_is_not_the_key_and_no_key_has_no_mark(self):
        self.assertNotIn("alice-key", self.alice)
        self.assertEqual(len(self.alice), 16)
        self.assertNotEqual(self.alice, self.bob)
        self.assertEqual(tools.account_mark(" alice-key "), self.alice)
        self.assertEqual((tools.account_mark(""), tools.account_mark(None)), ("", ""))
        with open(tools.nodes_path(), encoding="utf-8") as fh:
            self.assertNotIn("alice-key", fh.read(), "the key itself is never written beside the list")

    def test_lists_are_read_back_only_by_the_account_that_loaded_them(self):
        self.assertEqual([node.name for node in tools.read_nodes(self.server, self.alice)], ["Alice secret node"])
        self.assertIsNotNone(tools.read_snapshot(self.server, self.alice))
        for account in (self.bob, ""):
            self.assertEqual(tools.read_nodes(self.server, account), [], repr(account))
            self.assertIsNone(tools.read_snapshot(self.server, account), repr(account))
        self.assertEqual(tools.read_nodes("https://another.example.test", self.alice), [])
        # Even a list that somehow got stored with no account on it is nobody's.
        tools.write_nodes(self.server, "", [makes("Stored for nobody", "image")])
        self.assertEqual(tools.read_nodes(self.server, ""), [])

    def test_a_list_stored_before_accounts_were_marked_is_nobodys(self):
        import json

        for path in (tools.nodes_path(), tools.snapshot_path()):
            with open(path, encoding="utf-8") as fh:
                stored = json.load(fh)
            del stored["account"]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(stored, fh)
        self.assertEqual(tools.read_nodes(self.server, self.alice), [])
        self.assertIsNone(tools.read_snapshot(self.server, self.alice))
        self.assertEqual(tools.read_nodes(self.server, ""), [], "least of all to nobody")

    def switch(self, made, key):
        before = (str(made.settings.get("api_key", "")), made._base_url())
        made.settings["api_key"] = key
        with mock.patch.object(ui.Panel, "reload_workflows", lambda panel, quiet=False: None), mock.patch.object(ui.Panel, "_rebuild_list", lambda panel: None), \
                mock.patch.object(ui.Panel, "_rebuild_show_filter", lambda panel: None), mock.patch.object(ui.Panel, "_show_account_info", lambda panel: None), \
                mock.patch.object(ui.Panel, "_show_detail", lambda panel: None):
            made._account_switched(before)

    def test_a_node_list_that_arrives_is_stored_for_the_account_that_asked(self):
        class Arrived(FinishedLoader):
            def take(self):
                fresh = super().take()
                fresh["nodes"] = [makes("Bob node", "image")]
                return fresh

        made = panel(settings={"api_key": "bob-key", "base_url": self.server})
        made.loader = Arrived()
        made._chosen = {}
        with mock.patch.object(ui.Panel, "_rebuild_list", lambda panel: None), mock.patch.object(ui.Panel, "_rebuild_show_filter", lambda panel: None), \
                mock.patch.object(ui.Panel, "_select_tool", lambda panel, keep: None):
            made._collect_loader()
        self.assertEqual([node.name for node in tools.read_nodes(self.server, self.bob)], ["Bob node"])
        self.assertEqual(tools.read_nodes(self.server, self.alice), [], "one list is kept, the last account's")
        self.assertEqual(tools.read_nodes(self.server, ""), [])

    def test_after_signing_out_nothing_of_the_account_is_left_listed(self):
        made = panel(settings={"api_key": "alice-key", "base_url": self.server}, node_tools=tools.read_nodes(self.server, self.alice),
                     workflow_tools=[makes("Alice private workflow", "video", source="workflow")])
        made._chosen, made.balance = {}, "6,275 credits"
        made.tool = made.node_tools[0]
        self.switch(made, "")
        self.assertEqual((made.node_tools, made.workflow_tools), ([], []))
        self.assertIsNone(made.tool, "nor on the card")
        self.assertEqual(made.balance, "")

    def test_the_next_account_starts_with_its_own_lists_or_none(self):
        made = panel(settings={"api_key": "alice-key", "base_url": self.server}, node_tools=tools.read_nodes(self.server, self.alice))
        made._chosen, made.balance = {}, ""
        self.switch(made, "bob-key")
        self.assertEqual(made.node_tools, [], "not Alice's, while Bob's are on their way")
        self.switch(made, "alice-key")
        self.assertEqual([node.name for node in made.node_tools], ["Alice secret node"], "her own are there at once when she comes back")

