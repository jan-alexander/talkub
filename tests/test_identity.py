import os

import pytest
from helpers import FakeRunner, need

from talkub.errors import NoIdentity
from talkub.identity import (
    create_identity,
    identities_dir,
    identity_files,
    master_key_candidates_from_env,
    own_recipients,
    recipient_of,
)
from talkub.runner import Runner

SECRET = "AGE-SECRET-KEY-133FP6CADJCE6M9H6AUXY3NLXNX73NSSLQS0CHGVLPSVZKK87V80SPY0R56"
SECRET2 = "AGE-SECRET-KEY-1LHPX92WHKCJYFAXTHK80Z8AU99RC40TTJWPGPX2QKM37N4XJNAWSJJJVF7"


def test_identities_dir_uses_xdg(tmp_path):
    assert (
        identities_dir({"XDG_CONFIG_HOME": str(tmp_path)})
        == tmp_path / "talkub" / "identities"
    )


def test_master_key_from_env_direct():
    assert master_key_candidates_from_env(
        {"SOPS_AGE_KEY": f"# comment\n{SECRET}\n"}
    ) == [SECRET]


def test_master_key_from_env_returns_every_key():
    env = {"SOPS_AGE_KEY": f"{SECRET}\n# second\n{SECRET2}\n"}
    assert master_key_candidates_from_env(env) == [SECRET, SECRET2]


def test_master_key_from_env_file(tmp_path):
    keyfile = tmp_path / "k.txt"
    keyfile.write_text(f"# created\n{SECRET}\n")
    assert master_key_candidates_from_env({"SOPS_AGE_KEY_FILE": str(keyfile)}) == [
        SECRET
    ]


def test_master_key_from_env_file_missing(tmp_path):
    with pytest.raises(NoIdentity):
        master_key_candidates_from_env({"SOPS_AGE_KEY_FILE": str(tmp_path / "nope")})


def test_master_key_from_env_absent():
    assert master_key_candidates_from_env({}) == []


def test_identity_files_prefers_talkub_identity(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("x")
    b.write_text("x")
    env = {
        "TALKUB_IDENTITY": f"{a}{os.pathsep}{b}",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    }
    assert identity_files(env) == [a, b]


def test_identity_files_missing_talkub_identity(tmp_path):
    with pytest.raises(NoIdentity):
        identity_files({"TALKUB_IDENTITY": str(tmp_path / "missing.txt")})


def test_identity_files_default_dir_skips_hidden(tmp_path):
    directory = identities_dir({"XDG_CONFIG_HOME": str(tmp_path)})
    directory.mkdir(parents=True)
    (directory / "yubikey.txt").write_text("x")
    (directory / ".DS_Store").write_text("x")
    assert identity_files({"XDG_CONFIG_HOME": str(tmp_path)}) == [
        directory / "yubikey.txt"
    ]


def test_recipient_of_formats(tmp_path):
    keygen = tmp_path / "sw.txt"
    keygen.write_text("# created: x\n# public key: age1abc\nAGE-SECRET-KEY-1X\n")
    yubikey = tmp_path / "yk.txt"
    yubikey.write_text(
        "#       Serial: 1, Slot: 1\n#    Recipient: age1yubikey1xyz\nAGE-PLUGIN-YUBIKEY-1X\n"
    )
    assert recipient_of(keygen) == "age1abc"
    assert recipient_of(yubikey) == "age1yubikey1xyz"


@need("age-keygen")
def test_create_software_identity(tmp_path):
    path, public = create_identity(Runner(), "software", tmp_path / "ids")
    assert path.name == "software.txt"
    assert public.startswith("age1")
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "ids").stat().st_mode & 0o777 == 0o700


def test_create_yubikey_identity_uses_plugin(tmp_path):
    runner = FakeRunner(
        outputs={
            "age-plugin-yubikey": b"#    Recipient: age1yubikey1xyz\nAGE-PLUGIN-YUBIKEY-1X\n"
        }
    )
    path, public = create_identity(runner, "yubikey", tmp_path)
    assert public == "age1yubikey1xyz"
    assert [c.argv for c in runner.calls] == [
        ["age-plugin-yubikey", "--generate"],
        ["age-plugin-yubikey", "--identity"],
    ]


def test_create_secure_enclave_identity(tmp_path):
    def write_identity(argv):
        out = argv[argv.index("-o") + 1]
        with open(out, "w") as fh:
            fh.write("# public key: age1se1abc\nAGE-PLUGIN-SE-1X\n")

    runner = FakeRunner(on_run=write_identity)
    _, public = create_identity(runner, "se", tmp_path)
    assert public == "age1se1abc"
    assert runner.calls[0].argv[:2] == ["age-plugin-se", "keygen"]


def test_own_recipients_requires_identity(tmp_path):
    with pytest.raises(NoIdentity) as exc:
        own_recipients({"XDG_CONFIG_HOME": str(tmp_path)}, user="jam")
    assert "identity new" in exc.value.hint


def test_own_recipients_names_by_user_and_file(tmp_path):
    directory = identities_dir({"XDG_CONFIG_HOME": str(tmp_path)})
    directory.mkdir(parents=True)
    (directory / "yubikey.txt").write_text("#    Recipient: age1yubikey1xyz\n")
    result = own_recipients({"XDG_CONFIG_HOME": str(tmp_path)}, user="jan.mock")
    assert [(r.name, r.key) for r in result] == [
        ("jan.mock-yubikey", "age1yubikey1xyz")
    ]
