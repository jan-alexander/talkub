import json
import sys

import pytest
from helpers import need

from talkub import coredns
from talkub.config import GitSpec
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.errors import CommandFailed, ValidationError
from talkub.core.fsutil import atomic_write
from talkub.talos import inspect, lifecycle

pytestmark = need("age", "age-keygen", "sops")

GIT_URL = "git@github.com:me/cluster.git"
DELETES = [
    [
        "-n",
        "kube-system",
        "delete",
        "deploy",
        "coredns",
        "--ignore-not-found",
        "--cascade=foreground",
    ],
    ["-n", "kube-system", "delete", "configmap", "coredns", "--ignore-not-found"],
    ["-n", "kube-system", "delete", "serviceaccount", "coredns", "--ignore-not-found"],
    ["delete", "clusterrolebinding", "system:coredns", "--ignore-not-found"],
    ["delete", "clusterrole", "system:coredns", "--ignore-not-found"],
]


PORTS = [
    {"name": "udp-53", "port": 53, "protocol": "UDP", "targetPort": 53},
    {"name": "tcp-53", "port": 53, "protocol": "TCP", "targetPort": 53},
]
RENDERED_SERVICE = """---
# Source: coredns/templates/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: kube-dns
  namespace: kube-system
spec:
  selector:
    k8s-app: kube-dns
  clusterIP: 10.96.0.10
  ports:
  - {"name":"udp-53","port":53,"protocol":"UDP","targetPort":53}
  - {"name":"tcp-53","port":53,"protocol":"TCP","targetPort":53}
  type: ClusterIP
"""
NOT_FOUND = 'Error from server (NotFound): services "kube-dns" not found\n'


def live_service(ports=PORTS, cluster_ip="10.96.0.10") -> str:
    return json.dumps(
        {"kind": "Service", "spec": {"clusterIP": cluster_ip, "ports": ports}}
    )


def endpoints(*names: str, ready: bool = True) -> str:
    endpoints = [
        {
            "addresses": [f"10.244.0.{i}"],
            "conditions": {"ready": ready},
            "targetRef": {"kind": "Pod", "name": n},
        }
        for i, n in enumerate(names)
    ]
    return json.dumps(
        {"kind": "List", "items": [{"kind": "EndpointSlice", "endpoints": endpoints}]}
    )


def first_rule(spy, *words, stdout="", stderr="", rc=0):
    spy.rules.insert(
        0, {"match": list(words), "stdout": stdout, "stderr": stderr, "rc": rc}
    )
    spy._save()


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
    kubectl.on(
        "endpointslices",
        stdout=endpoints("coredns-talkub-abc-1", "coredns-talkub-abc-2"),
    )
    kubectl.on("get", "svc", "json", stdout=live_service())
    kubectl.on("template", "--show-only", stdout=RENDERED_SERVICE)  # helm
    kubectl.on(
        "show", "chart", stdout="name: coredns\nversion: 1.45.0\n"
    )  # helm shares the rules
    script = kubectl.directory / "spy.py"
    fake_bin(
        "helm",
        f'exec "{sys.executable}" "{script}" "{kubectl.log}" "{kubectl.rules_file}" "$@"\n',
    )
    applied = []

    def fake_apply(ctx, names, *, mode="auto", confirm, skip_precheck=False):
        applied.append(
            {"names": list(names), "mode": mode, "skip_precheck": skip_precheck}
        )
        if not confirm("apply this configuration to cp-01?"):
            return []
        kubectl.log.open("a").write(json.dumps({"args": ["<talos apply>"]}) + "\n")
        return ["cp-01"]

    monkeypatch.setattr(lifecycle, "apply", fake_apply)
    set_drift(monkeypatch, [])
    monkeypatch.setattr(coredns, "ENDPOINTS_INTERVAL", 0)
    return repo, kubectl, applied


def set_drift(monkeypatch, nodes):
    monkeypatch.setattr(lifecycle, "drifting_nodes", lambda ctx: list(nodes))


APPLY = {"names": [], "mode": "no-reboot", "skip_precheck": False}
NODE_QUESTION = "apply this configuration to cp-01?"


def run(repo, *, resume=False, answer=True, answers=None):
    questions = []
    replies = list(answers) if answers is not None else None

    def confirm(question):
        questions.append(question)
        return replies.pop(0) if replies is not None else answer

    notes = coredns.takeover(Context.open(repo), resume=resume, confirm=confirm)
    return notes, questions


def mutations(argvs):
    return [a for a in argvs if a[0] != "show" and "get" not in a]


