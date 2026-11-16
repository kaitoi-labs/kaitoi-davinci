"""What the editor can run: published Kaitoi workflows and single nodes.

A *tool* is one runnable thing with a known contract: named inputs with types
and named outputs with types. Two sources produce tools:

- **Workflows.** Every template in the account that has an API endpoint. The
  contract comes from Kaitoi, so a workflow built in Studio appears here with
  no change to the plugin.
- **Nodes.** Every node in Kaitoi's catalog the account can use: built-in,
  community, and its own. A current server lists them with their pins. An
  older one lists them without, and a node then starts out *unloaded* and
  gets its inputs and outputs when it is picked.

Nothing here talks to Resolve. What the timeline can supply arrives as a set
of media kinds (``{"video", "image", "audio"}``), which is all the matching
needs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import threading
import zlib
import time
import re
from dataclasses import dataclass, replace
from typing import Any, Callable

from . import api, config

MEDIA = ("video", "image", "audio")
MEDIA_LABELS = {"video": "Video", "image": "Image", "audio": "Audio"}
DEFAULT_CATEGORY = "Workflows"
NODE_CATEGORY = "Other nodes"
# Pin types a form can hold. Anything else (node links, models, embeddings)
# is left to the node's own default.
FORM_TYPES = ("string", "int", "float", "bool", "color")
ORIGIN_LABELS = {"builtin": "Built-in", "community": "Community", "user": "Mine"}

# Kaitoi's own aliases for data types (app/data_types/classification.py): the
# spellings a node may give a pin, and the one name each is known by. Kept in
# step with that table, so that what is a picture, a video, sound or a web page
# to Kaitoi is the same to this plugin. Its "dict" and "json" are left out:
# nothing here draws or fills them differently.
_TYPE_ALIASES = {
    "str": "string", "txt": "string", "md": "string", "text": "string", "prompt": "string",
    "number": "float", "double": "float", "integer": "int", "boolean": "bool",
    "img": "image", "jpg": "image", "jpeg": "image", "png": "image", "gif": "image", "webp": "image",
    "bmp": "image", "exr": "image", "hdr": "image", "tiff": "image", "tif": "image", "heic": "image", "heif": "image", "psd": "image",
    "svg": "vector",
    "mp4": "video", "webm": "video", "mov": "video",
    "mp3": "audio", "wav": "audio", "ogg": "audio", "aiff": "audio", "aac": "audio", "opus": "audio",
    "glb": "3d", "gltf": "3d", "obj": "3d", "usd": "3d", "usda": "3d", "usdc": "3d", "usdz": "3d", "fbx": "3d", "mesh": "3d", "model": "3d", "model3d": "3d",
    "ply": "splat", "ksplat": "splat", "spz": "splat", "sog": "splat",
    "rrd": "rerun", "emb": "embedding", "npy": "embedding", "npz": "embedding",
    "zip": "archive", "tar": "archive", "gz": "archive", "7z": "archive", "rar": "archive",
    "document": "file", "html": "web", "htm": "web",
}
# The names a pin written as "<something>_file" may keep; any other is a plain file.
_FILE_TYPES = frozenset({"3d", "archive", "audio", "data", "embedding", "file", "image", "object", "pdf", "plot", "rerun", "splat", "vector", "video", "web"})
STATE_POINTER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.(value|options)")  # a default such as "selectedVoice.value"
_PROMPT_NAMES = ("prompt", "text", "description", "script", "instruction", "instructions", "caption", "lyrics")


class ToolInputError(ValueError):
    """A form value that cannot be sent as it stands."""


def normalize_type(data_type: Any) -> str:
    """The one name Kaitoi knows a pin's type by, whatever way a node spelled it."""
    text = str(data_type or "").strip().lower().replace("-", "_")
    while text.endswith(("?", "!")):
        text = text[:-1]
    while text.endswith("[]"):
        text = text[:-2]
    text = re.sub(r"\s+", "_", text)
    if text.endswith("_file"):
        base = _TYPE_ALIASES.get(text[:-5], text[:-5])
        return base if base in _FILE_TYPES else "file"
    return _TYPE_ALIASES.get(text) or _TYPE_ALIASES.get(text.replace("_", "")) or text


@dataclass(frozen=True)
class Field:
    """One input of a tool."""

    name: str  # the name the API takes the value under
    label: str
    data_type: str  # normalized
    default: Any = None
    options: tuple = ()  # the values a choice may take, as the API takes them
    minimum: Any = None
    maximum: Any = None
    step: Any = None
    option_labels: tuple = ()  # what to call each option; empty means "by its value"
    dynamic: bool = False  # its choices are only made when the node starts in Studio

    @property
    def media(self) -> str | None:
        return self.data_type if self.data_type in MEDIA else None

    @property
    def choices(self) -> tuple:
        """``(value, label)`` for each option, in order."""
        labels = self.option_labels if len(self.option_labels) == len(self.options) else ()
        return tuple((value, str(labels[index]) if labels else str(value)) for index, value in enumerate(self.options))

    def choice_label(self, value: Any) -> str | None:
        """The label of the option holding ``value``, or None when there is none."""
        for option, label in self.choices:
            if option == value and type(option) is type(value):
                return label
        for option, label in self.choices:
            if str(option) == str(value):
                return label
        return None

    @property
    def widget(self) -> str:
        """How the panel draws it: media, choice, bool, number, color or text."""
        if self.media:
            return "media"
        if self.options:
            return "choice"
        if self.data_type == "bool":
            return "bool"
        if self.data_type in ("int", "float"):
            return "number"
        if self.data_type == "color":
            return "color"
        return "text"

    @property
    def is_prompt(self) -> bool:
        """Free writing that deserves the big text box, not a one-line field."""
        if self.widget != "text":
            return False
        words = set(re.findall(r"[a-z]+", f"{self.name} {self.label}".lower()))
        return any(name in words for name in _PROMPT_NAMES)


@dataclass(frozen=True)
class Output:
    """One result of a tool."""

    name: str
    label: str
    data_type: str

    @property
    def media(self) -> str | None:
        return self.data_type if self.data_type in MEDIA else None


