"""Node placeholders in shortcut definitions: syntax only."""

from __future__ import annotations

import pytest

from talkub.config.shortcuts import Shortcut, needs_node, shortcut_from_dict, shortcut_to_dict
from talkub.core.errors import ValidationError


@pytest.mark.parametrize("kind", ["run", "kubernetes"])
@pytest.mark.parametrize(
    "arg",
    ["{node}", "{node.addr}", "{node.name}", "{node.ip}", "{node.mgmt}", "{node.role}", "--nodes={node},x"],
)
def test_valid_node_placeholders(kind, arg):
    program = "t" if kind == "run" else "stern"
    shortcut = shortcut_from_dict("ext", {kind: [program, "-n", arg]})
    assert shortcut.argv == (program, "-n", arg)
    assert needs_node(shortcut)
    assert shortcut_from_dict("ext", shortcut_to_dict(shortcut)) == shortcut


def test_unknown_field_is_rejected_with_path_and_hint():
    with pytest.raises(ValidationError) as exc:
        shortcut_from_dict("ext", {"run": ["t", "-n", "{node.adr}", "get", "extensions"]})
    assert str(exc.value) == ".talkub.yaml: shortcuts.ext.run: unknown placeholder {node.adr}"
    assert exc.value.hint == "one of {node}, {node.addr}, {node.name}, {node.ip}, {node.mgmt}, {node.role}"


def test_unknown_field_in_kubernetes_is_rejected():
    with pytest.raises(ValidationError) as exc:
        shortcut_from_dict("logs", {"kubernetes": ["stern", "--node={node.host}"]})
    assert str(exc.value) == ".talkub.yaml: shortcuts.logs.kubernetes: unknown placeholder {node.host}"


@pytest.mark.parametrize(
    "arg",
    ["-o=jsonpath={.items[0].metadata.name}", "{{.status}}", "{nodes}", "{Node}", "{kubeconfig}"],
)
def test_other_braces_are_literal(arg):
    shortcut = shortcut_from_dict("x", {"run": ["k", "get", "pods", arg]})
    assert shortcut.argv[-1] == arg
    assert not needs_node(shortcut)


@pytest.mark.parametrize("arg", ["{node.ADDR}", "{node.mgmt_ip}", "{node.ip4}", "{node.}", "-n={node.Name}"])
def test_any_malformed_node_field_is_rejected(arg):
    # Typos must not slip through as literals and reach talosctl as `-n {node.mgmt_ip}`.
    with pytest.raises(ValidationError) as exc:
        shortcut_from_dict("x", {"run": ["t", "-n", arg]})
    assert "unknown placeholder {node." in str(exc.value)


def test_jsonpath_and_node_in_one_argument():
    shortcut = shortcut_from_dict("x", {"run": ["k", "get", "node", "-o=jsonpath={.metadata.name}/{node.name}"]})
    assert needs_node(shortcut)


def test_needs_node_only_for_argv_kinds():
    assert not needs_node(Shortcut(name="p", kind="run", argv=("k", "get", "pods")))
    assert not needs_node(Shortcut(name="s", kind="secret", namespace="{node}", secret="a", key="b"))