def test_happy_path_order_files_and_notes(cluster):
    repo, kubectl, applied = cluster
    notes, questions = run(repo)
    assert questions == [
        "Move CoreDNS from Talos to Argo CD now? DNS keeps working during the switch.",
        NODE_QUESTION,
    ]
    assert applied == [APPLY]
    argvs = kubectl.argvs
    assert argvs[0] == [
        "-n",
        "kube-system",
        "get",
        "svc",
        "kube-dns",
        "-o",
        "jsonpath={.spec.clusterIP}",
    ]
    assert argvs[1] == [
        "show",
        "chart",
        "coredns",
        "--repo",
        "https://coredns.github.io/helm",
    ]
    assert argvs[2] == ["<talos apply>"]
    assert argvs[3] == [
        "-n",
        "kube-system",
        "get",
        "deploy",
        "coredns",
        "-o",
        "jsonpath={.status.readyReplicas}",
    ]
    assert argvs[4] == [
        "-n",
        "kube-system",
        "annotate",
        "svc",
        "kube-dns",
        "--overwrite",
        "meta.helm.sh/release-name=coredns",
        "meta.helm.sh/release-namespace=kube-system",
    ]
    assert argvs[5] == [
        "-n",
        "kube-system",
        "label",
        "svc",
        "kube-dns",
        "--overwrite",
        "app.kubernetes.io/managed-by=Helm",
    ]
    template = argvs[6]
    assert template[:4] == ["template", "coredns", "coredns", "--repo"]
    assert template[template.index("--version") + 1] == "1.45.0"
    assert template[template.index("--namespace") + 1] == "kube-system"
    assert template[template.index("--values") + 1] == str(
        repo.gitops_dir / "values" / "coredns.yaml"
    )
    assert template[-2:] == ["--show-only", "templates/service.yaml"]
    assert argvs[7][:7] == [
        "-n",
        "kube-system",
        "patch",
        "svc",
        "kube-dns",
        "--type=json",
        "-p",
    ]
    assert json.loads(argvs[7][7]) == [
        {"op": "replace", "path": "/spec/ports", "value": PORTS}
    ]
    assert len(argvs[7]) == 8
    assert argvs[8] == [
        "-n",
        "kube-system",
        "patch",
        "svc",
        "kube-dns",
        "--type=merge",
        "-p",
        '{"metadata": {"managedFields": [{}]}}',
    ]
    helm = argvs[9]
    assert helm[:5] == ["upgrade", "--install", "coredns", "coredns", "--repo"]
    assert helm[helm.index("--version") + 1] == "1.45.0"
    assert helm[helm.index("-n") + 1] == "kube-system"
    assert helm[helm.index("-f") + 1] == str(
        repo.gitops_dir / "values" / "coredns.yaml"
    )
    assert "--create-namespace" not in helm
    assert argvs[10] == [
        "-n",
        "kube-system",
        "rollout",
        "status",
        "deploy/coredns-talkub",
        "--timeout",
        "5m",
    ]
    assert argvs[11] == ["-n", "kube-system", "get", "svc", "kube-dns", "-o", "json"]
    assert argvs[12:17] == DELETES
    assert argvs[17] == [
        "-n",
        "kube-system",
        "get",
        "endpointslices",
        "-l",
        "kubernetes.io/service-name=kube-dns",
        "-o",
        "json",
    ]
    assert len(argvs) == 18
    assert all(
        call["kubeconfig"]
        for call in kubectl.calls
        if call["args"][0] not in ("<talos apply>", "show")
    )

    assert "charts" not in repo.settings_file.read_text()
    assert (
        repo.talos_dir / "patches" / "controlplane-coredns.yaml"
    ).read_text() == coredns.talos_patch()
    assert (repo.gitops_dir / "values" / "coredns.yaml").read_text() == coredns.values()
    app = yamlio.load((repo.gitops_dir / "apps" / "coredns.yaml").read_text())
    assert app["spec"]["destination"]["namespace"] == "kube-system"
    assert app["spec"]["sources"][1]["repoURL"] == GIT_URL
    assert app["spec"]["sources"][0]["helm"]["valueFiles"] == [
        "$values/gitops/values/coredns.yaml"
    ]
    assert notes == [
        "CoreDNS now runs as coredns-talkub (Helm release coredns)",
        "commit and push gitops/ and talos/ — Argo CD manages CoreDNS from now on",
    ]


