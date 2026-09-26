import json
import subprocess
from dataclasses import dataclass, field

import pytest

from helpers import FakeRunner, need
from talkub.cli.main import main
from talkub.context import Context
from talkub.lint import (
    DEFAULT_YAMLLINT,
    Finding,
    LintReport,
    _DOWNLOAD_ERROR_PREFIX,
    cache_dir,
    kubeconform_argv,
    lint_manifests,
    lint_yaml,
    manifest_files,
    parse_kubeconform,
    run,
    yaml_files,
)
from talkub.runner import Runner


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_report_counts_and_render():
    report = LintReport(findings=[Finding("error", "a.yaml", "bad", 3), Finding("warning", "b.yaml", "meh")])
    assert report.errors == 1
    assert report.warnings == 1
    assert report.render() == [
        "a.yaml:3: error: bad",
        "b.yaml: warning: meh",
        "lint: 1 errors, 1 warnings, 0 skipped",
    ]


def test_empty_report_renders_only_summary():
    assert LintReport().render() == ["lint: 0 errors, 0 warnings, 0 skipped"]


def test_default_yamllint_matches_constraints():
    assert DEFAULT_YAMLLINT == (
        "# Managed by talkub. yamllint configuration for this cluster repo.\n"
        "extends: relaxed\n"
        "ignore: |\n"
        "  *.sops.yaml\n"
        "rules:\n"
        "  line-length: disable\n"
    )


def test_yaml_files_excludes_git_dir(empty_repo):
    (empty_repo.root / "a.yaml").write_text("a: 1\n")
    (empty_repo.root / "sub").mkdir()
    (empty_repo.root / "sub" / "b.yml").write_text("b: 1\n")
    (empty_repo.root / ".git").mkdir()
    (empty_repo.root / ".git" / "c.yaml").write_text("c: 1\n")
    files = [empty_repo.rel(p) for p in yaml_files(empty_repo)]
    assert files == [".talkub.yaml", "a.yaml", "sub/b.yml"]


def test_syntax_error_is_an_error_with_a_line(empty_repo):
    (empty_repo.root / "broken.yaml").write_text("a: [1, 2\n")
    report = LintReport()
    lint_yaml(empty_repo, report)
    findings = [f for f in report.findings if f.path == "broken.yaml"]
    assert findings and findings[0].level == "error"
    assert findings[0].line is not None


def test_error_and_warning_findings_have_line_numbers(empty_repo):
    (empty_repo.root / "mixed.yaml").write_text("a: [1 , 2]\nb: 1   \n")
    report = LintReport()
    lint_yaml(empty_repo, report)
    findings = {f.line: f for f in report.findings if f.path == "mixed.yaml"}
    assert findings[1].level == "warning" and "commas" in findings[1].message
    assert findings[2].level == "error" and "trailing-spaces" in findings[2].message


def test_sops_yaml_ignored_by_default(empty_repo):
    (empty_repo.root / "secret.sops.yaml").write_text("a: [1, 2\n")  # would be a syntax error otherwise
    report = LintReport()
    lint_yaml(empty_repo, report)
    assert not any(f.path == "secret.sops.yaml" for f in report.findings)


def test_repo_yamllint_overrides_default(empty_repo):
    (empty_repo.root / ".yamllint").write_text(
        "extends: relaxed\nrules:\n  line-length:\n    level: error\n    max: 20\n"
    )
    (empty_repo.root / "long.yaml").write_text("a: " + " ".join(["word"] * 10) + "\n")
    report = LintReport()
    lint_yaml(empty_repo, report)
    findings = [f for f in report.findings if f.path == "long.yaml"]
    assert findings and findings[0].level == "error" and "line-length" in findings[0].message


def test_run_reports_yaml_findings(empty_repo):
    (empty_repo.root / "broken.yaml").write_text("a: [1, 2\n")
    report = run(Context.open(empty_repo), offline=True)
    assert report.errors == 1


def test_cli_lint_ok_needs_no_master_key(empty_repo, monkeypatch, capfd):
    monkeypatch.chdir(empty_repo.root)
    assert _run(["lint"]) == 0
    assert "lint: 0 errors, 0 warnings, 0 skipped" in capfd.readouterr().out


def test_cli_lint_exit_1_on_error(empty_repo, monkeypatch, capfd):
    (empty_repo.root / "broken.yaml").write_text("a: [1, 2\n")
    monkeypatch.chdir(empty_repo.root)
    assert _run(["lint"]) == 1
    assert "lint: 1 errors, 0 warnings, 0 skipped" in capfd.readouterr().out


