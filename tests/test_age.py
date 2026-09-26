import pytest
from helpers import FakeRunner, need

from talkub.crypto import age
from talkub.errors import DecryptFailed, ValidationError
from talkub.runner import Runner


@need("age", "age-keygen")
def test_roundtrip_with_multiple_recipients(tmp_path):
    runner = Runner()
    alice, bob = age.generate(runner), age.generate(runner)
    ciphertext = age.encrypt(runner, b"top secret\n", [alice.public, bob.public])
    assert ciphertext.startswith(b"-----BEGIN AGE ENCRYPTED FILE-----")
    encrypted = tmp_path / "x.age"
    encrypted.write_bytes(ciphertext)
    bob_identity = tmp_path / "bob.txt"
    bob_identity.write_text(bob.secret + "\n")
    assert age.decrypt(runner, encrypted, [bob_identity]) == b"top secret\n"


@need("age", "age-keygen")
def test_public_key_matches_generated():
    runner = Runner()
    pair = age.generate(runner)
    assert age.public_key(runner, pair.secret) == pair.public


@need("age", "age-keygen")
def test_decrypt_with_wrong_identity(tmp_path):
    runner = Runner()
    owner, stranger = age.generate(runner), age.generate(runner)
    encrypted = tmp_path / "x.age"
    encrypted.write_bytes(age.encrypt(runner, b"x", [owner.public]))
    identity = tmp_path / "stranger.txt"
    identity.write_text(stranger.secret + "\n")
    with pytest.raises(DecryptFailed) as exc:
        age.decrypt(runner, encrypted, [identity])
    assert "recipients.txt" in exc.value.hint


def test_decrypt_lets_plugin_prompts_reach_terminal(tmp_path):
    runner = FakeRunner()
    age.decrypt(runner, tmp_path / "m.age", [tmp_path / "yubikey.txt"])
    assert runner.calls[0].passthrough_stderr is True
    assert runner.calls[0].argv == [
        "age",
        "--decrypt",
        "-i",
        str(tmp_path / "yubikey.txt"),
        str(tmp_path / "m.age"),
    ]


def test_encrypt_requires_recipients():
    with pytest.raises(ValidationError):
        age.encrypt(FakeRunner(), b"x", [])
