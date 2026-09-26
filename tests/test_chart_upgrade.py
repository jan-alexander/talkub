"""`talkub chart upgrade`: bump one Argo CD app's Helm chart (or every chart app) to a new version."""

from __future__ import annotations

import json
import os

import pytest

from helpers import FakeRunner, write_chart_app
from talkub import argoapps, chart_upgrade, yamlio
from talkub.cli.main import main
from talkub.context import Context
from talkub.errors import CommandFailed, ToolMissing, ValidationError

CHART_OLD = b"apiVersion: v2\nname: mychart\nversion: 1.0.0\nappVersion: v1.0\n"
CHART_NEW = b"apiVersion: v2\nname: mychart\nversion: 2.0.0\nappVersion: v2.0\n"


def _kubeconform_json(resources):
    return json.dumps({"resources": resources, "summary": {"valid": len(resources), "invalid": 0, "errors": 0}})


def _ok_kubeconform(kubeconform):
    kubeconform.on("-", stdout=_kubeconform_json([]), read_stdin=True)


def _setup_app(
    repo, spy, key="myapp", *, chart="mychart", repo_url="https://example.com/charts", old="1.0.0",
    template_rc=0, template_stderr="",
):
    write_chart_app(repo, key, repo_url, chart, old, "ns1")
    helm = spy("helm")
    helm.on("show", "chart", "--version", old, stdout=CHART_OLD.decode())
    helm.on("show", "chart", "--version", "2.0.0", stdout=CHART_NEW.decode())
    helm.on("show", "values", "--version", old, stdout="foo: 1\n")
    helm.on("show", "values", "--version", "2.0.0", stdout="foo: 2\n")
    helm.on(
        "template", rc=template_rc, stderr=template_stderr,
        stdout="" if template_rc else "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n",
    )
    return helm


def test_upgrade_happy_path_shows_diff_and_sets_revision(empty_repo, spy, capsys):
    helm = _setup_app(empty_repo, spy)
    kubeconform = spy("kubeconform")
    _ok_kubeconform(kubeconform)

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), "myapp", all_apps=False, version="2.0.0", confirm=lambda q: True
    )

    assert code == 0
    assert argoapps.app_source(empty_repo, "myapp").version == "2.0.0"
    out = capsys.readouterr().out
    assert "1.0.0 → 2.0.0" in out
    assert "v1.0 → v2.0" in out
    assert "-foo: 1" in out
    assert "+foo: 2" in out
    assert any("template" in argv for argv in helm.argvs)


def test_upgrade_already_current_prints_note_and_does_nothing(empty_repo, spy, capsys):
    write_chart_app(empty_repo, "myapp", "https://example.com/charts", "mychart", "1.0.0", "ns1")
    helm = spy("helm")
    helm.on("show", "chart", stdout=CHART_OLD.decode())  # the latest-version lookup (no --version)

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), "myapp", all_apps=False, version=None, confirm=lambda q: pytest.fail("not asked")
    )

    assert code == 0
    assert "already at 1.0.0" in capsys.readouterr().out
    assert argoapps.app_source(empty_repo, "myapp").version == "1.0.0"
    assert not any("template" in argv for argv in helm.argvs)


def test_upgrade_without_version_skips_when_current_is_newer_than_latest(empty_repo, spy, capsys):
    """No `--version`: `chart upgrade` always resolves to the chart's latest release, which must
    not silently downgrade an app pinned ahead of it (e.g. a manually chosen pre-release)."""
    write_chart_app(empty_repo, "myapp", "https://example.com/charts", "mychart", "2.0.0", "ns1")
    helm = spy("helm")
    helm.on("show", "chart", stdout=CHART_OLD.decode())  # the latest-version lookup (no --version): "latest" is 1.0.0

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), "myapp", all_apps=False, version=None, confirm=lambda q: pytest.fail("not asked")
    )

    assert code == 0
    assert "2.0.0 is newer than latest 1.0.0" in capsys.readouterr().out
    assert argoapps.app_source(empty_repo, "myapp").version == "2.0.0"  # untouched
    assert not any("template" in argv for argv in helm.argvs)


