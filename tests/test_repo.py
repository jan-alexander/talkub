import pytest

from helpers import sample_settings
from talkub.errors import RepoNotFound, ValidationError
from talkub.repo import MARKER, ChartSpec, NodeSpec, Repo, Settings, find_repo


def test_settings_roundtrip(tmp_path):
    settings = sample_settings()
    settings.charts["argocd"] = ChartSpec("https://argoproj.github.io/argo-helm", "argo-cd", "10.9.2")
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert repo.load_settings() == settings
    assert "talosVersion: v1.14.0" in repo.settings_file.read_text()


def test_find_repo_searches_upwards(tmp_path):
    Repo(tmp_path).write_settings(sample_settings())
    nested = tmp_path / "gitops" / "apps"
    nested.mkdir(parents=True)
    assert find_repo(nested).root == tmp_path.resolve()


def test_find_repo_explicit_path_must_be_repo(tmp_path):
    with pytest.raises(RepoNotFound):
        find_repo(tmp_path, explicit=tmp_path)


def test_find_repo_not_found(tmp_path):
    with pytest.raises(RepoNotFound) as exc:
        find_repo(tmp_path)
    assert "talkub init" in exc.value.hint


def _data(**overrides):
    data = sample_settings().to_dict()
    data.update(overrides)
    return data


def test_invalid_role_rejected():
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_data(nodes={"n1": {"ip": "10.0.0.2", "role": "master"}}))
    assert "role" in str(exc.value)


def test_endpoint_must_be_https():
    with pytest.raises(ValidationError):
        Settings.from_dict(_data(endpoint="10.0.0.10:6443"))


def test_unquoted_numeric_version_gives_hint():
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_data(kubernetesVersion=1.37))
    assert "quote" in exc.value.hint


def test_invalid_yaml_reported(tmp_path):
    (tmp_path / MARKER).write_text("name: [unclosed\n")
    with pytest.raises(ValidationError):
        Repo(tmp_path).load_settings()


def test_sops_files_excludes_sops_config_and_git(tmp_path):
    repo = Repo(tmp_path)
    (tmp_path / ".sops.yaml").write_text("creation_rules: []\n")
    (tmp_path / "talos").mkdir()
    (tmp_path / "talos" / "secrets.sops.yaml").write_text("a: b\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.sops.yaml").write_text("a: b\n")
    assert repo.sops_files() == [tmp_path / "talos" / "secrets.sops.yaml"]


def test_node_defaults_to_controlplane():
    settings = Settings.from_dict(_data(nodes={"cp-01": {"ip": "10.0.0.10"}}))
    assert settings.nodes["cp-01"] == NodeSpec(ip="10.0.0.10", role="controlplane")