@dataclass(frozen=True)
class Tool:
    id: str
    source: str  # "workflow" | "node"
    name: str
    description: str
    category: str
    tags: tuple
    inputs: tuple
    outputs: tuple
    template_id: str = ""
    endpoint_id: str = ""
    node_type: str = ""  # nodes: the catalog identifier
    origin: str = ""  # nodes: "builtin" | "community" | "user"
    ready: bool = True  # False for a node whose pins have not been fetched yet
    thumbnail_url: str = ""
    updated_at: str = ""
    icon: str = ""  # nodes: the name of the icon Studio draws for it
    needs: tuple = ()  # nodes: inputs it cannot run without that this plugin cannot give ("a 3D model (Input Model)")
    session: bool = False  # nodes: runs as a live session a person takes part in, in Studio
    agent: bool = False  # nodes: an agent, or a building block made for agents

    @property
    def media_inputs(self) -> tuple:
        return tuple(f for f in self.inputs if f.media)

    @property
    def param_inputs(self) -> tuple:
        return tuple(f for f in self.inputs if not f.media)

    @property
    def media_outputs(self) -> tuple:
        return tuple(o for o in self.outputs if o.media)


# ── Tasks: what a tool turns into what ──────────────────────────────────────


def _kinds(items) -> tuple:
    present = {item.media for item in items if item.media}
    return tuple(kind for kind in MEDIA if kind in present)


def task_key(tool: Tool) -> str:
    """A stable id for the tool's shape, for example ``video>video+audio``."""
    return f"{'+'.join(_kinds(tool.inputs))}>{'+'.join(_kinds(tool.outputs))}"


def task_label(tool: Tool) -> str:
    """The same shape in words: ``Video → Video``, ``Create Image``.

    A node whose pins are not loaded yet has no shape; the catalog's own
    category stands in, which for model nodes already says it ("Image to Video").
    """
    if not tool.ready:
        return ORIGIN_LABELS.get(tool.origin, tool.origin.title() or "Node")
    takes = " + ".join(MEDIA_LABELS[k] for k in _kinds(tool.inputs))
    makes = " + ".join(MEDIA_LABELS[k] for k in _kinds(tool.outputs)) or "Text"
    return f"{takes} → {makes}" if takes else f"Create {makes}"


def primary_input(tool: Tool, supplies: set) -> Field | None:
    """The media input the timeline fills: the first one it can supply."""
    for field in tool.media_inputs:
        if field.media in supplies:
            return field
    return None


def fits(tool: Tool, supplies: set) -> bool:
    """Can this tool run on what the editor is pointing at?

    A tool that takes no media always fits: it creates from nothing. So does a
    node whose pins are not loaded yet, since nothing is known against it.
    """
    return not tool.ready or not tool.media_inputs or primary_input(tool, supplies) is not None


def matches(tool: Tool, query: str) -> bool:
    """Every word of ``query`` appears somewhere in the tool's description.

    Tags are searched but never shown, so an author can add the words people
    actually type.
    """
    words = [word for word in re.split(r"\s+", query.strip().lower()) if word]
    if not words:
        return True
    haystack = " ".join(
        [tool.name, tool.description, tool.category, task_label(tool), " ".join(tool.tags)]
    ).lower()
    return all(word in haystack for word in words)


WEB_TYPE = "web"  # the result of a node that draws a page of its own in Studio


def places_in_resolve(tool: Tool) -> bool:
    """Does the tool make something DaVinci Resolve can put on a timeline: a
    picture, a video or sound? Text, 3D models and the like are downloaded
    but cannot be placed.

    A node with a web page of its own is left out even when it also makes a
    picture, a video or sound: it is worked in that page, in Studio, and there
    is no such page in Resolve. A workflow is not: it was built and published
    to run with nobody at a page. So is a node that cannot run without an
    input neither the form nor the timeline can give, a 3D model say; a
    session node, which a person takes part in live, in Studio; and an agent
    node, which works with other nodes around it. A node whose pins are not
    loaded yet is not ruled out for its pins: nothing is known of them."""
    if tool.source == "node" and (tool.session or tool.agent):
        return False
    if not tool.ready:
        return True
    if tool.needs:
        return False
    if tool.source == "node" and any(output.data_type == WEB_TYPE for output in tool.outputs):
        return False
    return any(output.media for output in tool.outputs)


def filter_tools(
    tools, *, query: str = "", supplies: set | None = None, task: str = "", category: str = "", origin: str = "", origins=None,
    placeable: bool = False, only=None,
) -> list:
    """Narrow ``tools`` by search words, by what the timeline supplies, by task,
    by category, by where a node comes from (one ``origin``, or any of
    ``origins``), to those whose result Resolve can place (``placeable``), and
    to the ids in ``only`` (the user's favorites)."""
    chosen = []
    for tool in tools:
        if placeable and not places_in_resolve(tool):
            continue
        if only is not None and tool.id not in only:
            continue
        if supplies is not None and not fits(tool, supplies):
            continue
        if task and task_key(tool) != task:
            continue
        if category and tool.category != category:
            continue
        if origin and tool.origin != origin:
            continue
        if origins is not None and tool.origin not in origins:
            continue
        if not matches(tool, query):
            continue
        chosen.append(tool)
    return chosen


def group(tools) -> list:
    """``[(category, [tools])]``, categories and their tools by name."""
    buckets: dict = {}
    for tool in tools:
        buckets.setdefault(tool.category or DEFAULT_CATEGORY, []).append(tool)
    return [(name, sorted(buckets[name], key=lambda t: t.name.lower())) for name in sorted(buckets, key=str.lower)]


def categories(tools) -> list:
    """The categories present in ``tools``, by name."""
    return sorted({tool.category for tool in tools if tool.category}, key=str.lower)


def origins(tools) -> list:
    """``[(origin, label)]`` present in ``tools``: built-in first, then the rest."""
    present = {tool.origin for tool in tools if tool.origin}
    known = [key for key in ORIGIN_LABELS if key in present]
    return [(key, ORIGIN_LABELS.get(key, key.title())) for key in known + sorted(present - set(known))]


def tasks(tools) -> list:
    """``[(task key, task label)]`` present in ``tools``, by label."""
    seen: dict = {}
    for tool in tools:
        seen.setdefault(task_key(tool), task_label(tool))
    return sorted(seen.items(), key=lambda pair: pair[1].lower())


# ── Building tools ──────────────────────────────────────────────────────────


def _number(value: Any) -> Any:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def pin_details(pin: dict) -> dict:
    """A pin's choices, range and step as Kaitoi describes them, ready for a
    :class:`Field`. A server that does not send them yet gives none, and the
    input is then drawn as a plain box.

    A choice arrives as ``{"value", "label"}``; a bare value is its own label.
    """
    values, labels = [], []
    raw = pin.get("options")
    for item in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(item, dict):
            if "value" not in item:
                continue
            value, label = item["value"], item.get("label")
        else:
            value, label = item, None
        if not isinstance(value, (str, int, float, bool)):
            continue
        values.append(value)
        # An empty value is a real choice (often "no preset"); it needs a name to be seen.
        labels.append(str(label) if label not in (None, "") else (str(value) if value != "" else "None"))
    return {
        "options": tuple(values),
        "option_labels": tuple(labels) if labels != [str(value) for value in values] else (),
        "minimum": _number(pin.get("minimum", pin.get("min"))),
        "maximum": _number(pin.get("maximum", pin.get("max"))),
        "step": _number(pin.get("step")),
    }


