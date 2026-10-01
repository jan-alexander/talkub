import pytest
from helpers import DEFAULT_SCHEMATIC_ID, sample_settings

from talkub.config import (
    MARKER,
    NodeSpec,
    Repo,
    SchematicSpec,
    Settings,
    Shortcut,
    TailscaleSpec,
    find_repo,
    parse_installer_image,
    valid_label,
)
from talkub.config.schematic import customization_hash
from talkub.core.errors import RepoNotFound, ValidationError


def test_settings_roundtrip(tmp_path):
    settings = sample_settings()
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert repo.load_settings() == settings
    assert "talosVersion: v1.14.0" in repo.settings_file.read_text()


def test_legacy_charts_block_is_ignored_and_dropped_on_write(tmp_path):
    legacy = {
        "argocd": {
            "repo": "https://argoproj.github.io/argo-helm",
            "chart": "argo-cd",
            "version": "10.9.2",
        }
    }
    settings = Settings.from_dict(_data(charts=legacy))
    assert settings == sample_settings()
    assert "charts" not in settings.to_dict()
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert "charts" not in repo.settings_file.read_text()
    assert Settings.from_dict(_data(charts="garbage")) == sample_settings()  # not even validated


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


def test_full_settings_roundtrip(tmp_path):
    settings = sample_settings()
    settings.platform = "hcloud"
    settings.schematics["hcloud"] = SchematicSpec(
        extensions=["siderolabs/tailscale"],
        kernel_args=["console=ttyS0"],
        id="abc",
        hash="def",
    )
    settings.nodes["w-01"] = NodeSpec(
        ip="10.0.0.11",
        role="worker",
        mgmt="w-01.tail.ts.net",
        platform="metal",
        arch="arm64",
    )
    settings.tailscale = TailscaleSpec(tailnet="tail.ts.net")
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert repo.load_settings() == settings
    text = repo.settings_file.read_text()
    assert "installImage" not in text
    assert "kernelArgs" in text and "nodeCidr" not in text
    assert "useIps" not in text  # default False is not written


def test_tailscale_use_ips_roundtrip(tmp_path):
    settings = sample_settings()
    settings.tailscale = TailscaleSpec(tailnet="tail.ts.net", use_ips=True)
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert repo.load_settings() == settings
    assert "useIps: true" in repo.settings_file.read_text()


def test_tailscale_use_ips_defaults_false_and_parses_strictly():
    data = sample_settings().to_dict()
    data["tailscale"] = {"tailnet": "t.ts.net"}
    assert Settings.from_dict(data).tailscale.use_ips is False
    data["tailscale"]["useIps"] = "true"  # not a bool
    with pytest.raises(ValidationError):
        Settings.from_dict(data)


def test_tailscale_legacy_node_cidr_is_ignored_and_dropped(tmp_path):
    data = sample_settings().to_dict()
    data["tailscale"] = {"tailnet": "t.ts.net", "nodeCidr": "10.0.0.0/24"}
    settings = Settings.from_dict(data)
    assert settings.tailscale == TailscaleSpec(tailnet="t.ts.net")
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert "nodeCidr" not in repo.settings_file.read_text()


def test_tailscale_needs_tailnet():
    data = sample_settings().to_dict()
    data["tailscale"] = {"useIps": True}
    with pytest.raises(ValidationError, match="tailnet"):
        Settings.from_dict(data)


def test_phase1_install_image_is_migrated():
    data = sample_settings().to_dict()
    del data["platform"]
    del data["schematics"]
    data["installImage"] = f"factory.talos.dev/metal-installer/{DEFAULT_SCHEMATIC_ID}"
    settings = Settings.from_dict(data)
    assert settings.platform == "metal"
    assert settings.schematics["metal"].id == DEFAULT_SCHEMATIC_ID
    assert settings.schematics["metal"].is_current
    assert "installImage" not in settings.to_dict()


def test_phase1_install_image_must_be_a_factory_image():
    data = sample_settings().to_dict()
    del data["schematics"]
    data["installImage"] = "ghcr.io/siderolabs/installer"
    with pytest.raises(ValidationError):
        Settings.from_dict(data)


def test_parse_installer_image():
    assert parse_installer_image("factory.talos.dev/hcloud-installer/abc123") == (
        "hcloud",
        "abc123",
    )


def test_unknown_platform_and_arch_rejected():
    with pytest.raises(ValidationError):
        Settings.from_dict(_data(platform="vmware"))
    with pytest.raises(ValidationError):
        Settings.from_dict(_data(nodes={"n1": {"ip": "10.0.0.2", "arch": "riscv"}}))


