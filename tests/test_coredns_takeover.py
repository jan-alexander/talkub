import json
import sys

import pytest

from helpers import need
from talkub import coredns, lifecycle, yamlio
from talkub.context import Context
from talkub.errors import CommandFailed, ValidationError
from talkub.fsutil import atomic_write
from talkub.repo import GitSpec
from talkub.talos import inspect

pytestmark = need("age", "age-keygen", "sops")

GIT_URL = "git@github.com:me/cluster.git"
DELETES = [
    ["-n", "kube-system", "delete", "deploy", "coredns", "--ignore-not-found", "--cascade=foreground"],
    ["-n", "kube-system", "delete", "configmap", "coredns", "--ignore-not-found"],
    ["-n", "kube-system", "delete", "serviceaccount", "coredns", "--ignore-not-found"],
    ["delete", "clusterrolebinding", "system:coredns", "--ignore-not-found"],
    ["delete", "clusterrole", "system:coredns", "--ignore-not-found"],
]


def endpoints(*names: str) -> str:
    addresses = [{"ip": f"10.244.0.{i}", "targetRef": {"kind": "Pod", "name": n}} for i, n in enumerate(names)]
    return json.dumps({"kind": "Endpoints", "subsets": [{"addresses": addresses}]})


@pytest.fixture
def cluster(repo_with_configs, spy, fake_bin, monkeypatch):
    """Bootstrapped repo with a git URL; kubectl and helm log into one file (shared order)."""
    repo = repo_with_configs
    settings = repo.load_settings()
    settings.git = GitSpec(GIT_URL)
    repo.write_settings(settings)
    kubectl = spy("kubectl")
    kubectl.on("jsonpath={.spec.clusterIP}", stdout="10.96.0.10")
    kubectl.on("jsonpath={.status.readyReplicas}", stdout="2")
    kubectl.on("endpoints", stdout=endpoints("coredns-talkub-abc-1", "coredns-talkub-abc-2"))
    kubectl.on("show", "chart", stdout="name: coredns\nversion: 1.45.0\n")  # helm shares the rules
    script = kubectl.directory / "spy.py"
    fake_bin("helm", f'exec "{sys.executable}" "{script}" "{kubectl.log}" "{kubectl.rules_file}" "$@"\n')
    applied = []

    def fake_apply(ctx, names, *, mode="auto", confirm, skip_precheck=False):
        applied.append(list(names))
        kubectl.log.open("a").write(json.dumps({"args": ["<talos apply>"]}) + "\n")
        return ["cp-01"]

    monkeypatch.setattr(lifecycle, "apply", fake_apply)
    monkeypatch.setattr(coredns, "ENDPOINTS_INTERVAL", 0)
    return repo, kubectl, applied


def run(repo, *, resume=False, answer=True):
    questions = []

    def confirm(question):
        questions.append(question)
        return answer

    notes = coredns.takeover(Context.open(repo), resume=resume, confirm=confirm)
    return notes, questions


def mutations(argvs):
    return [a for a in argvs if a[0] != "show" and "get" not in a]