def test_upgrade_decline_leaves_file_unchanged(empty_repo, spy, capsys):
    _setup_app(empty_repo, spy)
    kubeconform = spy("kubeconform")

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), "myapp", all_apps=False, version="2.0.0", confirm=lambda q: False
    )

    assert code == 1
    assert argoapps.app_source(empty_repo, "myapp").version == "1.0.0"
    assert "aborted" in capsys.readouterr().out
    assert kubeconform.calls == []  # never got to lint


def test_upgrade_lint_failure_keeps_file_changed_and_exits_1(empty_repo, spy, capsys):
    _setup_app(empty_repo, spy, template_rc=1, template_stderr="boom\n")
    kubeconform = spy("kubeconform")

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), "myapp", all_apps=False, version="2.0.0", confirm=lambda q: True
    )

    assert code == 1
    # the file stays changed even though lint failed
    assert argoapps.app_source(empty_repo, "myapp").version == "2.0.0"
    out = capsys.readouterr().out
    assert "fix the values or revert the change" in out
    assert kubeconform.calls == []


def test_upgrade_all_apps_continues_after_one_failure(empty_repo, spy, capsys):
    write_chart_app(empty_repo, "app-a", "https://example.com/charts", "mychart", "1.0.0", "ns1")
    write_chart_app(empty_repo, "app-b", "https://example.com/charts", "mychart", "1.0.0", "ns1")
    helm = spy("helm")
    helm.on("show", "chart", "--version", "1.0.0", stdout=CHART_OLD.decode())
    helm.on("show", "chart", "--version", "2.0.0", stdout=CHART_NEW.decode())
    helm.on("show", "chart", stdout=CHART_NEW.decode())  # the latest-version lookup (no --version): "latest" is 2.0.0
    helm.on("show", "values", "--version", "1.0.0", stdout="foo: 1\n")
    helm.on("show", "values", "--version", "2.0.0", stdout="foo: 2\n")
    helm.on("template", "app-b", stderr="boom\n", rc=1)  # app-b fails to render
    helm.on("template", stdout="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
    kubeconform = spy("kubeconform")
    _ok_kubeconform(kubeconform)

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), None, all_apps=True, version=None, confirm=lambda q: True
    )

    assert code == 1
    assert argoapps.app_source(empty_repo, "app-a").version == "2.0.0"
    assert argoapps.app_source(empty_repo, "app-b").version == "2.0.0"  # still changed despite the lint failure
    out = capsys.readouterr().out
    assert "1/2 succeeded" in out


def test_upgrade_all_apps_with_version_is_an_error(empty_repo):
    with pytest.raises(ValidationError):
        chart_upgrade.upgrade(Context.open(empty_repo), None, all_apps=True, version="2.0.0", confirm=lambda q: True)


def test_upgrade_neither_app_nor_all_is_an_error(empty_repo):
    with pytest.raises(ValidationError):
        chart_upgrade.upgrade(Context.open(empty_repo), None, all_apps=False, version=None, confirm=lambda q: True)


def test_upgrade_app_and_all_together_is_an_error(empty_repo):
    with pytest.raises(ValidationError):
        chart_upgrade.upgrade(Context.open(empty_repo), "myapp", all_apps=True, version=None, confirm=lambda q: True)


def _write_two_source_app(repo, key):
    """An Application with two Helm chart sources: `argoapps.app_source` (which assumes exactly
    one) raises `ValidationError` for it."""
    doc = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": key},
        "spec": {
            "destination": {"namespace": "ns1"},
            "sources": [
                {"repoURL": "https://example.com/charts", "chart": "mychart", "targetRevision": "1.0.0"},
                {"repoURL": "https://example.com/charts", "chart": "other", "targetRevision": "1.0.0"},
            ],
        },
    }
    apps_dir = repo.gitops_dir / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    (apps_dir / f"{key}.yaml").write_text(yamlio.dump(doc))