def test_installer_image_and_node_helpers():
    settings = sample_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker", mgmt="w-01.ts", platform="hcloud")
    assert settings.installer_image("metal") == f"factory.talos.dev/metal-installer/{DEFAULT_SCHEMATIC_ID}:v1.14.0"
    assert settings.node_platform("cp-01") == "metal"
    assert settings.node_platform("w-01") == "hcloud"
    assert settings.mgmt_address("cp-01") == "10.0.0.10"
    assert settings.mgmt_address("w-01") == "w-01.ts"
    with pytest.raises(ValidationError):
        settings.node("nope")


def test_installer_image_refuses_outdated_schematic():
    settings = sample_settings()
    settings.schematics["metal"].extensions.append("siderolabs/tailscale")
    assert not settings.schematics["metal"].is_current
    with pytest.raises(ValidationError) as exc:
        settings.installer_image("metal")
    assert "outdated" in str(exc.value)


def test_ensure_schematic_adds_empty_entry():
    settings = sample_settings()
    spec = settings.ensure_schematic("hcloud")
    assert spec == SchematicSpec()
    assert settings.schematics["hcloud"] is spec
    with pytest.raises(ValidationError):
        settings.ensure_schematic("vmware")


def test_schematic_is_current_only_with_matching_hash():
    assert SchematicSpec(id="x", hash=customization_hash([], [])).is_current
    assert not SchematicSpec(id=None, hash=customization_hash([], [])).is_current
    assert not SchematicSpec(id="x", hash="other").is_current


def test_git_block_round_trips(tmp_path):
    from helpers import sample_settings

    from talkub.config import GitSpec, Repo

    repo = Repo(tmp_path)
    settings = sample_settings()
    assert settings.git is None
    repo.write_settings(settings)
    assert "git:" not in repo.settings_file.read_text()  # absent block stays absent
    settings.git = GitSpec(url="git@github.com:me/c.git")
    repo.write_settings(settings)
    assert repo.load_settings().git == GitSpec(url="git@github.com:me/c.git")


@pytest.mark.parametrize("raw", ["git: nope\n", "git: {}\n", "git: {url: ''}\n"])
def test_git_block_must_have_url(tmp_path, raw):
    from helpers import sample_settings

    from talkub.config import Repo
    from talkub.core import yamlio
    from talkub.core.errors import ValidationError

    repo = Repo(tmp_path)
    repo.write_settings(sample_settings())
    data = yamlio.load(repo.settings_file.read_text())
    data["git"] = yamlio.load(raw)["git"]
    repo.settings_file.write_text(yamlio.dump(data))
    with pytest.raises(ValidationError):
        repo.load_settings()


# --- shortcuts ---


def test_valid_label():
    assert valid_label("cp-01")
    assert valid_label("a")
    assert not valid_label("")
    assert not valid_label("Cp-01")
    assert not valid_label("-cp01")
    assert not valid_label("cp01-")
    assert not valid_label("cp_01")
    assert not valid_label("a" * 64)
    assert valid_label("a" * 63)


def _shortcuts_data(**shortcuts):
    return _data(shortcuts=shortcuts)


def test_shortcuts_full_example_roundtrip(tmp_path):
    settings = sample_settings()
    settings.shortcuts = {
        "creds": Shortcut(
            name="creds",
            kind="secret",
            description="db password",
            namespace="prod",
            secret="db-creds",
            key="password",
        ),
        "backup": Shortcut(
            name="backup",
            kind="job",
            namespace="prod",
            cronjob="backup-job",
            follow=True,
        ),
        "argocd-password": Shortcut(
            name="argocd-password",
            kind="run",
            argv=("kubectl", "get", "secret", "argocd-initial-admin-secret"),
        ),
        "grafana": Shortcut(
            name="grafana",
            kind="portForward",
            namespace="monitoring",
            service="grafana",
            ports=("3000:3000", "8080"),
        ),
        "k9s": Shortcut(name="k9s", kind="kubernetes", argv=("k9s", "--kubeconfig={kubeconfig}")),
    }
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert repo.load_settings() == settings
    text = repo.settings_file.read_text()
    assert "secret:" in text
    assert "job:" in text
    assert "run:" in text
    assert "portForward:" in text
    assert "kubernetes:" in text
    assert "follow: true" in text


def test_shortcuts_absent_block_not_written(tmp_path):
    repo = Repo(tmp_path)
    repo.write_settings(sample_settings())
    assert "shortcuts:" not in repo.settings_file.read_text()


def test_shortcut_job_follow_only_written_when_true(tmp_path):
    settings = sample_settings()
    settings.shortcuts = {"backup": Shortcut(name="backup", kind="job", namespace="prod", cronjob="cj")}
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert "follow" not in repo.settings_file.read_text()


def test_shortcuts_must_be_a_mapping():
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_data(shortcuts=["nope"]))
    assert "shortcuts" in str(exc.value)


def test_shortcut_entry_must_be_a_mapping():
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_shortcuts_data(backup="nope"))
    assert "shortcuts.backup" in str(exc.value)


