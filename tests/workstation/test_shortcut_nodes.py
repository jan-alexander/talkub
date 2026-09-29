"""Choosing the node for a shortcut and filling its {node…} placeholders (no external tools)."""

from __future__ import annotations

import dataclasses

import pytest
from helpers import sample_settings

from talkub.config import NodeSpec, Shortcut
from talkub.core.errors import ValidationError
from talkub.workstation import shortcuts


def _settings(**nodes):
    return dataclasses.replace(sample_settings(), nodes=nodes)


THREE = _settings(
    **{
        "cp-01": NodeSpec(ip="10.0.0.10", mgmt="cp-01.tail.ts.net"),
        "cp-02": NodeSpec(ip="10.0.0.11"),
        "w-01": NodeSpec(ip="10.0.0.20", role="worker"),
    }
)
ONE = _settings(**{"cp-01": NodeSpec(ip="10.0.0.10")})


def test_first_argument_is_the_node():
    assert shortcuts.pick_node(THREE, "ext", ["w-01", "-o", "yaml"]) == ("w-01", ["-o", "yaml"])


def test_unknown_node_lists_known():
    with pytest.raises(ValidationError) as exc:
        shortcuts.pick_node(THREE, "ext", ["cp-9"])
    assert str(exc.value) == "unknown node 'cp-9'"
    assert exc.value.hint == "one of cp-01, cp-02, w-01"


def test_single_node_is_the_default():
    assert shortcuts.pick_node(ONE, "ext", []) == ("cp-01", [])


def test_leading_option_means_no_node_given():
    assert shortcuts.pick_node(ONE, "ext", ["-o", "yaml"]) == ("cp-01", ["-o", "yaml"])


def test_several_nodes_need_an_argument():
    with pytest.raises(ValidationError) as exc:
        shortcuts.pick_node(THREE, "ext", ["-o", "yaml"])
    assert str(exc.value) == "shortcut ext needs a node"
    assert exc.value.hint == "one of cp-01, cp-02, w-01"


def test_no_nodes_at_all():
    with pytest.raises(ValidationError) as exc:
        shortcuts.pick_node(_settings(), "ext", [])
    assert exc.value.hint == shortcuts.NO_NODES_HINT
    with pytest.raises(ValidationError) as exc:
        shortcuts.pick_node(_settings(), "ext", ["cp-01"])
    assert exc.value.hint == shortcuts.NO_NODES_HINT


@pytest.mark.parametrize(
    ("node", "arg", "expected"),
    [
        ("cp-01", "{node}", "cp-01.tail.ts.net"),
        ("cp-01", "{node.addr}", "cp-01.tail.ts.net"),
        ("cp-02", "{node}", "10.0.0.11"),
        ("cp-01", "{node.ip}", "10.0.0.10"),
        ("cp-01", "{node.mgmt}", "cp-01.tail.ts.net"),
        ("w-01", "{node.name}", "w-01"),
        ("w-01", "{node.role}", "worker"),
        ("cp-02", "--nodes={node},{node.name}", "--nodes=10.0.0.11,cp-02"),
        ("cp-02", "-o=jsonpath={.items[0]}/{node.name}", "-o=jsonpath={.items[0]}/cp-02"),
        ("cp-02", "{{.x}}", "{{.x}}"),
        ("cp-02", "{kubeconfig}", "{kubeconfig}"),
    ],
)
def test_fill_node(node, arg, expected):
    assert shortcuts.fill_node(["t", "-n", arg], THREE, node) == ["t", "-n", expected]


def test_missing_mgmt_is_an_error():
    with pytest.raises(ValidationError) as exc:
        shortcuts.fill_node(["t", "-n", "{node.mgmt}"], THREE, "cp-02")
    assert str(exc.value) == "node cp-02 has no mgmt address"
    assert exc.value.hint == "use {node.addr} or set mgmt on the node"


def test_listing_marks_shortcuts_that_take_a_node():
    ext = Shortcut(name="ext", kind="run", description="extensions", argv=("t", "-n", "{node}", "get", "extensions"))
    pods = Shortcut(name="pods", kind="run", argv=("k", "get", "pods"))
    lines = shortcuts.listing({"ext": (ext, "global"), "pods": (pods, "project")})
    assert [line.split() for line in lines] == [
        ["ext", "<node>", "run", "extensions", "global"],
        ["pods", "run", "project"],
    ]
    assert lines[0].index("run") == lines[1].index("run")