def _field_from_pin(pin: dict) -> Field:
    """An endpoint input as Kaitoi describes it."""
    name = str(pin.get("fieldName") or pin.get("name") or "value")
    return Field(
        name=name,
        label=str(pin.get("name") or name),
        data_type=normalize_type(pin.get("dataType")),
        default=pin.get("default"),
        **pin_details(pin),
    )


def _output_from_pin(pin: dict) -> Output:
    name = str(pin.get("fieldName") or pin.get("name") or "value")
    return Output(name=name, label=str(pin.get("name") or name), data_type=normalize_type(pin.get("dataType")))


def tool_from_endpoint(template: dict, endpoint: dict, contract: dict) -> Tool:
    """A published workflow endpoint as a tool.

    One template can carry several endpoints. Each is its own tool; when there
    is more than one, the endpoint's name tells them apart.
    """
    template_name = str(template.get("name") or "Untitled workflow")
    endpoint_name = str(endpoint.get("name") or "").strip()
    siblings = len(template.get("endpoints") or [])
    name = template_name if siblings <= 1 or not endpoint_name else f"{template_name}: {endpoint_name}"
    return Tool(
        id=f"workflow:{endpoint.get('endpointId')}",
        source="workflow",
        name=name,
        description=str(endpoint.get("description") or template.get("description") or "").strip(),
        category=str(template.get("category") or "").strip() or DEFAULT_CATEGORY,
        tags=tuple(str(tag) for tag in (template.get("tags") or [])),
        inputs=tuple(_field_from_pin(pin) for pin in (contract.get("inputs") or [])),
        outputs=tuple(_output_from_pin(pin) for pin in (contract.get("outputs") or [])),
        template_id=str(template.get("templateId") or ""),
        endpoint_id=str(endpoint.get("endpointId") or ""),
        thumbnail_url=str(template.get("thumbnailUrl") or ""),
        updated_at=contract_stamp(template, endpoint),
    )


def node_from_summary(summary: dict) -> Tool:
    """A catalog entry as a tool. With its pins when the entry carries them;
    otherwise one that still has to load them."""
    if isinstance(summary.get("inputs"), list) and isinstance(summary.get("outputs"), list):
        return node_from_detail(summary)
    return _node_without_pins(summary)


def _node_without_pins(summary: dict) -> Tool:
    node_type = str(summary.get("type") or "")
    return Tool(
        id=f"node:{node_type}",
        source="node",
        name=str(summary.get("title") or node_type.rsplit("/", 1)[-1]),
        description=str(summary.get("description") or "").strip(),
        category=str(summary.get("category") or "").strip() or NODE_CATEGORY,
        tags=tuple(str(tag) for tag in (summary.get("tags") or [])),
        inputs=(),
        outputs=(),
        node_type=node_type,
        origin=str(summary.get("source") or "").strip().lower(),
        ready=False,
        thumbnail_url=str(summary.get("thumbnailUrl") or ""),
        icon=str(summary.get("icon") or "").strip(),
        # Only a plain yes counts: a server that does not say leaves both off.
        session=summary.get("session") is True,
        agent=summary.get("agent") is True,
    )


# What an input of a kind no form holds and no timeline supplies is called,
# when a node cannot run without it.
_NEEDS_WORDS = {"3d": "a 3D model", "splat": "a 3D scan", "archive": "an archive", "pdf": "a PDF", "vector": "a vector drawing", "file": "a file",
                "plugin": "a plugin", "node": "another node", "object": "data from another node", "embedding": "an embedding"}


def _needed(pin: dict, data_type: str) -> str:
    """A node's input that it cannot run without and that this plugin cannot
    give, in words; "" for one it can do without.

    Kaitoi marks each input: "required", "optional", "defaulted" (it has a
    value of its own) or "unspecified", an older node that says neither and so
    promises nothing. Only the first and the last are counted. A server that
    does not say at all is not held against the node.
    """
    if pin.get("requirement") not in ("required", "unspecified"):
        return ""
    what = _NEEDS_WORDS.get(data_type) or f"{data_type or 'another'} input"
    return f"{what} ({pin.get('label') or pin.get('name')})"


def node_from_detail(detail: dict) -> Tool:
    """A node with its pins: every input a form can hold, and its outputs."""
    base = _node_without_pins(detail)
    inputs = []
    needs = [str(need) for need in detail.get("needs") or []]  # as this plugin stored it: its file keeps no pin it cannot fill
    for pin in detail.get("inputs") or []:
        if not isinstance(pin, dict):
            continue
        name = pin.get("name")
        data_type = normalize_type(pin.get("type"))
        if not name:
            continue
        if data_type not in MEDIA and data_type not in FORM_TYPES:
            need = _needed(pin, data_type)
            if need and need not in needs:
                needs.append(need)
            continue
        details = pin_details(pin) if data_type not in MEDIA else {}
        default = pin.get("default")
        # "selectedVoice.value": the node fills this in itself when it starts
        # in Studio (a list of voices fetched from a provider, say). Here it
        # is neither a value nor a list, so the input is a plain box.
        pointer = isinstance(default, str) and bool(STATE_POINTER.fullmatch(default))
        fills_itself = bool(pin.get("dynamic")) or pointer
        if pointer:
            default = None
        inputs.append(
            Field(name=str(name), label=str(pin.get("label") or name), data_type=data_type, default=default, dynamic=fills_itself, **details)
        )
    outputs = [
        Output(name=str(pin["name"]), label=str(pin.get("label") or pin["name"]), data_type=normalize_type(pin.get("type")))
        for pin in detail.get("outputs") or []
        if isinstance(pin, dict) and pin.get("name") and pin.get("name") != "self" and normalize_type(pin.get("type")) != "node"
    ]
    return Tool(
        id=base.id,
        source="node",
        name=base.name,
        description=base.description,
        category=base.category,
        tags=base.tags,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        node_type=base.node_type,
        origin=base.origin,
        ready=True,
        thumbnail_url=base.thumbnail_url,
        icon=base.icon,
        needs=tuple(needs),
        session=base.session,
        agent=base.agent,
    )


def needs_initial_state(tool: Tool) -> bool:
    """Has this node an input whose list only the node itself can fill in,
    and which has not been filled in yet?"""
    # A prompt box whose example text the node writes itself does not count:
    # starting the node only to fetch a sample sentence is not worth it.
    return tool.source == "node" and tool.ready and any(field.dynamic and not field.options and not field.is_prompt for field in tool.inputs)