def test_values_get_stub_zone_when_operator_app_exists(cluster):
    from talkub.config import TailscaleSpec

    repo, _, _ = cluster
    settings = repo.load_settings()
    settings.tailscale = TailscaleSpec(
        tailnet="tail1234.ts.net", node_cidr="10.0.0.0/24"
    )
    repo.write_settings(settings)
    atomic_write(
        repo.gitops_dir / "apps" / "tailscale-operator.yaml", b"kind: Application\n"
    )
    run(repo)
    assert (repo.gitops_dir / "values" / "coredns.yaml").read_text() == coredns.values(
        "tail1234.ts.net"
    )


def test_wrong_cluster_ip_aborts_without_changes(cluster):
    repo, kubectl, applied = cluster
    kubectl.rules.insert(
        0, {"match": ["jsonpath={.spec.clusterIP}"], "stdout": "10.43.0.10", "rc": 0}
    )
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
    app_text = (
        "# edited by hand\n" + (repo.gitops_dir / "apps" / "coredns.yaml").read_text()
    )
    app_text = app_text.replace("targetRevision: 1.45.0", "targetRevision: 1.44.0")
    atomic_write(repo.gitops_dir / "apps" / "coredns.yaml", app_text.encode())
    kubectl.log.unlink()
    run(repo, resume=True)
    argvs = kubectl.argvs
    assert not any(a[0] == "show" for a in argvs)  # no chart resolution
    assert [
        "<talos apply>"
    ] not in argvs  # every node is in sync: the Talos step is skipped
    assert argvs[7][:3] == ["upgrade", "--install", "coredns"]
    assert (
        argvs[7][argvs[7].index("--version") + 1] == "1.44.0"
    )  # the app file is the source
    assert argvs[10:15] == DELETES
    assert (repo.gitops_dir / "apps" / "coredns.yaml").read_text() == app_text
    assert applied == [APPLY]


def test_resume_rejects_mismatched_namespace(cluster):
    repo, kubectl, applied = cluster
    run(repo)
    path = repo.gitops_dir / "apps" / "coredns.yaml"
    doc = yamlio.load(path.read_text())
    doc["spec"]["destination"]["namespace"] = "other-ns"
    atomic_write(path, yamlio.dump(doc).encode())
    kubectl.log.unlink()
    with pytest.raises(ValidationError) as exc:
        run(repo, resume=True)
    assert "other-ns" in str(exc.value)
    assert mutations(kubectl.argvs) == []
    assert applied == [
        APPLY
    ]  # only from the first (successful) run; resume aborted before Talos


def test_resume_rejects_mismatched_release_name(cluster):
    repo, kubectl, applied = cluster
    run(repo)
    path = repo.gitops_dir / "apps" / "coredns.yaml"
    doc = yamlio.load(path.read_text())
    doc["spec"]["sources"][0].setdefault("helm", {})["releaseName"] = "other-release"
    atomic_write(path, yamlio.dump(doc).encode())
    kubectl.log.unlink()
    with pytest.raises(ValidationError) as exc:
        run(repo, resume=True)
    assert "other-release" in str(exc.value)
    assert mutations(kubectl.argvs) == []
    assert applied == [
        APPLY
    ]  # only from the first (successful) run; resume aborted before Talos


def test_drift_is_refused_before_any_change(cluster, monkeypatch):
    repo, kubectl, applied = cluster
    set_drift(monkeypatch, ["cp-01"])
    before = repo.settings_file.read_text()
    with pytest.raises(ValidationError) as exc:
        run(repo, answer=True)
    assert "cp-01" in str(exc.value)
    assert "talkub plan" in exc.value.hint
    assert repo.settings_file.read_text() == before
    assert not (repo.gitops_dir / "apps" / "coredns.yaml").exists()
    assert not (repo.talos_dir / "patches" / "controlplane-coredns.yaml").exists()
    assert mutations(kubectl.argvs) == [] and applied == []


def test_declined_node_apply_stops_before_the_cluster_changes(cluster):
    repo, kubectl, applied = cluster
    with pytest.raises(ValidationError) as exc:
        run(repo, answers=[True, False])
    assert "--resume" in exc.value.hint
    assert applied == [APPLY]
    assert mutations(kubectl.argvs) == []


