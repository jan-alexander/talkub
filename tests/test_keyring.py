import pytest

from helpers import need
from talkub.crypto import age
from talkub.errors import DecryptFailed, NoIdentity, ValidationError
from talkub.identity import create_identity
from talkub.keyring import Keyring
from talkub.recipients import Recipient
from talkub.runner import Runner

pytestmark = need("age", "age-keygen")


def _other_identity(tmp_path, name):
    path, public = create_identity(Runner(), "software", tmp_path / f"ids-{name}", name)
    return path, public


def test_create_writes_key_files(empty_repo, software_identity):
    secret = Keyring(empty_repo, Runner()).create([Recipient("me", software_identity[1])])
    assert empty_repo.master_age.read_bytes().startswith(b"-----BEGIN AGE ENCRYPTED FILE-----")
    assert empty_repo.master_pub.read_text().strip() == age.public_key(Runner(), secret)
    assert "# name: me" in empty_repo.recipients_file.read_text()


def test_master_key_resolved_from_default_identities(keyed_repo):
    secret = Keyring(keyed_repo, Runner()).master_key()
    assert secret.startswith("AGE-SECRET-KEY-1")
    assert age.public_key(Runner(), secret) == keyed_repo.master_pub.read_text().strip()


def test_create_twice_rejected(keyed_repo, software_identity):
    with pytest.raises(ValidationError):
        Keyring(keyed_repo, Runner()).create([Recipient("me", software_identity[1])])


def test_env_master_key_wins_without_touching_master_age(keyed_repo):
    secret = Keyring(keyed_repo, Runner()).master_key()
    keyed_repo.master_age.unlink()
    assert Keyring(keyed_repo, Runner(), env={"SOPS_AGE_KEY": secret}).master_key() == secret


def test_talkub_identity_not_a_recipient(keyed_repo, tmp_path):
    stranger, _ = _other_identity(tmp_path, "stranger")
    with pytest.raises(DecryptFailed):
        Keyring(keyed_repo, Runner(), env={"TALKUB_IDENTITY": str(stranger)}).master_key()


def test_no_identity(keyed_repo, tmp_path):
    with pytest.raises(NoIdentity):
        Keyring(keyed_repo, Runner(), env={"XDG_CONFIG_HOME": str(tmp_path / "empty")}).master_key()


def test_add_lets_new_admin_decrypt(keyed_repo, tmp_path):
    bob, bob_public = _other_identity(tmp_path, "bob")
    Keyring(keyed_repo, Runner()).add(Recipient("bob", bob_public))
    as_bob = Keyring(keyed_repo, Runner(), env={"TALKUB_IDENTITY": str(bob)})
    assert as_bob.master_key() == Keyring(keyed_repo, Runner()).master_key()
    assert [r.name for r in as_bob.recipients()] == ["me", "bob"]


def test_add_duplicate_rejected(keyed_repo, software_identity):
    keyring = Keyring(keyed_repo, Runner())
    with pytest.raises(ValidationError):
        keyring.add(Recipient("me", "age1qk2vu7xxezxcfhyhasnx8c7z5j479xwevu5tqq8tc2fjqjzdegjqvepymq"))
    with pytest.raises(ValidationError):
        keyring.add(Recipient("other", software_identity[1]))


def test_remove_revokes_access(keyed_repo, tmp_path):
    bob, bob_public = _other_identity(tmp_path, "bob")
    keyring = Keyring(keyed_repo, Runner())
    keyring.add(Recipient("bob", bob_public))
    keyring.remove("bob")
    with pytest.raises(DecryptFailed):
        Keyring(keyed_repo, Runner(), env={"TALKUB_IDENTITY": str(bob)}).master_key()
    assert [r.name for r in keyring.recipients()] == ["me"]


def test_remove_last_recipient_refused(keyed_repo):
    with pytest.raises(ValidationError) as exc:
        Keyring(keyed_repo, Runner()).remove("me")
    assert "last recipient" in str(exc.value)


def test_remove_unknown(keyed_repo):
    with pytest.raises(ValidationError):
        Keyring(keyed_repo, Runner()).remove("nobody")


def test_add_with_unrelated_env_key_refused_and_master_age_untouched(keyed_repo, tmp_path):
    unrelated = age.generate(Runner())
    keyfile = tmp_path / "personal-sops-keys.txt"
    keyfile.write_text(unrelated.secret + "\n")
    before = keyed_repo.master_age.read_bytes()
    keyring = Keyring(keyed_repo, Runner(), env={"SOPS_AGE_KEY_FILE": str(keyfile)})
    with pytest.raises(NoIdentity) as exc:
        keyring.add(Recipient("backup", "age1qk2vu7xxezxcfhyhasnx8c7z5j479xwevu5tqq8tc2fjqjzdegjqvepymq"))
    assert "does not contain this cluster's master key" in str(exc.value)
    assert keyed_repo.master_age.read_bytes() == before
    assert [r.name for r in Keyring(keyed_repo, Runner()).recipients()] == ["me"]


def test_env_with_several_keys_picks_the_master(keyed_repo):
    master = Keyring(keyed_repo, Runner()).master_key()
    unrelated = age.generate(Runner())
    env = {"SOPS_AGE_KEY": f"{unrelated.secret}\n{master}\n"}
    assert Keyring(keyed_repo, Runner(), env=env).master_key() == master


def test_write_refuses_key_not_matching_master_pub(keyed_repo):
    unrelated = age.generate(Runner())
    keyring = Keyring(keyed_repo, Runner())
    before = keyed_repo.master_age.read_bytes()
    with pytest.raises(ValidationError):
        keyring._write(unrelated.secret, keyring.recipients())
    assert keyed_repo.master_age.read_bytes() == before
