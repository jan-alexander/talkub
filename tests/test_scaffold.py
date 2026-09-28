import subprocess

import pytest
from helpers import need

from talkub.config import NodeSpec
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.ephemeral import decrypted
from talkub.core.errors import CommandFailed, ValidationError
from talkub.core.runner import Runner
from talkub.scaffold import TEMPLATES, InitRequest, init_cluster
from talkub.secrets.recipients import Recipient

pytestmark = need("age", "age-keygen", "sops", "talosctl")

HELM = "printf 'apiVersion: v2\\nname: x\\nversion: 1.2.3\\n'\n"


def _request(target, public, platform="metal"):
    return InitRequest(
        target=target,
        name="demo",
        endpoint="https://10.0.0.10:6443",
        git_url="https://git.example.com/demo.git",
        nodes={"cp-01": NodeSpec(ip="10.0.0.10")},
        recipients=[Recipient("me", public)],
        platform=platform,
    )


def test_init_creates_complete_repo(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    repo = init_cluster(_request(tmp_path / "demo", software_identity[1]), Runner())

    settings = repo.load_settings()
    assert settings.name == "demo"
    assert settings.nodes == {"cp-01": NodeSpec(ip="10.0.0.10")}
    assert "charts" not in repo.settings_file.read_text()
    assert settings.talos_version.startswith("v")
    assert settings.platform == "metal"
    assert settings.schematics["metal"].is_current
    assert settings.installer_image("metal").startswith("factory.talos.dev/metal-installer/")

    for rel in (
        ".gitignore",
        ".sops.yaml",
        ".yamllint",
        "README.md",
        "keys/recipients.txt",
        "keys/master.age",
        "keys/master.pub",
        "talos/secrets.sops.yaml",
        "talos/talosconfig.sops.yaml",
        "talos/patches/controlplane.yaml",
        "talos/patches/controlplane-coredns.yaml",
        "talos/nodes/cp-01.yaml",
        "gitops/root.yaml",
        "gitops/apps/argocd.yaml",
        "gitops/apps/sealed-secrets.yaml",
        "gitops/apps/coredns.yaml",
        "gitops/values/argocd.yaml",
        "gitops/values/sealed-secrets.yaml",
        "gitops/values/coredns.yaml",
        "gitops/manifests/.gitkeep",
    ):
        assert (repo.root / rel).is_file(), rel

    coredns_values = yamlio.load((repo.root / "gitops/values/coredns.yaml").read_text())
    assert "servers" in coredns_values and len(coredns_values["servers"]) == 1  # no tailnet stub zone yet
    coredns_app = yamlio.load((repo.root / "gitops/apps/coredns.yaml").read_text())
    assert coredns_app["spec"]["sources"][1]["repoURL"] == "https://git.example.com/demo.git"
    assert coredns_app["spec"]["sources"][0]["targetRevision"] == "1.2.3"
    patch = yamlio.load((repo.root / "talos/patches/controlplane-coredns.yaml").read_text())
    assert patch == {
        "apiVersion": "v1alpha1",
        "kind": "KubeCoreDNSConfig",
        "enabled": False,
    }

    root_app = yamlio.load((repo.root / "gitops/root.yaml").read_text())
    assert root_app["spec"]["source"]["repoURL"] == "https://git.example.com/demo.git"
    argocd_app = yamlio.load((repo.root / "gitops/apps/argocd.yaml").read_text())
    assert argocd_app["spec"]["sources"][0]["targetRevision"] == "1.2.3"
    assert argocd_app["spec"]["sources"][0]["helm"]["valueFiles"] == ["$values/gitops/values/argocd.yaml"]

    ctx = Context.open(repo)
    talosconfig = yamlio.load(ctx.sops.decrypt(repo.talosconfig).decode())
    assert list(talosconfig["contexts"]["demo"]["endpoints"]) == ["10.0.0.10"]

    from talkub.config import GitSpec

    assert settings.git == GitSpec(url="https://git.example.com/demo.git")

    from talkub.quality.lint import DEFAULT_YAMLLINT

    assert (repo.root / ".yamllint").read_text() == DEFAULT_YAMLLINT


def test_generated_patches_validate_with_talosctl(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    repo = init_cluster(_request(tmp_path / "demo", software_identity[1]), Runner())
    ctx = Context.open(repo)
    with decrypted(ctx.sops, {"secrets.yaml": repo.talos_secrets}) as paths:
        out = paths["secrets.yaml"].parent / "cp.yaml"
        subprocess.run(
            [
                "talosctl",
                "gen",
                "config",
                "demo",
                "https://10.0.0.10:6443",
                "--with-secrets",
                str(paths["secrets.yaml"]),
                "-t",
                "controlplane",
                "-o",
                str(out),
                "--with-docs=false",
                "--with-examples=false",
                "--config-patch-control-plane",
                f"@{repo.root / 'talos/patches/controlplane.yaml'}",
                "--config-patch",
                f"@{repo.root / 'talos/nodes/cp-01.yaml'}",
            ],
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            ["talosctl", "validate", "--config", str(out), "--mode", "metal"],
            capture_output=True,
            text=True,
            check=False,
        )
        rendered = out.read_text()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hostname: cp-01" in rendered
    assert "NoSchedule" not in rendered


def test_non_empty_target_refused(tmp_path, software_identity):
    target = tmp_path / "demo"
    target.mkdir()
    (target / "something").write_text("x")
    with pytest.raises(ValidationError):
        init_cluster(_request(target, software_identity[1]), Runner())


def test_existing_git_dir_is_allowed(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    target = tmp_path / "demo"
    (target / ".git").mkdir(parents=True)
    init_cluster(_request(target, software_identity[1]), Runner())
    assert (target / ".talkub.yaml").is_file()


def test_failed_helm_lookup_writes_nothing(tmp_path, software_identity, fake_bin):
    fake_bin("helm", "echo 'network unreachable' >&2; exit 1\n")
    target = tmp_path / "demo"
    with pytest.raises(CommandFailed):
        init_cluster(_request(target, software_identity[1]), Runner())
    assert not target.exists()


def test_old_talosctl_refused_before_anything_runs(tmp_path, software_identity, fake_bin):
    log = tmp_path / "helm.log"
    fake_bin("helm", f"echo called >> {log}\n")
    fake_bin("talosctl", "printf 'Client:\\n\\tTag:         v1.9.0\\n'\n")
    target = tmp_path / "demo"
    with pytest.raises(ValidationError, match=r"talosctl v1\.9\.0 is too old"):
        init_cluster(_request(target, software_identity[1]), Runner())
    assert not target.exists()
    assert not log.exists()


def test_failure_after_writing_cleans_up_and_can_be_retried(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    # Fails after keys/ has been written, so cleanup is really exercised.
    broken_talosctl = fake_bin("talosctl", "echo 'boom' >&2; exit 1\n")
    target = tmp_path / "demo"
    (target / ".git").mkdir(parents=True)
    with pytest.raises(CommandFailed):
        init_cluster(_request(target, software_identity[1]), Runner())
    assert sorted(p.name for p in target.iterdir()) == [".git"]

    broken_talosctl.unlink()
    init_cluster(_request(target, software_identity[1]), Runner())
    assert (target / "keys/master.age").is_file()


def test_target_that_is_a_file_refused(tmp_path, software_identity):
    target = tmp_path / "demo"
    target.write_text("x")
    with pytest.raises(ValidationError):
        init_cluster(_request(target, software_identity[1]), Runner())
    assert target.read_text() == "x"


def test_existing_ds_store_is_allowed(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    target = tmp_path / "demo"
    target.mkdir()
    (target / ".DS_Store").write_bytes(b"\0")
    init_cluster(_request(target, software_identity[1]), Runner())
    assert (target / ".talkub.yaml").is_file()


def test_repo_readme_template_has_no_placeholder_link():
    text = (TEMPLATES / "repo" / "README.md.tmpl").read_text()
    assert "https://github.com/)" not in text


def test_needs_a_controlplane_node(tmp_path, software_identity):
    request = _request(tmp_path / "demo", software_identity[1])
    request.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", role="worker")
    with pytest.raises(ValidationError):
        init_cluster(request, Runner())


def test_init_with_hcloud_platform(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    repo = init_cluster(_request(tmp_path / "demo", software_identity[1], platform="hcloud"), Runner())
    settings = repo.load_settings()
    assert settings.platform == "hcloud"
    assert settings.installer_image("hcloud").startswith("factory.talos.dev/hcloud-installer/")


def test_init_rejects_unknown_platform(tmp_path, software_identity):
    with pytest.raises(ValidationError):
        init_cluster(
            _request(tmp_path / "demo", software_identity[1], platform="vmware"),
            Runner(),
        )


def test_init_rejects_invalid_git_url(tmp_path, software_identity):
    request = _request(tmp_path / "demo", software_identity[1])
    from dataclasses import replace

    with pytest.raises(ValidationError):
        init_cluster(replace(request, git_url="http://git.example.com/demo.git"), Runner())
    assert not (tmp_path / "demo").exists()