def test_resume_after_failed_rollout_skips_the_health_gate(cluster, monkeypatch):
    repo, kubectl, applied = cluster
    first_rule(kubectl, "rollout", "status", rc=1)
    with pytest.raises(CommandFailed) as exc:
        run(repo)
    assert "before `talkub apply`" in exc.value.hint
    kubectl.rules.pop(0)
    kubectl._save()
    set_drift(monkeypatch, ["cp-01"])  # e.g. the Talos apply had not gone through
    run(repo, resume=True)
    assert applied == [APPLY, {"names": [], "mode": "no-reboot", "skip_precheck": True}]
    assert kubectl.argvs[-1][3] == "endpointslices"


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
    kubectl.rules.insert(
        0, {"match": ["jsonpath={.status.readyReplicas}"], "stdout": "", "rc": 1}
    )
    kubectl._save()
    run(repo)
    assert "warning:" in capsys.readouterr().err
    assert kubectl.argvs[-1][3] == "endpointslices"


def test_failed_rollout_deletes_nothing(cluster):
    repo, kubectl, _ = cluster
    kubectl.on("rollout", "status", rc=1)
    kubectl.rules.insert(0, kubectl.rules.pop())
    kubectl._save()
    with pytest.raises(CommandFailed) as exc:
        run(repo)
    assert str(exc.value).startswith("takeover step 'rollout status' failed")
    assert "--resume" in exc.value.hint
    assert not any("delete" in a for a in kubectl.argvs)


def test_endpoints_still_on_old_pods_fail(cluster):
    repo, kubectl, _ = cluster
    first_rule(
        kubectl,
        "endpointslices",
        stdout=endpoints("coredns-talkub-x-1", "coredns-7c-2"),
    )
    with pytest.raises(CommandFailed) as exc:
        run(repo)
    assert "coredns-7c-2" in str(exc.value)
    assert exc.value.hint
    polls = [a for a in kubectl.argvs if "endpointslices" in a]
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
    assert kubectl.argvs[-1][3] == "endpointslices"


def test_cli_prompt_declined(cluster, monkeypatch):
    import typer

    from talkub.cli.main import main

    repo, kubectl, applied = cluster
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)
    cli(main, repo)
    assert applied == [] and mutations(kubectl.argvs) == []


def test_service_mismatch_after_helm_deletes_nothing(cluster):
    repo, kubectl, _ = cluster
    first_rule(
        kubectl,
        "get",
        "svc",
        "json",
        stdout=live_service(
            [*PORTS, {"name": "metrics", "port": 9153, "protocol": "TCP"}]
        ),
    )
    with pytest.raises(CommandFailed) as exc:
        run(repo)
    assert str(exc.value).startswith("takeover step 'verify service' failed")
    assert not any("delete" in a for a in kubectl.argvs)


def test_service_with_wrong_cluster_ip_after_helm_fails(cluster):
    repo, kubectl, _ = cluster
    first_rule(
        kubectl, "get", "svc", "json", stdout=live_service(cluster_ip="10.96.0.99")
    )
    with pytest.raises(CommandFailed) as exc:
        run(repo)
    assert str(exc.value).startswith("takeover step 'verify service' failed")
    assert not any("delete" in a for a in kubectl.argvs)


def test_resume_with_missing_service_lets_helm_create_it(cluster):
    repo, kubectl, _ = cluster
    run(repo)
    kubectl.log.unlink()
    first_rule(kubectl, "jsonpath={.spec.clusterIP}", stderr=NOT_FOUND, rc=1)
    run(repo, resume=True)
    argvs = kubectl.argvs
    assert not any(verb in a for a in argvs for verb in ("annotate", "label", "patch"))
    assert argvs[2][0] == "template" and argvs[3][:2] == ["upgrade", "--install"]
    assert ["-n", "kube-system", "get", "svc", "kube-dns", "-o", "json"] in argvs
    assert argvs[-1][3] == "endpointslices"


def test_missing_service_without_resume_is_rejected(cluster):
    repo, kubectl, applied = cluster
    first_rule(kubectl, "jsonpath={.spec.clusterIP}", stderr=NOT_FOUND, rc=1)
    with pytest.raises(ValidationError):
        run(repo)
    assert mutations(kubectl.argvs) == [] and applied == []
    assert not (repo.gitops_dir / "apps" / "coredns.yaml").exists()


def test_service_spec():
    assert inspect.service_spec(RENDERED_SERVICE)["ports"] == PORTS
    assert inspect.service_spec(live_service())["clusterIP"] == "10.96.0.10"
    assert inspect.service_spec("kind: ConfigMap\n") == {}
    assert inspect.service_spec(": not yaml: [") == {}


def test_endpointslice_pod_names():
    assert inspect.endpointslice_pod_names(endpoints("a", "b")) == ["a", "b"]
    assert inspect.endpointslice_pod_names(endpoints("a", ready=False)) == []
    assert inspect.endpointslice_pod_names("{}") == []
    assert inspect.endpointslice_pod_names("not json") == []