def apply_initial_state(tool: Tool, state: Any) -> Tool:
    """The node with the lists and starting values it filled in when it
    started (``api.get_node_initial_state``) put on its dynamic inputs."""
    filled = state.get("inputs") if isinstance(state, dict) else None
    if not isinstance(filled, dict):
        return tool
    inputs = []
    for field in tool.inputs:
        entry = filled.get(field.name)
        if not field.dynamic or not isinstance(entry, dict):
            inputs.append(field)
            continue
        details = pin_details({"options": entry.get("options")})
        value = entry.get("value")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or value == "":
            value = field.default
        inputs.append(replace(field, options=details["options"], option_labels=details["option_labels"], default=value))
    return replace(tool, inputs=tuple(inputs))


def load_node(cfg: api.Config, tool: Tool) -> Tool:
    """Fetch a node's pins. Returns the tool, loaded."""
    return node_from_detail(api.get_node_type(cfg, tool.node_type))


RATE_LIMIT_WAIT_SECONDS = 20  # when the server does not say how long to wait
RATE_LIMIT_LONGEST_WAIT_SECONDS = 90


def _retry_wait(details: Any) -> int:
    """Whole seconds to wait after a "too many requests": what the server
    asked for, rounded up, at least one and never unreasonably long."""
    asked = details.get("retryAfterSeconds") if isinstance(details, dict) else None
    try:
        seconds = math.ceil(float(asked)) if asked is not None and not isinstance(asked, bool) else RATE_LIMIT_WAIT_SECONDS
    except (TypeError, ValueError, OverflowError):
        seconds = RATE_LIMIT_WAIT_SECONDS
    return min(max(1, seconds), RATE_LIMIT_LONGEST_WAIT_SECONDS)


def load_node_patiently(
    cfg: api.Config,
    tool: Tool,
    *,
    should_stop: Callable[[], bool] | None = None,
    on_wait: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    attempts: int = 6,
) -> Tool | None:
    """:func:`load_node` for many nodes in a row: when the server says "too
    many requests", wait as long as it asks and try again.

    ``on_wait`` is told how many seconds are left of a wait. Returns None when
    ``should_stop`` turns true during one.
    """
    for attempt in range(attempts):
        try:
            return load_node(cfg, tool)
        except api.KaitoiAPIError as exc:
            if exc.status != 429 or attempt == attempts - 1:
                raise
            left = float(_retry_wait(exc.details))
            while left > 0:
                if should_stop is not None and should_stop():
                    return None
                if on_wait is not None:
                    on_wait(int(left + 0.999))
                pause = min(0.5, left)
                (sleep or time.sleep)(pause)
                left -= pause
    return None


def load_node_catalog(cfg: api.Config, *, should_stop: Callable[[], bool] | None = None) -> list:
    """Every node the account can use: with its pins from a current server,
    unloaded from an older one. The plain loaders are left out: on their own
    they only hand back the file they were given."""
    nodes = []
    for summary in api.list_node_types(cfg, should_stop=should_stop):
        node_type = str(summary.get("type") or "")
        if not node_type or "/loaders/" in node_type:
            continue
        nodes.append(node_from_summary(summary))
    return nodes


# Endpoints whose contract was fetched since this script started. A server
# from before contracts described their inputs is asked once a session, not
# on every reload.
_contracts_asked: set = set()


def contract_stamp(template: dict, endpoint: dict) -> str:
    """When this workflow last changed: its endpoint or its template,
    whichever is later to be saved. The endpoint decides which inputs there
    are; the template holds their saved values and the thumbnail. Saving a
    template does not touch its endpoints, so both dates count."""
    return f"{endpoint.get('updatedAt') or ''}|{template.get('updatedAt') or ''}".strip("|")


def _describes_inputs(inputs: Any) -> bool:
    """Did the server that sent these inputs say what a form needs (choices,
    saved default)? A current server sends the keys even when they are empty."""
    pins = [pin for pin in inputs or [] if isinstance(pin, dict)]
    return all("options" in pin and "default" in pin for pin in pins)


def _contract_of(endpoint: dict) -> dict | None:
    """The contract when the template list already carries it."""
    if isinstance(endpoint.get("inputs"), list) and isinstance(endpoint.get("outputs"), list):
        return {"inputs": endpoint["inputs"], "outputs": endpoint["outputs"]}
    return None


