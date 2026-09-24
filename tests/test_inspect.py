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


def test_application_summary_defensive():
    # Non-dict status should count as Unknown/Unknown, not raise AttributeError
    assert inspect.application_summary('{"items": [{"status": "weird"}]}') == {"Unknown/Unknown": 1}
    # Non-dict sync should count as Unknown/Unknown
    assert inspect.application_summary('{"items": [{"status": {"sync": "weird"}}]}') == {"Unknown/Unknown": 1}
    # Non-dict health should count as Unknown/Unknown
    assert inspect.application_summary('{"items": [{"status": {"health": 5}}]}') == {"Unknown/Unknown": 1}