@need("age", "age-keygen", "sops", "talosctl", "helm")
def test_fresh_init_repo_has_no_yamllint_errors(tmp_path, software_identity, fake_bin):
    from talkub.recipients import Recipient
    from talkub.repo import NodeSpec
    from talkub.runner import Runner
    from talkub.scaffold import InitRequest, init_cluster

    fake_bin("helm", "printf 'apiVersion: v2\\nname: x\\nversion: 1.2.3\\n'\n")
    request = InitRequest(
        target=tmp_path / "demo",
        name="demo",
        endpoint="https://10.0.0.10:6443",
        git_url="https://git.example.com/demo.git",
        nodes={"cp-01": NodeSpec(ip="10.0.0.10")},
        recipients=[Recipient("me", software_identity[1])],
    )
    repo = init_cluster(request, Runner())
    report = LintReport()
    lint_yaml(repo, report)
    assert report.errors == 0, report.render()


# --- kubeconform (Task 2) ---


@dataclass
class ScriptedRunner(Runner):
    """Returns one scripted stdout per call, in order (unlike FakeRunner's static per-binary output)."""

    outputs: list[bytes] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, self.outputs.pop(0), b"")


def test_cache_dir_uses_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdgcache"))
    assert cache_dir() == tmp_path / "xdgcache" / "talkub" / "kubeconform"


def test_cache_dir_falls_back_to_home_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cache_dir() == tmp_path / ".cache" / "talkub" / "kubeconform"


def test_manifest_files_selects_apps_and_manifests_excludes_values_and_sops(empty_repo):
    gitops = empty_repo.root / "gitops"
    (gitops / "apps" / "sub").mkdir(parents=True)
    (gitops / "apps" / "a.yaml").write_text("x: 1\n")
    (gitops / "apps" / "sub" / "b.yml").write_text("x: 1\n")
    (gitops / "apps" / "secret.sops.yaml").write_text("x: 1\n")
    (gitops / "manifests").mkdir()
    (gitops / "manifests" / "c.yaml").write_text("x: 1\n")
    (gitops / "values").mkdir()
    (gitops / "values" / "d.yaml").write_text("x: 1\n")
    files = [empty_repo.rel(p) for p in manifest_files(empty_repo)]
    assert files == ["gitops/apps/a.yaml", "gitops/apps/sub/b.yml", "gitops/manifests/c.yaml"]


def test_manifest_files_empty_when_no_gitops_dirs(empty_repo):
    assert manifest_files(empty_repo) == []


