import pytest
from helpers import DEFAULT_SCHEMATIC_ID, need

from talkub.config import NodeSpec, SchematicSpec
from talkub.config.schematic import customization_hash
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.fsutil import atomic_write
from talkub.talos.nodes import node_patch_path, render_node_patch
from talkub.talos.render import (
    PatchRef,
    install_summary,
    render_configs,
    select_patches,
)


def test_select_patches_by_role(empty_repo):
    patches = empty_repo.talos_dir / "patches"
    patches.mkdir(parents=True)
    for name in (
        "worker-extra.yaml",
        "common.yaml",
        "controlplane.yaml",
        "dns.sops.yaml",
        "notes.txt",
    ):
        (patches / name).write_text("{}\n")
    assert select_patches(empty_repo, "controlplane") == [
        PatchRef("--config-patch", patches / "common.yaml"),
        PatchRef("--config-patch-control-plane", patches / "controlplane.yaml"),
        PatchRef("--config-patch", patches / "dns.sops.yaml"),
    ]
    assert select_patches(empty_repo, "worker") == [
        PatchRef("--config-patch", patches / "common.yaml"),
        PatchRef("--config-patch", patches / "dns.sops.yaml"),
        PatchRef("--config-patch-worker", patches / "worker-extra.yaml"),
    ]


def test_select_patches_without_directory(empty_repo):
    assert select_patches(empty_repo, "controlplane") == []


pytestmark_bins = need("age", "age-keygen", "sops", "talosctl")


def _docs(config: bytes):
    return [d for d in yamlio.load_all(config.decode()) if d]


@pytestmark_bins
def test_render_controlplane(talos_repo):
    settings = talos_repo.load_settings()
    configs = render_configs(Context.open(talos_repo), settings, ["cp-01"])
    text = configs["cp-01"].decode()
    assert "type: controlplane" in text
    assert "hostname: cp-01" in text
    assert f"factory.talos.dev/metal-installer/{DEFAULT_SCHEMATIC_ID}:v1.14.0" in text
    assert install_summary(configs["cp-01"]) == (
        f"factory.talos.dev/metal-installer/{DEFAULT_SCHEMATIC_ID}:v1.14.0",
        'disk.dev_path == "/dev/sda"',
    )


@pytestmark_bins
def test_render_applies_encrypted_patch(talos_repo):
    ctx = Context.open(talos_repo)
    patch = talos_repo.talos_dir / "patches" / "dns.sops.yaml"
    plain = b"apiVersion: v1alpha1\nkind: ResolverConfig\nnameservers:\n  - address: 9.9.9.9\n"
    atomic_write(
        patch,
        ctx.sops.encrypt(
            plain, filename=patch.name, public_key=ctx.keyring.public_key()
        ),
    )
    configs = render_configs(ctx, talos_repo.load_settings(), ["cp-01"])
    assert "9.9.9.9" in configs["cp-01"].decode()


@pytestmark_bins
def test_render_hcloud_worker_and_sans(talos_repo):
    settings = talos_repo.load_settings()
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.tail.ts.net")
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker", platform="hcloud")
    settings.schematics["hcloud"] = SchematicSpec(
        id=DEFAULT_SCHEMATIC_ID, hash=customization_hash([], [])
    )
    atomic_write(
        node_patch_path(talos_repo, "w-01"), render_node_patch("w-01").encode()
    )
    configs = render_configs(Context.open(talos_repo), settings, ["cp-01", "w-01"])
    assert "cp-01.tail.ts.net" in configs["cp-01"].decode()
    worker = configs["w-01"].decode()
    assert "type: worker" in worker
    assert "hcloud-installer" in worker


@pytestmark_bins
def test_render_adds_tailnet_sans_for_controlplanes(talos_repo):
    from talkub.config import TailscaleSpec

    settings = talos_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net", node_cidr="10.0.0.0/24")
    configs = render_configs(Context.open(talos_repo), settings, ["cp-01"])
    assert "cp-01.t.ts.net" in configs["cp-01"].decode()


