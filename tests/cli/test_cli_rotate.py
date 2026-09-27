import pytest

from talkub.cli.main import main


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_talosconfig_without_finish_is_rejected(repo_with_configs, capfd):
    code = _run(
        [
            "--repo",
            str(repo_with_configs.root),
            "rotate-ca",
            "--talosconfig",
            "/tmp/whatever",
        ]
    )
    assert code == 1
    assert "--talosconfig needs --finish" in capfd.readouterr().err


def test_finish_calls_rotation_finish_with_path(repo_with_configs, tmp_path, monkeypatch, capfd):
    calls = []

    def fake_finish(ctx, talosconfig=None):
        calls.append(talosconfig)
        return ["finished"]

    monkeypatch.setattr("talkub.talos.rotation.finish", fake_finish)
    given = tmp_path / "talosconfig"
    given.write_text("fake")

    code = _run(
        [
            "--repo",
            str(repo_with_configs.root),
            "rotate-ca",
            "--finish",
            "--talosconfig",
            str(given),
        ]
    )

    assert code == 0
    assert calls == [given]
    assert "note: finished" in capfd.readouterr().out


def test_finish_without_talosconfig_passes_none(repo_with_configs, monkeypatch, capfd):
    calls = []

    def fake_finish(ctx, talosconfig=None):
        calls.append(talosconfig)
        return []

    monkeypatch.setattr("talkub.talos.rotation.finish", fake_finish)

    code = _run(["--repo", str(repo_with_configs.root), "rotate-ca", "--finish"])

    assert code == 0
    assert calls == [None]


def test_yes_skips_prompt_and_confirms(repo_with_configs, monkeypatch, capfd):
    seen_confirm = []

    def fake_rotate(ctx, confirm):
        seen_confirm.append(confirm("proceed?"))
        return ["rotated"]

    monkeypatch.setattr("talkub.talos.rotation.rotate", fake_rotate)
    monkeypatch.setattr(
        "typer.confirm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not prompt")),
    )

    code = _run(["--repo", str(repo_with_configs.root), "rotate-ca", "--yes"])

    assert code == 0
    assert seen_confirm == [True]
    assert "note: rotated" in capfd.readouterr().out


def test_without_yes_confirm_uses_typer_confirm(repo_with_configs, monkeypatch, capfd):
    seen_confirm = []

    def fake_rotate(ctx, confirm):
        seen_confirm.append(confirm("proceed?"))
        return []

    monkeypatch.setattr("talkub.talos.rotation.rotate", fake_rotate)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)

    code = _run(["--repo", str(repo_with_configs.root), "rotate-ca"])

    assert code == 0
    assert seen_confirm == [False]


def test_finish_with_missing_talosconfig_is_a_clean_error(repo_with_configs, tmp_path, monkeypatch, capfd):
    monkeypatch.setattr(
        "talkub.talos.rotation.finish",
        lambda *a, **k: pytest.fail("must not be called"),
    )
    code = _run(
        [
            "--repo",
            str(repo_with_configs.root),
            "rotate-ca",
            "--finish",
            "--talosconfig",
            str(tmp_path / "nope"),
        ]
    )
    assert code == 1
    err = capfd.readouterr().err
    assert "nope" in err and "Traceback" not in err


def test_yes_with_finish_is_rejected(repo_with_configs, monkeypatch, capfd):
    monkeypatch.setattr(
        "talkub.talos.rotation.finish",
        lambda *a, **k: pytest.fail("must not be called"),
    )
    code = _run(["--repo", str(repo_with_configs.root), "rotate-ca", "--finish", "--yes"])
    assert code == 1
    assert "--yes" in capfd.readouterr().err
