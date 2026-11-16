"""Running one node: the inline graph built around it.

A workflow has an endpoint to call. A single node does not, so the plugin
builds the smallest graph that runs it: the node itself, with one loader in
front of every clip, frame or sound it is given.

    file ─▶ loader ─▶ ┐
    file ─▶ loader ─▶ ┤ node ─▶ results
            values ─▶ ┘

Nothing about a node is hard-coded. Pin names and types are read from the
node's live schema, a value is sent with the type its pin declares, and every
pin left alone gets the catalog's own default.
"""

from __future__ import annotations

from typing import Any

from . import api, tools

LOADERS = {
    "video": "builtin/loaders/load_video",
    "image": "builtin/loaders/load_image",
    "audio": "builtin/loaders/load_audio",
}
TARGET_NODE_ID = "node"

# Kaitoi pin type -> the typed value an inline graph takes.
_TYPED = {"string": "string", "color": "string", "int": "number", "float": "number", "bool": "boolean"}


class GraphBuildError(RuntimeError):
    pass


def _loader_pins(config: api.Config, media: str, cache: dict) -> tuple[str, str, str]:
    """``(loader type, its input pin, its output pin)`` for one kind of media."""
    if media not in cache:
        loader_type = LOADERS.get(media)
        if not loader_type:
            raise GraphBuildError(f"Kaitoi has no loader for {media} files.")
        schema = api.get_node_type(config, loader_type)
        pin_in = api.find_input_pin(schema, media)
        pin_out = api.find_output_pin(schema, media)
        if not pin_in or not pin_out:
            raise GraphBuildError(f"{loader_type} exposes no {media} pins")
        cache[media] = (loader_type, pin_in, pin_out)
    return cache[media]


def build_node_graph(
    config: api.Config,
    tool: tools.Tool,
    values: dict[str, Any],
    file_ids: dict[str, str],
) -> dict[str, Any]:
    """Build the inline graph that runs ``tool`` (a node) once.

    ``values`` are its form values by pin name (None means "the node's own
    default"); ``file_ids`` are uploaded files by pin name. Returns
    ``{graph, target, overrides, title}``: ``overrides`` binds each uploaded
    file to its loader for this run.

    Network calls happen here (the node's schema and one per kind of loader),
    so call it off the UI thread.
    """
    if not tool.node_type:
        raise GraphBuildError(f"'{tool.name}' is not a node.")
    schema = api.get_node_type(config, tool.node_type)
    pins = {str(pin.get("name")): pin for pin in schema.get("inputs") or [] if pin.get("name")}

    nodes: list[dict[str, Any]] = []
    connections: list[dict[str, Any]] = []
    overrides: dict[str, dict[str, Any]] = {}
    wired: set[str] = set()
    loaders: dict = {}
    for field in tool.media_inputs:
        file_id = file_ids.get(field.name)
        if not file_id:
            continue
        if field.name not in pins:
            raise GraphBuildError(f"{tool.name} no longer has an input called '{field.name}'. Reload the node list.")
        loader_type, pin_in, pin_out = _loader_pins(config, field.media, loaders)
        loader_id = f"load_{field.name}"
        nodes.append({"id": loader_id, "type": loader_type, "inputs": {}})
        connections.append({"from": [loader_id, pin_out], "to": [TARGET_NODE_ID, field.name]})
        overrides[loader_id] = {pin_in: api.file_input(file_id)}
        wired.add(field.name)

    inputs = api.autofill_node_inputs(schema, skip_pins=wired)
    for field in tool.param_inputs:
        value = values.get(field.name)
        if value is None and field.dynamic and field.options:
            # A picker the node filled in itself, left on "Node default (Aria)":
            # the first voice of the list it fetched. That lives only in the
            # node's state in Studio, not in the catalog, so nothing else would
            # send it. A plain box the node only wrote a sample into is
            # different: emptied, it is left out like any other empty box,
            # and the node runs with its own value.
            value = field.default
        if value is None or field.name not in pins:
            continue
        inputs[field.name] = {"type": _TYPED.get(field.data_type, "json"), "value": value}
    nodes.append({"id": TARGET_NODE_ID, "type": tool.node_type, "inputs": inputs})
    return {
        "graph": {"nodes": nodes, "connections": connections},
        "target": TARGET_NODE_ID,
        "overrides": overrides,
        "title": str(schema.get("title") or tool.name),
    }


def describe(built: dict[str, Any]) -> str:
    graph = built["graph"]
    wires = ", ".join(f"{c['from'][0]}.{c['from'][1]}→{c['to'][0]}.{c['to'][1]}" for c in graph["connections"])
    return f"{built['title']} [{wires or 'no files'}]"