def test_upgrade_all_apps_isolates_a_hard_command_failure(empty_repo, spy, capsys):
    write_chart_app(empty_repo, "app-a", "https://example.com/charts", "mychart", "1.0.0", "ns1")
    write_chart_app(empty_repo, "app-b", "https://example.com/charts", "badchart", "1.0.0", "ns1")
    helm = spy("helm")
    helm.on("badchart", stderr="not found\n", rc=1)  # app-b's `helm show chart` hard-fails
    helm.on("show", "chart", "--version", "1.0.0", stdout=CHART_OLD.decode())
    helm.on("show", "chart", "--version", "2.0.0", stdout=CHART_NEW.decode())
    helm.on("show", "chart", stdout=CHART_NEW.decode())  # app-a's latest-version lookup (no --version)
    helm.on("show", "values", "--version", "1.0.0", stdout="foo: 1\n")
    helm.on("show", "values", "--version", "2.0.0", stdout="foo: 2\n")
    helm.on("template", stdout="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
    kubeconform = spy("kubeconform")
    _ok_kubeconform(kubeconform)

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), None, all_apps=True, version=None, confirm=lambda q: True
    )

    assert code == 1
    assert argoapps.app_source(empty_repo, "app-a").version == "2.0.0"  # not aborted by app-b's failure
    assert argoapps.app_source(empty_repo, "app-b").version == "1.0.0"  # untouched
    out = capsys.readouterr().out
    assert "app-b: helm exited with 1" in out
    assert "1/2 succeeded" in out


def test_upgrade_all_apps_skips_a_multi_chart_app_without_failing(empty_repo, spy, capsys):
    """A file that isn't a single-chart app (here: two chart sources in one Application) is
    skipped in `--all` — reported by name, but not counted as a failure."""
    write_chart_app(empty_repo, "app-a", "https://example.com/charts", "mychart", "1.0.0", "ns1")
    _write_two_source_app(empty_repo, "app-c")
    helm = spy("helm")
    helm.on("show", "chart", "--version", "1.0.0", stdout=CHART_OLD.decode())
    helm.on("show", "chart", "--version", "2.0.0", stdout=CHART_NEW.decode())
    helm.on("show", "chart", stdout=CHART_NEW.decode())  # app-a's latest-version lookup (no --version)
    helm.on("show", "values", "--version", "1.0.0", stdout="foo: 1\n")
    helm.on("show", "values", "--version", "2.0.0", stdout="foo: 2\n")
    helm.on("template", stdout="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
    kubeconform = spy("kubeconform")
    _ok_kubeconform(kubeconform)

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), None, all_apps=True, version=None, confirm=lambda q: True
    )

    assert code == 0
    assert argoapps.app_source(empty_repo, "app-a").version == "2.0.0"
    out = capsys.readouterr().out
    assert "app-c: skipped (not a single-chart app)" in out
    assert "1/1 succeeded" in out


def test_upgrade_all_apps_addresses_a_file_by_stem_not_metadata_name(empty_repo, spy, capsys):
    """`gitops/apps/app-a.yaml`'s Application is named `renamed-app`; `--all` (and single-app
    upgrade) must still address it as `app-a`, the file stem — not the mismatched metadata name,
    which isn't even a file on disk."""
    write_chart_app(empty_repo, "app-a", "https://example.com/charts", "mychart", "1.0.0", "ns1")
    path = empty_repo.gitops_dir / "apps" / "app-a.yaml"
    doc = yamlio.load(path.read_text())
    doc["metadata"]["name"] = "renamed-app"
    path.write_text(yamlio.dump(doc))
    helm = spy("helm")
    helm.on("show", "chart", "--version", "1.0.0", stdout=CHART_OLD.decode())
    helm.on("show", "chart", "--version", "2.0.0", stdout=CHART_NEW.decode())
    helm.on("show", "chart", stdout=CHART_NEW.decode())
    helm.on("show", "values", "--version", "1.0.0", stdout="foo: 1\n")
    helm.on("show", "values", "--version", "2.0.0", stdout="foo: 2\n")
    helm.on("template", stdout="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
    kubeconform = spy("kubeconform")
    _ok_kubeconform(kubeconform)

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), None, all_apps=True, version=None, confirm=lambda q: True
    )

    assert code == 0
    assert argoapps.app_source(empty_repo, "app-a").version == "2.0.0"
    out = capsys.readouterr().out
    assert "1/1 succeeded" in out