def load_workflow_tools(
    cfg: api.Config,
    known: dict | None = None,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> tuple:
    """Fetch the account's published workflows. Returns ``(tools, snapshot)``.

    ``known`` is an earlier snapshot. An endpoint whose ``updatedAt`` has not
    moved reuses its stored contract, so a refresh costs one request and not
    one per endpoint. ``snapshot`` is what to store for next time.
    """
    known_contracts = (known or {}).get("contracts") or {}
    templates = api.list_templates(cfg, has_endpoint=True)
    contracts: dict = {}
    skipped: list = []
    tools = []
    for template in templates:
        for endpoint in template.get("endpoints") or []:
            if should_stop is not None and should_stop():
                raise api.KaitoiAPIError(0, "ABORTED", "stopped by user")
            if endpoint.get("isActive") is False:
                continue
            endpoint_id = str(endpoint.get("endpointId") or "")
            if not endpoint_id:
                continue
            stamp = contract_stamp(template, endpoint)
            contract = _contract_of(endpoint)
            if contract is None:
                stored = known_contracts.get(endpoint_id) or {}
                # A stored contract is good while the endpoint has not changed,
                # provided it describes its inputs. One that does not came
                # from an older server (or an older plugin): the server may
                # have learned to since, so it is asked again, once a session.
                unchanged = stamp and stored.get("updatedAt") == stamp and isinstance(stored.get("inputs"), list)
                if unchanged and (stored.get("detailed") is True or endpoint_id in _contracts_asked):
                    contract = {"inputs": stored["inputs"], "outputs": stored.get("outputs") or []}
                    contracts[endpoint_id] = dict(stored)
                    tools.append(tool_from_endpoint(template, endpoint, contract))
                    continue
            if contract is None:
                try:
                    docs = api.get_endpoint_docs(cfg, str(template.get("templateId") or ""), endpoint_id)
                except api.KaitoiAPIError as exc:
                    if exc.code == "ABORTED":
                        raise
                    # The workflow is still published; only its contract could
                    # not be read just now. Keep working from the stored one
                    # when there is one, and ask again on the next load.
                    skipped.append(endpoint_id)
                    stored = known_contracts.get(endpoint_id) or {}
                    if isinstance(stored.get("inputs"), list):
                        config.log(f"using the stored contract of workflow '{template.get('name')}': {exc}", "WARN")
                        contracts[endpoint_id] = {**stored, "updatedAt": ""}  # no stamp: never mistaken for up to date
                        tools.append(tool_from_endpoint(template, endpoint, {"inputs": stored["inputs"], "outputs": stored.get("outputs") or []}))
                    else:
                        config.log(f"skipped workflow '{template.get('name')}': {exc}", "WARN")
                    continue
                _contracts_asked.add(endpoint_id)
                contract = {"inputs": docs.get("inputs") or [], "outputs": docs.get("outputs") or []}
            contracts[endpoint_id] = {"updatedAt": stamp, "detailed": _describes_inputs(contract["inputs"]), **contract}
            tools.append(tool_from_endpoint(template, endpoint, contract))
    snapshot = {
        "base_url": cfg.base_url,
        "account": account_mark(cfg.api_key),
        "templates": templates,
        "contracts": contracts,
        # Published, but its contract could not be read this time: not to be
        # mistaken for a workflow that was taken down.
        "skipped": skipped,
    }
    return tools, snapshot


def tools_from_snapshot(snapshot: dict | None) -> list:
    """Rebuild tools from a stored snapshot, without the network."""
    if not snapshot:
        return []
    contracts = snapshot.get("contracts") or {}
    tools = []
    for template in snapshot.get("templates") or []:
        for endpoint in template.get("endpoints") or []:
            contract = contracts.get(str(endpoint.get("endpointId") or ""))
            if contract:
                tools.append(tool_from_endpoint(template, endpoint, contract))
    return tools


# ── Snapshot on disk: the list opens at once, then refreshes ────────────────


def snapshot_path() -> str:
    config.app_dir()
    return os.path.join(config.APP_DIR, "workflows.json")


def account_mark(api_key: Any) -> str:
    """A mark for the account a list was loaded with, kept beside the list so
    that it is only ever shown to that account again. It is not the key and
    gives no way back to it. "" for no key: nothing is shown to nobody."""
    key = str(api_key or "").strip()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else ""


def _stored_for(stored: Any, base_url: str, account: str) -> bool:
    """Was this stored list taken from this server, by this account? A list
    stored before accounts were marked is nobody's, and is loaded again."""
    return (
        isinstance(stored, dict)
        and stored.get("base_url") == base_url.rstrip("/")
        and bool(account)
        and stored.get("account") == account
    )


def read_snapshot(base_url: str, account: str) -> dict | None:
    """The stored snapshot, only if this same account took it from this same
    server: workflows are an account's own, and are not shown to the next one
    that signs in on this computer."""
    try:
        with open(snapshot_path(), "r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return None
    return stored if _stored_for(stored, base_url, account) else None


def nodes_path() -> str:
    config.app_dir()
    return os.path.join(config.APP_DIR, "nodes.json")


def read_nodes(base_url: str, account: str) -> list:
    """The stored node catalog, only if this same account took it from this
    same server: it holds the account's own nodes beside everyone's."""
    try:
        with open(nodes_path(), "r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return []
    if not _stored_for(stored, base_url, account):
        return []
    return [node_from_summary(entry) for entry in stored.get("nodes") or []]


def _node_row(node: Tool, with_pins: bool = True) -> dict:
    """A node as the catalog lists it, so :func:`node_from_summary` reads it back."""
    row: dict = {
        "type": node.node_type,
        "title": node.name,
        "category": node.category,
        "description": node.description,
        "tags": list(node.tags),
        "source": node.origin,
        # The link itself expires within the hour; kept so a thumbnail already
        # fetched is shown when the window next opens, before the list refreshes.
        "thumbnailUrl": node.thumbnail_url,
        "icon": node.icon,
    }
    if node.ready and with_pins:
        row["inputs"] = [
            {
                "name": field.name,
                "label": field.label,
                "type": field.data_type,
                # What a node filled in itself (a list of voices) is asked
                # for afresh each session, never read back from this file.
                "default": None if field.dynamic else field.default,
                "options": [] if field.dynamic else [{"value": value, "label": label} for value, label in field.choices],
                "min": field.minimum,
                "max": field.maximum,
                "step": field.step,
                "dynamic": field.dynamic,
            }
            for field in node.inputs
        ]
        row["outputs"] = [{"name": output.name, "label": output.label, "type": output.data_type} for output in node.outputs]
        row["needs"] = list(node.needs)
    if node.session:
        row["session"] = True
    if node.agent:
        row["agent"] = True
    return row


def write_nodes(base_url: str, account: str, nodes, *, with_pins: bool = True) -> None:
    """Store the catalog, marked with the account it is. ``with_pins=False``
    leaves the pins out: when the server lists none, the only pins known are
    those of nodes opened one by one, and keeping them would hide a later
    change to those nodes."""
    payload = {"base_url": base_url.rstrip("/"), "account": account, "nodes": [_node_row(node, with_pins) for node in nodes]}
    partial = nodes_path() + ".part"
    try:
        with open(partial, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(partial, nodes_path())
        os.chmod(nodes_path(), 0o600)
    except OSError as exc:
        config.log(f"could not store the node catalog: {exc}", "WARN")


def write_snapshot(snapshot: dict) -> None:
    target = snapshot_path()
    partial = target + ".part"
    try:
        with open(partial, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh)
        os.replace(partial, target)
        os.chmod(target, 0o600)
    except OSError as exc:
        config.log(f"could not store the workflow list: {exc}", "WARN")


# ── Thumbnails ──────────────────────────────────────────────────────────────


def thumbnail_dir() -> str:
    directory = os.path.join(config.app_dir(), "thumbnails")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory


def thumbnail_path(tool: Tool) -> str:
    """Where this tool's thumbnail is kept. Changes when the template does."""
    stamp = re.sub(r"[^0-9A-Za-z]", "", tool.updated_at)[-20:] or "0"
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", tool.template_id or tool.id)
    return os.path.join(thumbnail_dir(), f"{safe_id}-{stamp}.jpg")


def tile_path(tool: Tool) -> str:
    """Where the 16:9 cut of a thumbnail of another shape is kept."""
    return thumbnail_path(tool)[: -len(".jpg")] + ".tile.png"


def thumbnail_tile(tool: Tool) -> str | None:
    """The tool's thumbnail as a 16:9 picture, if there is one on disk: the
    thumbnail itself when it is that shape, else its cut. The list and the
    card both show this, so a tool looks the same in both. None when there is
    no thumbnail, or one of another shape that could not be cut."""
    picture = thumbnail_path(tool)
    if not tool.thumbnail_url or not os.path.isfile(picture):
        return None
    if is_wide_tile(picture):
        return picture
    cut = tile_path(tool)
    return cut if os.path.isfile(cut) else None


def jpeg_size(path: str) -> tuple | None:
    """``(width, height)`` of a JPEG file, read from its header, or None."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(256 * 1024)
    except OSError:
        return None
    at = 2
    while at + 9 < len(data):
        if data[at] != 0xFF:
            at += 1
            continue
        marker = data[at + 1]
        if marker in (0xC0, 0xC1, 0xC2):  # the frame header: height then width
            height, width = struct.unpack(">HH", data[at + 5 : at + 9])
            return (width, height) if width and height else None
        if marker in (0xD8, 0x01, 0xFF) or 0xD0 <= marker <= 0xD7:
            at += 2 if marker != 0xFF else 1
            continue
        at += 2 + struct.unpack(">H", data[at + 2 : at + 4])[0]
    return None


def is_wide_tile(path: str) -> bool:
    """Is the picture close to 16:9? A list draws its rows' pictures at one
    height, so any other shape comes out a different width and pushes that
    row's text out of line with the rest."""
    size = jpeg_size(path)
    return bool(size) and abs(size[0] / size[1] - 16 / 9) < 0.08


# ── A 16:9 tile from a picture of any shape ─────────────────────────────────
#
# Resolve's Python has no imaging library, so the little that is needed is
# done here: read a small PNG, cut the middle out of it at 16:9, write a PNG.

TILE_BACKGROUND = (23, 23, 26)  # shows through a picture that is partly see-through
TILE_MOST_SIDE = 512  # twice what is asked for: anything larger is not the small picture, and slow to read here
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}  # grey, colour, grey + alpha, colour + alpha


def _png_rows(data: bytes) -> tuple | None:
    """``(width, height, channels, rows)`` of an 8-bit PNG that is not
    interlaced, each row as ``bytes`` of ``width * channels``. None for any
    other file: this reads only what the Kaitoi server makes."""
    if not data.startswith(_PNG_SIGNATURE):
        return None
    at, header, packed = 8, None, []
    while at + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[at : at + 8])
        body = data[at + 8 : at + 8 + length]
        if kind == b"IHDR":
            if len(body) < 13:
                return None
            header = struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"IDAT":
            packed.append(body)
        elif kind == b"IEND":
            break
        at += 12 + length
    if header is None:
        return None
    width, height, depth, colour, _, _, interlace = header
    channels = _PNG_CHANNELS.get(colour)
    if depth != 8 or channels is None or interlace or not (0 < width <= TILE_MOST_SIDE and 0 < height <= TILE_MOST_SIDE):
        return None
    stride = width * channels
    needed = height * (stride + 1)
    try:
        # No more is unpacked than the picture says it holds: a few kilobytes
        # can be made to unpack into gigabytes.
        raw = zlib.decompressobj().decompress(b"".join(packed), needed)
    except zlib.error:
        return None
    if len(raw) < needed:
        return None
    rows, previous = [], bytes(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1 : start + 1 + stride])
        if kind == 1:  # each byte adds the one a pixel to its left
            for x in range(channels, stride):
                line[x] = (line[x] + line[x - channels]) & 255
        elif kind == 2:  # the one above
            for x in range(stride):
                line[x] = (line[x] + previous[x]) & 255
        elif kind == 3:  # the mean of left and above
            for x in range(stride):
                left = line[x - channels] if x >= channels else 0
                line[x] = (line[x] + ((left + previous[x]) >> 1)) & 255
        elif kind == 4:  # whichever of left, above and above-left is nearest their sum
            for x in range(stride):
                left = line[x - channels] if x >= channels else 0
                above = previous[x]
                corner = previous[x - channels] if x >= channels else 0
                guess = left + above - corner
                to_left, to_above, to_corner = abs(guess - left), abs(guess - above), abs(guess - corner)
                if to_left <= to_above and to_left <= to_corner:
                    nearest = left
                elif to_above <= to_corner:
                    nearest = above
                else:
                    nearest = corner
                line[x] = (line[x] + nearest) & 255
        elif kind != 0:
            return None
        previous = bytes(line)
        rows.append(previous)
    return width, height, channels, rows


def _png_from_rgb(width: int, height: int, rows) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rows)
    return _PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def crop_png_to_tile(data: bytes, aspect: float = 16 / 9) -> bytes | None:
    """The middle of a PNG, cut to ``aspect``, as a plain colour PNG. A list
    draws every row's picture at one height, so only pictures of one shape
    line up. None when the file is not a PNG this can read."""
    read = _png_rows(data)
    if read is None:
        return None
    width, height, channels, rows = read
    wide, tall = width, height
    if width / height > aspect:
        wide = max(1, int(round(height * aspect)))
    else:
        tall = max(1, int(round(width / aspect)))
    left, top = (width - wide) // 2, (height - tall) // 2
    out = []
    for row in rows[top : top + tall]:
        part = row[left * channels : (left + wide) * channels]
        if channels == 3:
            out.append(part)
            continue
        line = bytearray(wide * 3)
        for x in range(wide):
            pixel = part[x * channels : (x + 1) * channels]
            colour = (pixel[0], pixel[0], pixel[0]) if channels < 3 else pixel[:3]
            alpha = pixel[-1] if channels in (2, 4) else 255
            for c in range(3):
                line[x * 3 + c] = (colour[c] * alpha + TILE_BACKGROUND[c] * (255 - alpha)) // 255
        out.append(bytes(line))
    return _png_from_rgb(wide, tall, out)


