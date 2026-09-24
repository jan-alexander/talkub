import pytest

from helpers import need
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.kube import kube_session
from talkub.runner import Runner


def test_spy_rules_first_match_and_logging(spy):
    fake = spy("kubectl").on("get", "nodes", stdout="nodes!", rc=3).on("get", stdout="other")
    proc = Runner().run(["kubectl", "get", "nodes"], check=False)
    assert (proc.returncode, proc.stdout) == (3, b"nodes!")
    assert Runner().run(["kubectl", "get", "pods"]).stdout == b"other"
    assert fake.argvs == [["get", "nodes"], ["get", "pods"]]


@need("age", "age-keygen", "sops")
def test_kube_session_exposes_kubeconfig_only_during_session(repo_with_configs, spy):
    kubectl = spy("kubectl").on("get", "nodes", stdout='{"items": []}')
    helm = spy("helm")
    with kube_session(Context.open(repo_with_configs)) as kube:
        assert kube.kubectl(["get", "nodes", "-o", "json"]).stdout == b'{"items": []}'
        kube.helm(["list"])
        path = kube.kubeconfig
        assert path.read_text() == "kube: marker\n"
        assert kube.env == {"KUBECONFIG": str(path)}
    assert not path.exists()
    assert kubectl.calls[0]["kubeconfig"] is True
    assert helm.calls[0]["kubeconfig"] is True


@need("age", "age-keygen", "sops")
def test_kube_session_without_kubeconfig(keyed_repo, spy):
    spy("kubectl")
    with pytest.raises(ValidationError) as exc:
        with kube_session(Context.open(keyed_repo)):
            pass
    assert "bootstrap" in exc.value.hint
