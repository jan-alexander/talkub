import pytest
from helpers import need

from talkub.context import Context
from talkub.core.errors import DecryptFailed, ValidationError
from talkub.core.runner import Runner
from talkub.secrets.sops import Sops

pytestmark = need("age", "age-keygen", "sops")


def test_rotate_reencrypts_everything(repo_with_configs):
    ctx = Context.open(repo_with_configs)
    old_key = ctx.keyring.master_key()
    old_pub = ctx.keyring.public_key()

    ctx.keyring.rotate(ctx.sops)

    fresh = Context.open(repo_with_configs)
    new_key = fresh.keyring.master_key()
    assert new_key != old_key
    assert fresh.keyring.public_key() != old_pub
    assert fresh.keyring.public_key() in repo_with_configs.sops_config.read_text()
    assert fresh.sops.decrypt(repo_with_configs.talosconfig) == b"talos: marker\n"
    with pytest.raises(DecryptFailed):
        Sops(Runner(), lambda: old_key).decrypt(repo_with_configs.talosconfig)
    assert not repo_with_configs.master_next_age.exists()


def test_interrupted_rotation_blocks_new_one(repo_with_configs):
    repo_with_configs.master_next_age.write_text("leftover")
    ctx = Context.open(repo_with_configs)
    with pytest.raises(ValidationError) as exc:
        ctx.keyring.rotate(ctx.sops)
    assert "master.next.age" in exc.value.hint