THUMBNAIL_SIZE = 256  # the card draws it at 128 points; twice that for sharp screens
THUMBNAIL_MAX_BYTES = 512 * 1024
THUMBNAIL_KEEP_SECONDS = 7 * 24 * 3600  # a node says nothing of when its thumbnail changed: look again weekly
_renditions_refused = 0  # times, since the plugin started, the server sent the stored file instead of the small JPEG
_thumbnails_refused: set = set()  # tools whose thumbnail was not usable this session: not asked for again
_tiles_tried: set = set()  # cuts asked for this session, whatever came of it
_tiles_refused = 0  # times, since the plugin started, a request for a PNG was answered with something else


def renditions_unavailable() -> bool:
    """Is this a server from before thumbnails could be made small? After a
    few stored files there is no point asking for the rest."""
    return _renditions_refused >= 3


def option_position(field: Field, typed: str) -> int:
    """Which entry of a picker (counting from 1) text typed by hand means, or
    0 for none. As written first, since that is what would have been sent:
    an option's value, then its name; only then the same without regard to
    case, and a number written another way (1.0 for 1)."""
    typed = typed.strip()
    values = [str(value).strip() for value, _ in field.choices]
    names = [label.strip() for _, label in field.choices]
    for known in (values, names):
        if typed in known:
            return known.index(typed) + 1
    lowered = typed.lower()
    for known in ([value.lower() for value in values], [name.lower() for name in names]):
        if lowered in known:
            return known.index(lowered) + 1
    try:
        number = float(typed.replace(",", "."))
    except ValueError:
        return 0
    for position, value in enumerate(values):
        try:
            if float(value) == number:
                return position + 1
        except ValueError:
            pass
    return 0