def test_kubeconform_argv_contents(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cache = tmp_path / "cache" / "talkub" / "kubeconform"
    argv = kubeconform_argv("1.30.0")
    assert argv == [
        "kubeconform",
        "-output", "json",
        "-summary",
        "-verbose",
        "-strict",
        "-ignore-missing-schemas",
        "-kubernetes-version", "1.30.0",
        "-cache", str(cache),
        "-schema-location", "default",
        "-schema-location",
        "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
        "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
    ]


def _kubeconform_json(resources):
    return json.dumps({"resources": resources, "summary": {}})


def test_parse_kubeconform_findings_and_skipped():
    stdout = _kubeconform_json([
        {
            "filename": "gitops/apps/bad.yaml", "kind": "Namespace", "name": "test",
            "version": "v1", "status": "statusInvalid",
            "msg": "problem validating schema. Check JSON formatting: ... - at '/metadata': additional properties 'bogus' not allowed",
            "validationErrors": [{"path": "/metadata", "msg": "additional properties 'bogus' not allowed"}],
        },
        {
            "filename": "gitops/apps/unknown.yaml", "kind": "Widget", "name": "foo",
            "version": "example.com/v1", "status": "statusSkipped", "msg": "",
        },
        {
            "filename": "gitops/apps/good.yaml", "kind": "Deployment", "name": "nginx",
            "version": "apps/v1", "status": "statusValid", "msg": "",
        },
    ])
    findings, skipped = parse_kubeconform(stdout)
    assert findings == [
        Finding("error", "gitops/apps/bad.yaml", "Namespace/test: /metadata: additional properties 'bogus' not allowed")
    ]
    assert skipped == ["no schema for Widget/foo (example.com/v1)"]


def test_parse_kubeconform_error_without_validation_errors_uses_msg():
    stdout = _kubeconform_json([
        {
            "filename": "gitops/apps/x.yaml", "kind": "Pod", "name": "x",
            "version": "v1", "status": "statusError", "msg": "boom",
        },
    ])
    findings, skipped = parse_kubeconform(stdout)
    assert findings == [Finding("error", "gitops/apps/x.yaml", "Pod/x: boom")]
    assert skipped == []


def test_parse_kubeconform_offline_download_error_is_skipped():
    stdout = _kubeconform_json([
        {
            "filename": "gitops/apps/x.yaml", "kind": "Namespace", "name": "test", "version": "v1",
            "status": "statusError",
            "msg": "failed downloading schema at https://nonexistent.invalid/namespace.json: dial tcp: lookup nonexistent.invalid: no such host",
        },
    ])
    findings, skipped = parse_kubeconform(stdout, offline=True)
    assert findings == []
    assert skipped == ["schema not cached for Namespace/test (offline)"]


def test_parse_kubeconform_online_download_error_stays_an_error():
    stdout = _kubeconform_json([
        {
            "filename": "gitops/apps/x.yaml", "kind": "Namespace", "name": "test", "version": "v1",
            "status": "statusError", "msg": "failed downloading schema at https://x/namespace.json: boom",
        },
    ])
    findings, skipped = parse_kubeconform(stdout, offline=False)
    assert skipped == []
    assert findings == [Finding("error", "gitops/apps/x.yaml", "Namespace/test: failed downloading schema at https://x/namespace.json: boom")]


def test_parse_kubeconform_offline_non_download_error_stays_an_error():
    stdout = _kubeconform_json([
        {
            "filename": "gitops/apps/x.yaml", "kind": "Namespace", "name": "test", "version": "v1",
            "status": "statusError", "msg": "error unmarshalling resource: bad yaml",
        },
    ])
    findings, skipped = parse_kubeconform(stdout, offline=True)
    assert skipped == []
    assert findings == [Finding("error", "gitops/apps/x.yaml", "Namespace/test: error unmarshalling resource: bad yaml")]


def test_parse_kubeconform_non_json_output_is_one_error_finding():
    findings, skipped = parse_kubeconform("not json at all\nsecond line")
    assert skipped == []
    assert findings == [Finding("error", "kubeconform", "kubeconform produced unexpected output: not json at all")]


def test_parse_kubeconform_prefix_prepended_to_path():
    stdout = _kubeconform_json([
        {
            "filename": "-", "kind": "Pod", "name": "x", "version": "v1", "status": "statusInvalid",
            "msg": "boom", "validationErrors": [{"path": "/spec", "msg": "boom"}],
        },
    ])
    findings, _ = parse_kubeconform(stdout, prefix="coredns: ")
    assert findings[0].path == "coredns: -"


def _manifest(empty_repo, name="a.yaml"):
    apps = empty_repo.root / "gitops" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / name).write_text("x: 1\n")


def test_lint_manifests_missing_tool_reports_skipped(empty_repo, monkeypatch, tmp_path):
    _manifest(empty_repo)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    report = LintReport()
    lint_manifests(Context.open(empty_repo), report, offline=False)
    assert report.skipped == ["kubeconform not found (brew install kubeconform)"]
    assert report.findings == []


def test_lint_manifests_no_manifests_is_silent(empty_repo, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))  # would skip loudly if it got this far
    report = LintReport()
    lint_manifests(Context.open(empty_repo), report, offline=False)
    assert report.findings == [] and report.skipped == []


def test_lint_manifests_offline_empty_cache_skips_without_calling(empty_repo, fake_bin, monkeypatch, tmp_path):
    _manifest(empty_repo)
    fake_bin("kubeconform", "exit 1\n")  # never actually invoked
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))  # nonexistent -> empty
    runner = FakeRunner()
    report = LintReport()
    lint_manifests(Context.open(empty_repo, runner=runner), report, offline=True)
    assert report.skipped == ["kubeconform: no schema cache yet (run `talkub lint` online once)"]
    assert report.findings == []
    assert runner.calls == []


def test_lint_manifests_offline_populated_cache_runs_normally(empty_repo, fake_bin, monkeypatch, tmp_path):
    _manifest(empty_repo)
    fake_bin("kubeconform", "true\n")
    cache = tmp_path / "cache"
    (cache / "talkub" / "kubeconform").mkdir(parents=True)
    (cache / "talkub" / "kubeconform" / "marker").write_text("x")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    runner = FakeRunner(outputs={"kubeconform": _kubeconform_json([]).encode()})
    report = LintReport()
    lint_manifests(Context.open(empty_repo, runner=runner), report, offline=True)
    assert len(runner.calls) == 1
    assert report.findings == [] and report.skipped == []