def test_happy_path_order_files_and_notes(cluster):
    repo, kubectl, applied = cluster
    notes, questions = run(repo)
    assert questions == ["Move CoreDNS from Talos to Argo CD now? DNS keeps working during the switch."]
    assert applied == [[]]
    argvs = kubectl.argvs
    assert argvs[0] == ["-n", "kube-system", "get", "svc", "kube-dns", "-o", "jsonpath={.spec.clusterIP}"]
    assert argvs[1] == ["show", "chart", "coredns", "--repo", "https://coredns.github.io/helm"]
    assert argvs[2] == ["<talos apply>"]
    assert argvs[3] == ["-n", "kube-system", "get", "deploy", "coredns", "-o", "jsonpath={.status.readyReplicas}"]
    assert argvs[4] == [
        "-n", "kube-system", "annotate", "svc", "kube-dns", "--overwrite",
        "meta.helm.sh/release-name=coredns", "meta.helm.sh/release-namespace=kube-system",
    ]
    assert argvs[5] == ["-n", "kube-system", "label", "svc", "kube-dns", "--overwrite", "app.kubernetes.io/managed-by=Helm"]
    helm = argvs[6]
    assert helm[:5] == ["upgrade", "--install", "coredns", "coredns", "--repo"]
    assert helm[helm.index("--version") + 1] == "1.45.0"
    assert helm[helm.index("--namespace") + 1] == "kube-system"
    assert helm[helm.index("--values") + 1] == str(repo.gitops_dir / "values" / "coredns.yaml")
    assert "--create-namespace" not in helm
    assert argvs[7] == ["-n", "kube-system", "rollout", "status", "deploy/coredns-talkub", "--timeout", "5m"]
    assert argvs[8:13] == DELETES
    assert argvs[13] == ["-n", "kube-system", "get", "endpoints", "kube-dns", "-o", "json"]
    assert len(argvs) == 14
    assert all(call["kubeconfig"] for call in kubectl.calls if call["args"][0] not in ("<talos apply>", "show"))

    settings = repo.load_settings()
    assert settings.charts["coredns"].version == "1.45.0"
    assert (repo.talos_dir / "patches" / "controlplane-coredns.yaml").read_text() == coredns.talos_patch()
    assert (repo.gitops_dir / "values" / "coredns.yaml").read_text() == coredns.values()
    app = yamlio.load((repo.gitops_dir / "apps" / "coredns.yaml").read_text())
    assert app["spec"]["destination"]["namespace"] == "kube-system"
    assert app["spec"]["sources"][1]["repoURL"] == GIT_URL
    assert app["spec"]["sources"][0]["helm"]["valueFiles"] == ["$values/gitops/values/coredns.yaml"]
    assert notes == [
        "CoreDNS now runs as coredns-talkub (Helm release coredns)",
        "commit and push gitops/ and talos/ — Argo CD manages CoreDNS from now on",
    ]


def test_values_get_stub_zone_when_operator_app_exists(cluster):
    from talkub.repo import TailscaleSpec

    repo, _, _ = cluster
    settings = repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="tail1234.ts.net", node_cidr="10.0.0.0/24")
    repo.write_settings(settings)
    atomic_write(repo.gitops_dir / "apps" / "tailscale-operator.yaml", b"kind: Application\n")
    run(repo)
    assert (repo.gitops_dir / "values" / "coredns.yaml").read_text() == coredns.values("tail1234.ts.net")


def test_wrong_cluster_ip_aborts_without_changes(cluster):
    repo, kubectl, applied = cluster
    kubectl.rules.insert(0, {"match": ["jsonpath={.spec.clusterIP}"], "stdout": "10.43.0.10", "rc": 0})
    kubectl._save()
    before = repo.settings_file.read_text()
    with pytest.raises(ValidationError) as exc:
        run(repo)
    assert "10.43.0.10" in str(exc.value)
    assert repo.settings_file.read_text() == before
    assert not (repo.gitops_dir / "apps" / "coredns.yaml").exists()
    assert not (repo.talos_dir / "patches" / "controlplane-coredns.yaml").exists()
    assert mutations(kubectl.argvs) == [] and applied == []


def test_missing_git_url_is_rejected(cluster):
    repo, kubectl, _ = cluster
    settings = repo.load_settings()
    settings.git = None
    repo.write_settings(settings)
    with pytest.raises(ValidationError) as exc:
        run(repo)
    assert exc.value.hint == "talkub repo set-url <url>"
    assert kubectl.argvs == []


def test_already_taken_over(cluster):
    repo, kubectl, applied = cluster
    atomic_write(repo.gitops_dir / "apps" / "coredns.yaml", b"kind: Application\n")
    with pytest.raises(ValidationError) as exc:
        run(repo)
    assert str(exc.value) == "CoreDNS is already managed by talkub"
    assert exc.value.hint == "--resume if the takeover was interrupted"
    assert mutations(kubectl.argvs) == [] and applied == []


def test_resume_without_files_is_rejected(cluster):
    repo, kubectl, applied = cluster
    with pytest.raises(ValidationError):
        run(repo, resume=True)
    assert mutations(kubectl.argvs) == [] and applied == []