def _never_had_a_sample(field: Field | None) -> bool:
    return field is None or field.default in (None, "")


def empty_boxes_to_refill(text: dict, before: Tool | None, after: Tool) -> dict:
    """``text`` without the empty boxes of inputs the node fills in itself
    that had nothing to show until now. When a node's lists arrive while its
    form is open, those boxes are empty because there was nothing to put in
    them, not because the user emptied them: they get the node's own sample.
    A box that had a sample and was emptied stays empty."""
    was = {field.name: field for field in (before.param_inputs if before is not None else [])}
    refill = {field.name for field in after.param_inputs if field.dynamic and _never_had_a_sample(was.get(field.name))}
    return {name: value for name, value in text.items() if not (name in refill and value == "")}


def no_tile_path(tool: Tool) -> str:
    """An empty file that says the server would not make this thumbnail's cut,
    so it is not asked for at every start."""
    return thumbnail_path(tool)[: -len(".jpg")] + ".tile.none"


def _remove_quietly(*paths: str) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def _fetch_tile(tool: Tool, fresh: bool = False) -> None:
    """A thumbnail that is not 16:9 is fetched once more as a PNG and cut to
    that shape, so it can sit in a list row like the others. Failing here
    only means the tool keeps its icon; nothing raised here leaves.

    ``fresh``: the thumbnail itself was just replaced, so the cut on disk, or
    the note that there is none, is of another picture."""
    global _tiles_refused
    cut, refused_note = tile_path(tool), no_tile_path(tool)
    if fresh:
        _remove_quietly(cut, refused_note)
        _tiles_tried.discard(cut)  # the cut that was tried was of the picture before
    if is_wide_tile(thumbnail_path(tool)) or os.path.isfile(cut):
        return
    try:
        if time.time() - os.path.getmtime(refused_note) < THUMBNAIL_KEEP_SECONDS:
            return  # the server said no to this one not long ago
    except OSError:
        pass
    if cut in _tiles_tried or _tiles_refused >= 3:
        return  # once a session for each; not at all from a server that does not make them
    _tiles_tried.add(cut)
    joiner = "&" if "?" in tool.thumbnail_url else "?"
    mine = f"{threading.get_ident():x}"  # a loader being replaced may still be fetching this same picture
    fetched, partial = f"{cut}.{mine}.fetched", f"{cut}.{mine}.part"
    refused = False
    try:
        api.download_to(fetched, f"{tool.thumbnail_url}{joiner}size={THUMBNAIL_SIZE}&format=png", timeout=30, max_bytes=4 * THUMBNAIL_MAX_BYTES)
        with open(fetched, "rb") as fh:
            tile = crop_png_to_tile(fh.read())
        if tile is None:
            _tiles_refused += 1  # not the small PNG that was asked for
            refused = True
        else:
            with open(partial, "wb") as fh:
                fh.write(tile)
            os.chmod(partial, 0o644)  # Resolve creates files anyone may write
            os.replace(partial, cut)
            _remove_quietly(refused_note)
    except api.KaitoiAPIError as exc:
        if exc.code == "TOO_LARGE":
            _tiles_refused += 1
            refused = True
        elif 400 <= exc.status < 500 and exc.status not in (401, 403, 408, 429):
            # This picture cannot be made smaller; asking again changes nothing.
            # A link that has run out (401, 403) is another matter: the next
            # start gets a new one.
            refused = True
        config.log(f"no list picture for '{tool.name}': {exc}", "WARN")
    except Exception as exc:  # noqa: BLE001 - a picture is never worth the rest of the loading
        config.log(f"no list picture for '{tool.name}': {exc}", "WARN")
    finally:
        _remove_quietly(fetched, partial)
    if refused:
        try:
            with open(refused_note, "wb"):
                pass
            os.chmod(refused_note, 0o600)
        except OSError:
            pass


_THUMBNAIL_FILE = re.compile(r"(.+)-([0-9A-Za-z]+)\.(?:jpg|tile\.png|tile\.none)")


def drop_replaced_thumbnails(workflows: list) -> int:
    """A workflow's thumbnail is named for when the workflow last changed, so
    every change leaves the one before on disk. Remove those of the workflows
    given; returns how many files went. Other tools' files are not touched."""
    directory = thumbnail_dir()
    removed = 0
    for name in os.listdir(directory):
        # Left by a download that was cut off (the window closed, Resolve
        # quit). Each has a name of its own, so nothing would ever reuse it.
        if name.endswith((".fetched", ".part")):
            path = os.path.join(directory, name)
            try:
                if time.time() - os.path.getmtime(path) > 3600:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    current: dict = {}
    for tool in workflows:
        if tool.thumbnail_url:  # one with no date keeps the name that ends in -0
            name = os.path.basename(thumbnail_path(tool))[: -len(".jpg")]
            current.setdefault(name.rsplit("-", 1)[0], set()).add(name)
    if not current:
        return removed
    for name in os.listdir(directory):
        match = _THUMBNAIL_FILE.fullmatch(name)
        if match is None:
            continue
        kept = current.get(match.group(1))
        if kept is not None and f"{match.group(1)}-{match.group(2)}" not in kept:
            try:
                os.remove(os.path.join(directory, name))
                removed += 1
            except OSError:
                pass
    return removed


