import pytest

from helpers import need
from talkub.cli.main import main
from talkub.context import Context
from talkub.lint import DEFAULT_YAMLLINT, Finding, LintReport, lint_yaml, run, yaml_files


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
