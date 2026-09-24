from talkub.talos import inspect

VERSION = """Client:
\tTag:         v1.14.0
\tSHA:         9abd05af
Server:
\tNODE:        10.0.0.10
\tTag:         v1.13.2
\tSHA:         0123abcd
"""

EXTENSIONS = """{
    "node": "10.0.0.10",
    "metadata": {"namespace": "runtime", "type": "ExtensionStatuses.runtime.talos.dev", "id": "0"},
    "spec": {"image": "0.sqsh", "metadata": {"name": "siderolabs/intel-ucode", "version": "20260812"}}
}
{
    "node": "10.0.0.10",
    "metadata": {"namespace": "runtime", "type": "ExtensionStatuses.runtime.talos.dev", "id": "1"},
    "spec": {"image": "1.sqsh", "metadata": {"name": "schematic", "version": "188cef51"}}
}
"""

NODES = """{"items": [
  {"metadata": {"name": "cp-01"}, "status": {"nodeInfo": {"kubeletVersion": "v1.37.0"}}},
  {"metadata": {"name": "w-01"}, "status": {"nodeInfo": {"kubeletVersion": "v1.36.2"}}}
]}"""

APPS = """{"items": [
  {"metadata": {"name": "root"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}},
  {"metadata": {"name": "argocd"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}},
  {"metadata": {"name": "app"}, "status": {"sync": {"status": "OutOfSync"}}}
]}"""


def test_server_version():
    assert inspect.server_version(VERSION) == "v1.13.2"
    assert inspect.server_version("Client:\n\tTag: v1.14.0\n") is None
    assert inspect.server_version("garbage") is None


def test_json_documents_stream_and_garbage():
    assert inspect.json_documents('{"a": 1}\n{"b": 2}') == [{"a": 1}, {"b": 2}]
    assert inspect.json_documents('{"a": 1} not json') == [{"a": 1}]
    assert inspect.json_documents("") == []


def test_extensions_and_schematic():
    assert inspect.extensions(EXTENSIONS) == {"siderolabs/intel-ucode": "20260812", "schematic": "188cef51"}
    assert inspect.schematic_id(EXTENSIONS) == "188cef51"
    assert inspect.schematic_id("[]") is None
    assert inspect.schematic_id("{not json") is None


def test_bootstrap_already_done():
    assert inspect.bootstrap_already_done("error: rpc error: code = AlreadyExists desc = etcd data directory is not empty")
    assert inspect.bootstrap_already_done("Etcd Is ALREADY BOOTSTRAPPED, refusing")
    assert not inspect.bootstrap_already_done("connection refused")
    assert not inspect.bootstrap_already_done("")


ETCD_MEMBERS = """NODE        ID                 HOSTNAME   PEER URLS                  CLIENT URLS                LEARNER
10.0.0.10   43a0a9a8b4fc2e3d   cp-01      https://10.0.0.10:2380     https://10.0.0.10:2379     false
10.0.0.10   8e1f0c2d3b4a5968   cp-02      https://10.0.0.13:2380     https://10.0.0.13:2379     false
"""


def test_etcd_member_count():
    assert inspect.etcd_member_count(ETCD_MEMBERS) == 2
    assert inspect.etcd_member_count(ETCD_MEMBERS.splitlines()[0] + "\n") == 0
    assert inspect.etcd_member_count("\n" + ETCD_MEMBERS + "\n\n") == 2
    assert inspect.etcd_member_count("") is None
    assert inspect.etcd_member_count("   \n") is None
    assert inspect.etcd_member_count("error: connection refused") is None
    assert inspect.etcd_member_count("\x00garbage\nmore") is None


def test_config_drift():
    assert inspect.config_drift("Dry run summary:\nNo changes.\n") == "in sync"
    assert inspect.config_drift("Dry run summary:\nConfig diff:\n\n--- a\n+++ b\n+x\n") == "changes"
    assert inspect.config_drift("something else") == "unknown"


def test_kubelet_versions():
    assert inspect.kubelet_versions(NODES) == {"cp-01": "1.37.0", "w-01": "1.36.2"}
    assert inspect.kubelet_versions("oops") == {}