def test_lint_manifests_calls_kubeconform_and_records_findings(empty_repo, fake_bin, monkeypatch, tmp_path):
    _manifest(empty_repo, "a.yaml")
    fake_bin("kubeconform", "true\n")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    runner = FakeRunner(outputs={
        "kubeconform": _kubeconform_json([
            {
                "filename": "gitops/apps/a.yaml", "kind": "ConfigMap", "name": "x",
                "version": "v1", "status": "statusInvalid", "msg": "bad",
                "validationErrors": [{"path": "/data", "msg": "bad"}],
            },
        ]).encode(),
    })
    report = LintReport()
    lint_manifests(Context.open(empty_repo, runner=runner), report, offline=False)
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.argv[0] == "kubeconform"
    assert "gitops/apps/a.yaml" in call.argv
    assert report.findings == [Finding("error", "gitops/apps/a.yaml", "ConfigMap/x: /data: bad")]
    assert report.skipped == []


def test_lint_manifests_version_fallback_reruns_with_master(empty_repo, fake_bin, monkeypatch, tmp_path):
    _manifest(empty_repo, "a.yaml")
    fake_bin("kubeconform", "true\n")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    first = _kubeconform_json([
        {"filename": "gitops/apps/a.yaml", "kind": "ConfigMap", "name": "x", "version": "v1", "status": "statusSkipped", "msg": ""},
    ]).encode()
    second = _kubeconform_json([
        {"filename": "gitops/apps/a.yaml", "kind": "ConfigMap", "name": "x", "version": "v1", "status": "statusValid", "msg": ""},
    ]).encode()
    runner = ScriptedRunner(outputs=[first, second])
    report = LintReport()
    lint_manifests(Context.open(empty_repo, runner=runner), report, offline=False)
    assert len(runner.calls) == 2
    assert "1.37.0" in runner.calls[0]  # sample_settings' kubernetesVersion
    assert "master" in runner.calls[1]
    assert report.findings == [
        Finding(
            "warning", "kubeconform",
            "kubernetes schemas for 1.37.0 not available; validated against master",
        )
    ]
    assert report.skipped == []


def test_lint_manifests_version_fallback_preserves_invalid_crd_finding(empty_repo, fake_bin, monkeypatch, tmp_path):
    _manifest(empty_repo, "a.yaml")
    (empty_repo.root / "gitops" / "apps" / "b.yaml").write_text("x: 1\n")
    fake_bin("kubeconform", "true\n")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    first = _kubeconform_json([
        {"filename": "gitops/apps/a.yaml", "kind": "ConfigMap", "name": "x", "version": "v1", "status": "statusSkipped", "msg": ""},
        {
            "filename": "gitops/apps/b.yaml", "kind": "DNSConfig", "name": "y", "version": "tailscale.com/v1alpha1",
            "status": "statusInvalid", "msg": "bad", "validationErrors": [{"path": "/spec", "msg": "missing nameserver"}],
        },
    ]).encode()
    # The master rerun's crafted output omits the CRD entirely (it should still be reported).
    second = _kubeconform_json([
        {"filename": "gitops/apps/a.yaml", "kind": "ConfigMap", "name": "x", "version": "v1", "status": "statusValid", "msg": ""},
    ]).encode()
    runner = ScriptedRunner(outputs=[first, second])
    report = LintReport()
    lint_manifests(Context.open(empty_repo, runner=runner), report, offline=False)
    assert len(runner.calls) == 2
    crd_findings = [f for f in report.findings if f.path == "gitops/apps/b.yaml"]
    assert crd_findings == [Finding("error", "gitops/apps/b.yaml", "DNSConfig/y: /spec: missing nameserver")]