def test_shortcut_name_must_be_valid_label():
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_shortcuts_data(Bad_Name={"run": ["k", "get", "ns"]}))
    assert "shortcuts.Bad_Name" in str(exc.value)


@pytest.mark.parametrize("spec", [{}, {"run": ["k", "x"], "job": {"namespace": "a", "cronjob": "b"}}])
def test_shortcut_must_have_exactly_one_kind(spec):
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_shortcuts_data(backup=spec))
    assert "shortcuts.backup" in str(exc.value)


def test_shortcut_description_must_be_non_empty_string():
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_shortcuts_data(backup={"description": "", "run": ["k", "x"]}))
    assert "shortcuts.backup.description" in str(exc.value)


@pytest.mark.parametrize(
    "secret,path",
    [
        (
            {"namespace": "Bad_NS", "name": "s", "key": "k"},
            "shortcuts.backup.secret.namespace",
        ),
        (
            {"namespace": "prod", "name": "Bad_Name", "key": "k"},
            "shortcuts.backup.secret.name",
        ),
        (
            {"namespace": "prod", "name": "s", "key": "bad key!"},
            "shortcuts.backup.secret.key",
        ),
        ({"namespace": "prod", "name": "s", "key": ""}, "shortcuts.backup.secret.key"),
    ],
)
def test_shortcut_secret_field_rules(secret, path):
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_shortcuts_data(backup={"secret": secret}))
    assert path in str(exc.value)


def test_shortcut_secret_key_may_contain_dot_and_underscore():
    settings = Settings.from_dict(
        _shortcuts_data(backup={"secret": {"namespace": "prod", "name": "s", "key": "tls.crt_1"}})
    )
    assert settings.shortcuts["backup"].key == "tls.crt_1"


@pytest.mark.parametrize(
    "job,path",
    [
        ({"namespace": "Bad_NS", "cronjob": "cj"}, "shortcuts.backup.job.namespace"),
        ({"namespace": "prod", "cronjob": "Bad_CJ"}, "shortcuts.backup.job.cronjob"),
        (
            {"namespace": "prod", "cronjob": "cj", "follow": "yes"},
            "shortcuts.backup.job.follow",
        ),
    ],
)
def test_shortcut_job_field_rules(job, path):
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_shortcuts_data(backup={"job": job}))
    assert path in str(exc.value)


@pytest.mark.parametrize(
    "run",
    [
        [],
        ["kubectl", 1],
        ["helm", "get"],
    ],
)
def test_shortcut_run_field_rules(run):
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_shortcuts_data(backup={"run": run}))
    assert "shortcuts.backup.run" in str(exc.value)


@pytest.mark.parametrize("argv", [[], "k9s", ["k9s", 1], [""], [" ", "x"]])
def test_shortcut_kubernetes_field_rules(argv):
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_shortcuts_data(backup={"kubernetes": argv}))
    assert "shortcuts.backup.kubernetes" in str(exc.value)


def test_shortcut_kubernetes_accepts_any_program_and_empty_arguments():
    settings = Settings.from_dict(_shortcuts_data(ui={"kubernetes": ["/opt/bin/k9s", "", "{{.name}}"]}))
    shortcut = settings.shortcuts["ui"]
    assert shortcut.kind == "kubernetes"
    assert shortcut.argv == ("/opt/bin/k9s", "", "{{.name}}")


@pytest.mark.parametrize(
    "pf,path",
    [
        (
            {"namespace": "Bad_NS", "service": "svc", "ports": ["80"]},
            "shortcuts.backup.portForward.namespace",
        ),
        (
            {"namespace": "prod", "service": "Bad_SVC", "ports": ["80"]},
            "shortcuts.backup.portForward.service",
        ),
        (
            {"namespace": "prod", "service": "svc", "ports": []},
            "shortcuts.backup.portForward.ports",
        ),
        (
            {"namespace": "prod", "service": "svc", "ports": ["0"]},
            "shortcuts.backup.portForward.ports",
        ),
        (
            {"namespace": "prod", "service": "svc", "ports": ["70000"]},
            "shortcuts.backup.portForward.ports",
        ),
        (
            {"namespace": "prod", "service": "svc", "ports": ["abc"]},
            "shortcuts.backup.portForward.ports",
        ),
        (
            {"namespace": "prod", "service": "svc", "ports": ["1:2:3"]},
            "shortcuts.backup.portForward.ports",
        ),
    ],
)
def test_shortcut_portforward_field_rules(pf, path):
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_shortcuts_data(backup={"portForward": pf}))
    assert path in str(exc.value)


def test_shortcut_portforward_accepts_single_and_mapped_ports():
    settings = Settings.from_dict(
        _shortcuts_data(
            backup={
                "portForward": {
                    "namespace": "prod",
                    "service": "svc",
                    "ports": ["8080", "3000:3000"],
                }
            }
        )
    )
    assert settings.shortcuts["backup"].ports == ("8080", "3000:3000")