@pytestmark_bins
def test_render_does_not_duplicate_tailnet_san_already_used_as_mgmt(talos_repo):
    from talkub.config import TailscaleSpec

    settings = talos_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net", node_cidr="10.0.0.0/24")
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.t.ts.net")
    configs = render_configs(Context.open(talos_repo), settings, ["cp-01"])
    for doc in yamlio.load_all(configs["cp-01"].decode()):
        if isinstance(doc, dict) and doc.get("version") == "v1alpha1":
            sans = ((doc.get("machine") or {}).get("certSANs")) or []
            assert sans.count("cp-01.t.ts.net") == 1


def _cert_sans(config: bytes) -> list[str]:
    for doc in _docs(config):
        if isinstance(doc, dict) and doc.get("version") == "v1alpha1":
            return list((doc.get("machine") or {}).get("certSANs") or [])
    return []


@pytestmark_bins
def test_render_worker_gets_its_own_tailnet_and_mgmt_names(talos_repo):
    """After lockdown talkub connects to every node by its own name, so each node's
    certificate must cover it, not only the control planes' names."""
    from talkub.config import TailscaleSpec

    settings = talos_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net", node_cidr="10.0.0.0/24")
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.20", role="worker")
    settings.nodes["w-02"] = NodeSpec(ip="10.0.0.21", role="worker", mgmt="w-02.lan")
    settings.nodes["w-03"] = NodeSpec(ip="10.0.0.22", role="worker", mgmt="10.0.0.99")
    for name in ("w-01", "w-02", "w-03"):
        atomic_write(
            node_patch_path(talos_repo, name), render_node_patch(name).encode()
        )
    configs = render_configs(
        Context.open(talos_repo), settings, ["cp-01", "w-01", "w-02", "w-03"]
    )
    assert "w-01.t.ts.net" in _cert_sans(configs["w-01"])
    assert "cp-01.t.ts.net" in _cert_sans(configs["w-01"])
    w02 = _cert_sans(configs["w-02"])
    assert "w-02.lan" in w02 and "w-02.t.ts.net" in w02
    assert len(w02) == len(set(w02))
    assert "10.0.0.99" not in _cert_sans(
        configs["w-03"]
    )  # an IP mgmt is not added as a name
    assert "w-01.t.ts.net" not in _cert_sans(configs["cp-01"])


@pytestmark_bins
def test_render_worker_without_tailscale_gets_its_mgmt_name(talos_repo):
    settings = talos_repo.load_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.20", role="worker", mgmt="w-01.lan")
    atomic_write(
        node_patch_path(talos_repo, "w-01"), render_node_patch("w-01").encode()
    )
    configs = render_configs(Context.open(talos_repo), settings, ["w-01"])
    assert "w-01.lan" in _cert_sans(configs["w-01"])


@pytestmark_bins
def test_invalid_patch_is_reported_with_node_name(talos_repo):
    patches = talos_repo.talos_dir / "patches"
    patches.mkdir(parents=True, exist_ok=True)
    (patches / "common.yaml").write_text("machine:\n  network:\n    hostname: clash\n")
    with pytest.raises(ValidationError) as exc:
        render_configs(Context.open(talos_repo), talos_repo.load_settings(), ["cp-01"])
    assert "cp-01" in str(exc.value)
    assert "hostname" in exc.value.hint


@pytestmark_bins
def test_missing_node_patch(talos_repo):
    node_patch_path(talos_repo, "cp-01").unlink()
    with pytest.raises(ValidationError) as exc:
        render_configs(Context.open(talos_repo), talos_repo.load_settings(), ["cp-01"])
    assert "node add" in exc.value.hint


def test_outdated_schematic_is_not_rendered(empty_repo):
    settings = empty_repo.load_settings()
    settings.schematics["metal"].extensions.append("siderolabs/tailscale")
    with pytest.raises(ValidationError):
        render_configs(Context.open(empty_repo), settings, ["cp-01"])
