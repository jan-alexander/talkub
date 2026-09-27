import pytest
from helpers import need

from talkub.cli.main import main
from talkub.config import NodeSpec

HELM = "printf 'apiVersion: v2\\nname: x\\nversion: 1.2.3\\n'\n"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_node_add_static(empty_repo, capfd):
    code = _run(
        [
            "--repo",
            str(empty_repo.root),
            "node",
            "add",
            "w-01",
            "--ip",
            "10.0.0.11",
            "--role",
            "worker",
            "--mgmt",
            "w-01.tail.ts.net",
            "--static",
            "10.0.0.11/24",
            "--gateway",
            "10.0.0.1",
            "--dns",
            "1.1.1.1",
        ]
    )
    assert code == 0
    captured = capfd.readouterr()
    assert "warning: no --mac given" in captured.err
    assert "talos/nodes/w-01.yaml" in captured.out
    assert "apply --insecure w-01" in captured.out
    assert "talkub node volumes w-01" in captured.out
    assert empty_repo.load_settings().nodes["w-01"] == NodeSpec(ip="10.0.0.11", role="worker", mgmt="w-01.tail.ts.net")
    assert "kind: LinkConfig" in (empty_repo.talos_dir / "nodes" / "w-01.yaml").read_text()


def test_node_add_rejects_bad_gateway(empty_repo, capfd):
    code = _run(
        [
            "--repo",
            str(empty_repo.root),
            "node",
            "add",
            "w-01",
            "--ip",
            "10.0.0.11",
            "--static",
            "10.0.0.11/24",
            "--gateway",
            "192.168.1.1",
        ]
    )
    assert code == 1
    assert "not in 10.0.0.0/24" in capfd.readouterr().err


def test_init_node_options_need_single_node(tmp_path, capfd, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = _run(
        [
            "init",
            "demo",
            "--endpoint",
            "https://10.0.0.10:6443",
            "--git-url",
            "https://g",
            "--node",
            "cp-01=10.0.0.10",
            "--node",
            "cp-02=10.0.0.11",
            "--static",
            "10.0.0.10/24",
            "--gateway",
            "10.0.0.1",
        ]
    )
    assert code == 1
    assert "exactly one --node" in capfd.readouterr().err


@need("age", "age-keygen", "sops", "talosctl")
def test_init_with_platform_and_static_node(tmp_path, software_identity, fake_bin, monkeypatch):
    fake_bin("helm", HELM)
    monkeypatch.chdir(tmp_path)
    code = _run(
        [
            "init",
            "demo",
            "--endpoint",
            "https://10.0.0.10:6443",
            "--git-url",
            "https://git.example.com/demo.git",
            "--node",
            "cp-01=10.0.0.10",
            "--platform",
            "hcloud",
            "--static",
            "10.0.0.10/24",
            "--gateway",
            "10.0.0.1",
            "--mac",
            "aa:bb:cc:dd:ee:ff",
        ]
    )
    assert code == 0
    patch = (tmp_path / "demo" / "talos" / "nodes" / "cp-01.yaml").read_text()
    assert 'mac(link.permanent_addr) == "aa:bb:cc:dd:ee:ff"' in patch
    assert "platform: hcloud" in (tmp_path / "demo" / ".talkub.yaml").read_text()


@pytest.mark.parametrize("value", ["cp-01=-n", "cp-01=10.0.0.10/24", "cp-01=a b"])
def test_init_rejects_bad_node_address(tmp_path, capfd, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    code = _run(
        [
            "init",
            "demo",
            "--endpoint",
            "https://10.0.0.10:6443",
            "--git-url",
            "https://g",
            "--node",
            value,
        ]
    )
    assert code == 1
    assert "--node" in capfd.readouterr().err
    assert not (tmp_path / "demo").exists()


@need("age", "age-keygen", "sops", "talosctl")
def test_init_warns_when_static_address_differs(tmp_path, software_identity, fake_bin, monkeypatch, capfd):
    fake_bin("helm", HELM)
    monkeypatch.chdir(tmp_path)
    code = _run(
        [
            "init",
            "demo",
            "--endpoint",
            "https://10.0.0.50:6443",
            "--git-url",
            "https://git.example.com/demo.git",
            "--node",
            "cp-01=192.168.1.50",
            "--static",
            "10.0.0.50/24",
            "--gateway",
            "10.0.0.1",
            "--mac",
            "aa:bb:cc:dd:ee:ff",
        ]
    )
    assert code == 0
    assert "after install the node is reachable at 10.0.0.50" in capfd.readouterr().err


def test_init_rejects_invalid_node_name(tmp_path, capfd, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = _run(
        [
            "init",
            "demo",
            "--endpoint",
            "https://10.0.0.10:6443",
            "--git-url",
            "https://g",
            "--node",
            "a{x}=10.0.0.10",
            "--static",
            "10.0.0.10/24",
            "--gateway",
            "10.0.0.1",
        ]
    )
    assert code == 1
    assert "invalid node name" in capfd.readouterr().err