def test_lint_manifests_version_fallback_dedupes_resource_present_in_both_runs(empty_repo, fake_bin, monkeypatch, tmp_path):
    _manifest(empty_repo, "a.yaml")
    (empty_repo.root / "gitops" / "apps" / "b.yaml").write_text("x: 1\n")
    fake_bin("kubeconform", "true\n")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    crd = {
        "filename": "gitops/apps/b.yaml", "kind": "DNSConfig", "name": "y", "version": "tailscale.com/v1alpha1",
        "status": "statusInvalid", "msg": "bad", "validationErrors": [{"path": "/spec", "msg": "missing nameserver"}],
    }
    first = _kubeconform_json([
        {"filename": "gitops/apps/a.yaml", "kind": "ConfigMap", "name": "x", "version": "v1", "status": "statusSkipped", "msg": ""},
        crd,
    ]).encode()
    second = _kubeconform_json([
        {"filename": "gitops/apps/a.yaml", "kind": "ConfigMap", "name": "x", "version": "v1", "status": "statusValid", "msg": ""},
        crd,  # also reported by the master run: must not be duplicated
    ]).encode()
    runner = ScriptedRunner(outputs=[first, second])
    report = LintReport()
    lint_manifests(Context.open(empty_repo, runner=runner), report, offline=False)
    crd_findings = [f for f in report.findings if f.path == "gitops/apps/b.yaml"]
    assert len(crd_findings) == 1


def test_lint_manifests_crd_group_skip_does_not_trigger_fallback(empty_repo, fake_bin, monkeypatch, tmp_path):
    _manifest(empty_repo, "a.yaml")
    fake_bin("kubeconform", "true\n")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    runner = FakeRunner(outputs={
        "kubeconform": _kubeconform_json([
            {
                "filename": "gitops/apps/a.yaml", "kind": "DNSConfig", "name": "x",
                "version": "tailscale.com/v1alpha1", "status": "statusSkipped", "msg": "",
            },
        ]).encode(),
    })
    report = LintReport()
    lint_manifests(Context.open(empty_repo, runner=runner), report, offline=False)
    assert len(runner.calls) == 1  # no master rerun for a CRD group
    assert report.findings == []
    assert report.skipped == ["no schema for DNSConfig/x (tailscale.com/v1alpha1)"]


_GOOD_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
        - name: nginx
          image: nginx:1.25
"""

_BAD_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bad
  bogus: true
spec:
  replicas: 1
  selector:
    matchLabels:
      app: bad
  template:
    metadata:
      labels:
        app: bad
    spec:
      containers:
        - name: bad
          image: nginx:1.25
"""


@need("kubeconform")
def test_real_kubeconform_validates_manifests(empty_repo, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))  # never touch the real ~/.cache
    apps = empty_repo.root / "gitops" / "apps"
    apps.mkdir(parents=True)
    (apps / "good.yaml").write_text(_GOOD_DEPLOYMENT)
    (apps / "bad.yaml").write_text(_BAD_DEPLOYMENT)
    report = LintReport()
    lint_manifests(Context.open(empty_repo), report, offline=False)

    bad_findings = [f for f in report.findings if f.path == "gitops/apps/bad.yaml"]
    if not bad_findings or not any("bogus" in f.message for f in bad_findings):
        pytest.skip(f"kubeconform did not report the expected schema error (network issue?): {report.render()}")
    assert bad_findings[0].level == "error"

    good_findings = [f for f in report.findings if f.path == "gitops/apps/good.yaml"]
    if good_findings:
        pytest.skip(f"good.yaml unexpectedly flagged (network issue?): {report.render()}")


@need("kubeconform")
def test_real_kubeconform_download_error_msg_matches_our_prefix(tmp_path):
    """Pins the exact msg shape an unreachable schema host produces, since offline handling in
    `parse_kubeconform` depends on it starting with `_DOWNLOAD_ERROR_PREFIX`."""
    (tmp_path / "x.yaml").write_text("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: test\n")
    cache = tmp_path / "cache"
    cache.mkdir()
    proc = Runner().run(
        [
            "kubeconform", "-output", "json", "-verbose", "-ignore-missing-schemas",
            "-cache", str(cache),
            "-schema-location", "https://nonexistent.invalid.example/{{.ResourceKind}}.json",
            "x.yaml",
        ],
        cwd=tmp_path, check=False,
    )
    data = json.loads(proc.stdout.decode())
    resources = data["resources"]
    if not resources or resources[0]["status"] != "statusError":
        pytest.skip(f"kubeconform did not report a download error (network/DNS issue?): {data}")
    msg = resources[0]["msg"]
    if not msg.startswith("failed downloading schema"):
        pytest.skip(f"kubeconform's error msg shape changed: {msg!r}")
    assert msg.startswith(_DOWNLOAD_ERROR_PREFIX)