def test_upgrade_single_app_addresses_a_file_by_stem_not_metadata_name(empty_repo, spy):
    write_chart_app(empty_repo, "app-a", "https://example.com/charts", "mychart", "1.0.0", "ns1")
    path = empty_repo.gitops_dir / "apps" / "app-a.yaml"
    doc = yamlio.load(path.read_text())
    doc["metadata"]["name"] = "renamed-app"
    path.write_text(yamlio.dump(doc))
    helm = spy("helm")
    helm.on("show", "chart", "--version", "1.0.0", stdout=CHART_OLD.decode())
    helm.on("show", "chart", "--version", "2.0.0", stdout=CHART_NEW.decode())
    helm.on("show", "values", "--version", "1.0.0", stdout="foo: 1\n")
    helm.on("show", "values", "--version", "2.0.0", stdout="foo: 2\n")
    helm.on("template", stdout="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
    kubeconform = spy("kubeconform")
    _ok_kubeconform(kubeconform)

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), "app-a", all_apps=False, version="2.0.0", confirm=lambda q: True
    )

    assert code == 0
    assert argoapps.app_source(empty_repo, "app-a").version == "2.0.0"


def test_upgrade_single_app_hard_failure_still_raises(empty_repo, spy):
    write_chart_app(empty_repo, "myapp", "https://example.com/charts", "badchart", "1.0.0", "ns1")
    helm = spy("helm")
    helm.on("badchart", stderr="not found\n", rc=1)

    with pytest.raises(CommandFailed):
        chart_upgrade.upgrade(
            Context.open(empty_repo), "myapp", all_apps=False, version="2.0.0", confirm=lambda q: True
        )


def test_upgrade_kubeconform_missing_notes_and_still_succeeds(empty_repo, spy, monkeypatch, capsys):
    _setup_app(empty_repo, spy)
    bindir = os.environ["PATH"].split(os.pathsep)[0]
    monkeypatch.setenv("PATH", bindir)  # helm's spy script lives here (absolute paths inside); no kubeconform

    code = chart_upgrade.upgrade(
        Context.open(empty_repo), "myapp", all_apps=False, version="2.0.0", confirm=lambda q: True
    )

    assert code == 0
    assert argoapps.app_source(empty_repo, "myapp").version == "2.0.0"
    out = capsys.readouterr().out
    assert "kubeconform not found" in out


def test_upgrade_helm_missing_is_a_failure_and_does_not_write(empty_repo, monkeypatch, tmp_path):
    write_chart_app(empty_repo, "myapp", "https://example.com/charts", "mychart", "1.0.0", "ns1")
    runner = FakeRunner(
        outputs={"helm": CHART_NEW}  # `helm show chart`/`show values` succeed (FakeRunner never checks PATH)
    )
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))  # real `shutil.which("helm")` finds nothing

    with pytest.raises(ToolMissing):
        chart_upgrade.upgrade(
            Context.open(empty_repo, runner=runner), "myapp", all_apps=False, version="2.0.0", confirm=lambda q: True
        )

    assert argoapps.app_source(empty_repo, "myapp").version == "1.0.0"  # nothing was written


def test_cli_chart_upgrade_wiring(empty_repo, spy, monkeypatch, capfd):
    _setup_app(empty_repo, spy)
    kubeconform = spy("kubeconform")
    _ok_kubeconform(kubeconform)
    monkeypatch.chdir(empty_repo.root)

    with pytest.raises(SystemExit) as exc:
        main(["chart", "upgrade", "myapp", "--version", "2.0.0", "--yes"])

    assert exc.value.code == 0
    assert argoapps.app_source(empty_repo, "myapp").version == "2.0.0"
    assert "commit and push" in capfd.readouterr().out