def test_resume_skips_file_writing_and_runs_the_rest(cluster):
    repo, kubectl, applied = cluster
    run(repo)
    app_text = "# edited by hand\n" + (repo.gitops_dir / "apps" / "coredns.yaml").read_text()
    atomic_write(repo.gitops_dir / "apps" / "coredns.yaml", app_text.encode())
    kubectl.log.unlink()
    run(repo, resume=True)
    argvs = kubectl.argvs
    assert not any(a[0] == "show" for a in argvs)  # no chart resolution
    assert argvs[1] == ["<talos apply>"]
    assert argvs[5][:3] == ["upgrade", "--install", "coredns"]
    assert argvs[7:12] == DELETES
    assert (repo.gitops_dir / "apps" / "coredns.yaml").read_text() == app_text
    assert applied == [[], []]


def test_declined_confirm_changes_nothing(cluster):
    repo, kubectl, applied = cluster
    before = repo.settings_file.read_text()
    notes, _ = run(repo, answer=False)
    assert notes == []
    assert repo.settings_file.read_text() == before
    assert not (repo.gitops_dir / "apps" / "coredns.yaml").exists()
    assert mutations(kubectl.argvs) == [] and applied == []


def test_missing_old_deployment_only_warns(cluster, capsys):
    repo, kubectl, _ = cluster
    kubectl.rules.insert(0, {"match": ["jsonpath={.status.readyReplicas}"], "stdout": "", "rc": 1})
    kubectl._save()
    run(repo)
    assert "warning:" in capsys.readouterr().err
    assert kubectl.argvs[-1][3] == "endpoints"


def test_failed_rollout_deletes_nothing(cluster):
    repo, kubectl, _ = cluster
    kubectl.on("rollout", "status", rc=1)
    kubectl.rules.insert(0, kubectl.rules.pop())
    kubectl._save()
    with pytest.raises(CommandFailed) as exc:
        run(repo)
    assert "rollout" in str(exc.value) or "rollout" in (exc.value.hint or "")
    assert "--resume" in exc.value.hint
    assert not any("delete" in a for a in kubectl.argvs)


def test_endpoints_still_on_old_pods_fail(cluster):
    repo, kubectl, _ = cluster
    kubectl.rules.insert(0, {"match": ["endpoints"], "stdout": endpoints("coredns-talkub-x-1", "coredns-7c-2"), "rc": 0})
    kubectl._save()
    with pytest.raises(CommandFailed) as exc:
        run(repo)
    assert "coredns-7c-2" in str(exc.value)
    assert exc.value.hint
    polls = [a for a in kubectl.argvs if "endpoints" in a]
    assert len(polls) == coredns.ENDPOINTS_ATTEMPTS


def cli(main, repo, *args):
    try:
        main(["--repo", str(repo.root), "coredns", "takeover", *args])
    except SystemExit as exc:
        assert exc.code == 0


def test_cli_yes_skips_prompt(cluster, monkeypatch, capsys):
    import typer

    from talkub.cli.main import main

    repo, kubectl, _ = cluster

    def no_prompt(*args, **kwargs):
        raise AssertionError("prompted despite --yes")

    monkeypatch.setattr(typer, "confirm", no_prompt)
    cli(main, repo, "--yes")
    out = capsys.readouterr().out
    assert "note: CoreDNS now runs as coredns-talkub (Helm release coredns)" in out
    assert kubectl.argvs[-1][3] == "endpoints"


def test_cli_prompt_declined(cluster, monkeypatch):
    import typer

    from talkub.cli.main import main

    repo, kubectl, applied = cluster
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)
    cli(main, repo)
    assert applied == [] and mutations(kubectl.argvs) == []


def test_endpoint_pod_names():
    assert inspect.endpoint_pod_names(endpoints("a", "b")) == ["a", "b"]
    assert inspect.endpoint_pod_names('{"subsets": [{"notReadyAddresses": [{"targetRef": {"name": "x"}}]}]}') == []
    assert inspect.endpoint_pod_names("{}") == []
    assert inspect.endpoint_pod_names("not json") == []
