"""talkub renovate init: generate renovate.json for chart, Talos and Kubernetes update PRs."""

from __future__ import annotations

import json

import pytest

from talkub.cli.main import main
from talkub.core.errors import ValidationError
from talkub.gitops import renovate


def test_render_is_valid_json_with_trailing_newline():
    text = renovate.render()
    assert text.endswith("\n")
    assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"


def test_render_contains_argocd_manager_pattern():
    data = json.loads(renovate.render())
    assert data["argocd"]["managerFilePatterns"] == ["/^gitops/apps/.+\\.ya?ml$/"]


def test_render_contains_talos_and_kubernetes_custom_managers():
    data = json.loads(renovate.render())
    managers = data["customManagers"]
    dep_names = {m["depNameTemplate"] for m in managers}
    assert dep_names == {"siderolabs/talos", "kubernetes/kubernetes"}
    for manager in managers:
        assert manager["managerFilePatterns"] == ["/^\\.talkub\\.yaml$/"]
        assert manager["customType"] == "regex"


def test_render_contains_package_rule_for_talkub_yaml():
    data = json.loads(renovate.render())
    rule = data["packageRules"][0]
    assert rule["matchFileNames"] == [".talkub.yaml"]
    assert "talkub upgrade" in rule["prBodyNotes"][0]


def test_init_writes_renovate_json(empty_repo):
    path = renovate.init(empty_repo)
    assert path == empty_repo.root / "renovate.json"
    assert path.read_text() == renovate.render()


def test_init_refuses_existing_file_without_force(empty_repo):
    renovate.init(empty_repo)
    with pytest.raises(ValidationError):
        renovate.init(empty_repo)


def test_init_force_overwrites_existing_file(empty_repo):
    path = renovate.init(empty_repo)
    path.write_text("garbage")
    renovate.init(empty_repo, force=True)
    assert path.read_text() == renovate.render()


# --- CLI ---


def test_cli_renovate_init(empty_repo, capfd):
    try:
        main(["--repo", str(empty_repo.root), "renovate", "init"])
    except SystemExit as exc:
        assert exc.code == 0
    assert (empty_repo.root / "renovate.json").is_file()
    out = capfd.readouterr().out
    assert "renovate.json" in out
    assert "https://github.com/apps/renovate" in out
    assert "talkub lint" in out


def test_cli_renovate_init_refuses_existing_file(empty_repo, capfd):
    renovate.init(empty_repo)
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(empty_repo.root), "renovate", "init"])
    assert exc.value.code == 1
    assert "error" in capfd.readouterr().err