def test_application_summary():
    assert inspect.application_summary(APPS) == {"Synced/Healthy": 2, "OutOfSync/Unknown": 1}
    assert inspect.application_summary('{"items": []}') == {}
    assert inspect.application_summary("error: the server doesn't have a resource type") is None


def test_kubelet_versions_defensive():
    # Non-dict metadata should be skipped, not raise AttributeError
    assert inspect.kubelet_versions('{"items": [{"metadata": "weird"}]}') == {}
    # Non-dict status should be skipped, not raise AttributeError
    assert inspect.kubelet_versions('{"items": [{"metadata": {"name": "n1"}, "status": "weird"}]}') == {}


ADDRESSES = """NODE              NAMESPACE   TYPE            ID                                          VERSION   ADDRESS                         LINK
167.233.212.255   network     AddressStatus   cni0/10.244.0.1/24                          1         10.244.0.1/24                   cni0
167.233.212.255   network     AddressStatus   eth0/167.233.212.255/32                     1         167.233.212.255/32              eth0
167.233.212.255   network     AddressStatus   external/167.233.212.255/32                 1         167.233.212.255/32              external
167.233.212.255   network     AddressStatus   lo/127.0.0.1/8                              1         127.0.0.1/8                     lo
167.233.212.255   network     AddressStatus   tailscale0/100.100.148.20/32                1         100.100.148.20/32               tailscale0
"""


def test_tailnet_ipv4():
    assert inspect.tailnet_ipv4(ADDRESSES) == "100.100.148.20"
    assert inspect.tailnet_ipv4("NODE\n167.233.212.255   x\n") is None  # no ADDRESS column
    assert inspect.tailnet_ipv4("error: connection refused") is None
    assert inspect.tailnet_ipv4("") is None


def test_application_summary_defensive():
    # Non-dict status should count as Unknown/Unknown, not raise AttributeError
    assert inspect.application_summary('{"items": [{"status": "weird"}]}') == {"Unknown/Unknown": 1}
    # Non-dict sync should count as Unknown/Unknown
    assert inspect.application_summary('{"items": [{"status": {"sync": "weird"}}]}') == {"Unknown/Unknown": 1}
    # Non-dict health should count as Unknown/Unknown
    assert inspect.application_summary('{"items": [{"status": {"health": 5}}]}') == {"Unknown/Unknown": 1}


def test_redact_diff_hides_key_and_token_values():
    text = (
        "         key: LS0tLS1CRUdJTiBFRDI1NTE5IFBSSVZBVEUgS0VZLS0tLS0K\n"
        "+    - TS_AUTHKEY=tskey-auth-x\n"
        "     token: abc.def\n"
        "+certExtraSANs:\n"
        "crt: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCg==\n"
    )
    result = inspect.redact_diff(text)
    assert "LS0tLS1CRUdJTiBFRDI1NTE5IFBSSVZBVEUgS0VZLS0tLS0K" not in result
    assert "tskey-auth-x" not in result
    assert "abc.def" not in result
    assert "         key: <redacted>" in result
    assert "+    - TS_AUTHKEY=<redacted>" in result
    assert "     token: <redacted>" in result
    assert "+certExtraSANs:" in result  # no value: untouched
    assert "crt: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCg==" in result  # certificates are public


def test_redact_diff_matches_secret_boxencryption_and_bootstrap_keys():
    text = (
        "    secretboxEncryptionSecret: c2VjcmV0\n"
        "    aescbcEncryptionSecret: c2VjcmV0\n"
        "    bootstrapToken: dG9rZW4=\n"
        "+   - TS_AUTHKEY=abc\n"
        "+   - SOME_PASSWORD=hunter2\n"
        "+   - UNRELATED=fine\n"
    )
    result = inspect.redact_diff(text)
    assert "c2VjcmV0" not in result
    assert "dG9rZW4=" not in result
    assert "TS_AUTHKEY=abc" not in result and "TS_AUTHKEY=<redacted>" in result
    assert "SOME_PASSWORD=hunter2" not in result and "SOME_PASSWORD=<redacted>" in result
    assert "UNRELATED=fine" in result


def test_redact_diff_empty_and_no_secrets():
    assert inspect.redact_diff("") == ""
    assert inspect.redact_diff("Dry run summary:\nNo changes.\n") == "Dry run summary:\nNo changes.\n"