def fetch_thumbnail(cfg: api.Config, tool: Tool) -> str | None:
    """Download the tool's thumbnail once; returns its path, or None.

    A thumbnail is stored as its author uploaded it: megabytes, sometimes
    WebP, which Resolve's UI kit cannot read. So a small JPEG is asked for.
    A server from before it could make one sends the stored file instead;
    that is told apart by what arrives, and is not kept.

    The link is signed and is its own permission: the API key is not sent.
    """
    if not tool.thumbnail_url:
        return None
    target = thumbnail_path(tool)
    have = os.path.isfile(target) and os.path.getsize(target) > 0
    if have and (tool.updated_at or time.time() - os.path.getmtime(target) < THUMBNAIL_KEEP_SECONDS):
        _fetch_tile(tool)  # a picture kept from before the lists could show one of its shape
        return target  # a workflow's file is named for its date; a node's is good for a week
    if tool.id in _thumbnails_refused:
        return target if have else None
    global _renditions_refused
    joiner = "&" if "?" in tool.thumbnail_url else "?"
    fetched = f"{target}.{threading.get_ident():x}.fetched"
    try:
        api.download_to(fetched, f"{tool.thumbnail_url}{joiner}size={THUMBNAIL_SIZE}&format=jpeg", timeout=30, max_bytes=THUMBNAIL_MAX_BYTES)
        with open(fetched, "rb") as fh:
            start = fh.read(3)
        if start != b"\xff\xd8\xff":
            _renditions_refused += 1
            _thumbnails_refused.add(tool.id)
            return target if have else None  # not the small JPEG that was asked for
        os.replace(fetched, target)
        os.chmod(target, 0o644)  # Resolve creates files anyone may write
        _fetch_tile(tool, fresh=True)
    except api.KaitoiAPIError as exc:
        _thumbnails_refused.add(tool.id)
        if exc.code == "TOO_LARGE":
            _renditions_refused += 1
        else:
            config.log(f"no thumbnail for '{tool.name}': {exc}", "WARN")
        return target if have else None
    except OSError as exc:
        config.log(f"no thumbnail for '{tool.name}': {exc}", "WARN")
        return target if have else None
    finally:
        try:
            os.remove(fetched)
        except OSError:
            pass
    return target


# ── Form values → request inputs ────────────────────────────────────────────

SLIDER_MOST_STEPS = 1000  # a slider with more stops than pixels gains nothing


def slider_scale(field: Field) -> tuple | None:
    """``(step, stops)`` for drawing a number as a slider: it has ``stops + 1``
    positions, ``step`` apart, from the field's minimum. None when the field
    has no usable range and is better typed."""
    low, high = field.minimum, field.maximum
    if field.widget != "number" or _number(low) is None or _number(high) is None or high <= low:
        return None
    span = high - low
    if not math.isfinite(span):
        return None  # a range too wide to mean anything on a slider
    whole = field.data_type == "int"
    step = _number(field.step)
    if step is None or step <= 0 or not math.isfinite(step):
        step = 1 if whole else span / 100
    if whole:
        step = max(1, int(round(step)))
    if span / step > SLIDER_MOST_STEPS:
        step = math.ceil(span / SLIDER_MOST_STEPS) if whole else span / SLIDER_MOST_STEPS
    if not whole and step < 1e-6:
        return None  # finer than the box beside the slider can show: better typed
    stops = int(math.floor(span / step + 1e-9))
    return (step, stops) if stops >= 1 else None


def slider_value(field: Field, position: int, scale: tuple) -> Any:
    """The number at a slider position."""
    step, stops = scale
    position = max(0, min(int(position), stops))
    value = field.minimum + position * step
    if field.data_type == "int":
        return int(round(value))
    return min(round(value, 6), field.maximum)


def slider_position(field: Field, value: Any, scale: tuple) -> int:
    """The slider position nearest to a number."""
    step, stops = scale
    return max(0, min(int(round((float(value) - field.minimum) / step)), stops))


def number_text(value: Any) -> str:
    """A number as it is shown in a box: 3, 2.5, 0.01 (never 3.0 or 1e-05)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


BOOL_CHOICES = ("Workflow default", "On", "Off")
# The entry that means "send nothing for this input": the workflow then uses
# the value saved in its template, a node its own default.
LEAVE_LABELS = {"workflow": "Workflow default", "node": "Node default"}


def leave_label(source: str, uses: str | None = None) -> str:
    """The entry that sends nothing. ``uses`` is the value the tool then
    falls back on, when it is known: "Workflow default (Zoom In)"."""
    label = LEAVE_LABELS.get(source, LEAVE_LABELS["workflow"])
    return f"{label} ({uses})" if uses not in (None, "") else label


def is_leave_label(text: str) -> bool:
    return any(text == label or (text.startswith(f"{label} (") and text.endswith(")")) for label in LEAVE_LABELS.values())


def default_label(field: Field) -> str | None:
    """A field's default as its control would show it, or None when there is none."""
    if field.default is None:
        return None
    if field.options:
        return field.choice_label(field.default)
    if field.data_type == "bool" and isinstance(field.default, bool):
        return "On" if field.default else "Off"
    return None


def bool_choices(source: str, uses: str | None = None) -> tuple:
    return (leave_label(source, uses), "On", "Off")


def parse_value(field: Field, raw: Any) -> Any:
    """Turn what the form holds into what the API takes.

    Returns None for "leave it to the workflow": an empty box, or the default
    choice. Raises :class:`ToolInputError` with the field's label when the
    value cannot be sent.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw if field.data_type == "bool" else str(raw)
    text = str(raw).strip()
    if is_leave_label(text):
        return None
    if not text and "" not in field.options:
        return None
    kind = field.widget
    if kind == "bool":
        if text in ("On", "true", "True", "1"):
            return True
        if text in ("Off", "false", "False", "0"):
            return False
        raise ToolInputError(f"{field.label}: choose On or Off.")
    if kind == "number":
        try:
            number = float(text.replace(",", "."))
        except ValueError:
            raise ToolInputError(f"{field.label}: '{text}' is not a number.") from None
        if field.data_type == "int":
            if number != int(number):
                raise ToolInputError(f"{field.label}: needs a whole number.")
            number = int(number)
        if field.minimum is not None and number < field.minimum:
            raise ToolInputError(f"{field.label}: the lowest value is {field.minimum}.")
        if field.maximum is not None and number > field.maximum:
            raise ToolInputError(f"{field.label}: the highest value is {field.maximum}.")
        return number
    if kind == "color":
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", text):
            raise ToolInputError(f"{field.label}: write a colour as #rrggbb.")
        return text if text.startswith("#") else f"#{text}"
    if kind == "choice":
        for option, label in field.choices:  # what the form shows
            if label == text:
                return option
        for option in field.options:  # or the value itself
            if str(option) == text:
                return option
        raise ToolInputError(f"{field.label}: '{text}' is not one of the choices.")
    return text


def build_inputs(tool: Tool, values: dict, file_ids: dict) -> dict:
    """The ``inputs`` body for a workflow run.

    ``values`` holds parsed values by field name, ``file_ids`` uploaded file
    ids by field name. Anything missing is left out, and the workflow then
    uses the value saved in its template.
    """
    inputs: dict = {}
    for field in tool.inputs:
        if field.media:
            file_id = file_ids.get(field.name)
            if file_id:
                inputs[field.name] = api.file_input(file_id)
            continue
        value = values.get(field.name)
        if value is not None:
            inputs[field.name] = value
    return inputs
