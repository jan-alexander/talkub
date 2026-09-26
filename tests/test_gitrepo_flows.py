from string import Template

import pytest
from helpers import need

from talkub import gitrepo
from talkub.config import GitSpec
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.fsutil import atomic_write
from talkub.kube import kube_session
from talkub.scaffold import TEMPLATES

pytestmark = need("age", "age-keygen", "sops")

PLACEHOLDER = "https://example.invalid/CHANGE-ME/test.git"
SSH_URL = "git@github.com:me/test.git"


@pytest.fixture
def ctx(repo_with_configs):
    for rel in (
        "gitops/root.yaml",
        "gitops/apps/argocd.yaml",
        "gitops/apps/sealed-secrets.yaml",
    ):
        template = (TEMPLATES / "repo" / (rel + ".tmpl")).read_text()
        text = Template(template).safe_substitute(
            {"git_url": PLACEHOLDER, "name": "test"}
        )
        atomic_write(repo_with_configs.root / rel, text.encode())
    return Context.open(repo_with_configs)


def _creds(ctx, url, **kind):
    gitrepo.write_credentials(ctx, gitrepo.build_secret("test", url, **kind))


def test_set_url_from_placeholder_without_git_block(ctx):
    result = gitrepo.set_url(ctx, SSH_URL)
    assert (
        len(result.changed) == 3
        and not result.credentials_updated
        and not result.unchanged
    )
    assert ctx.repo.load_settings().git == GitSpec(SSH_URL)
    assert PLACEHOLDER not in (ctx.repo.gitops_dir / "root.yaml").read_text()


def test_set_url_again_is_a_no_op(ctx):
    gitrepo.set_url(ctx, SSH_URL)
    result = gitrepo.set_url(ctx, SSH_URL)
    assert result.unchanged and result.changed == []


def test_set_url_from_stored_url_updates_credentials(ctx):
    gitrepo.set_url(ctx, SSH_URL)
    _creds(ctx, SSH_URL, ssh_private_key="KEY\n")
    result = gitrepo.set_url(ctx, "git@gitlab.com:me/test.git")
    assert result.credentials_updated and len(result.changed) == 3
    secret = yamlio.load(ctx.sops.decrypt(gitrepo.credentials_path(ctx.repo)).decode())
    import base64

    assert (
        base64.b64decode(secret["data"]["url"]).decode() == "git@gitlab.com:me/test.git"
    )


def test_set_url_to_different_ssh_host_flags_credentials_need_refresh(ctx):
    gitrepo.set_url(ctx, SSH_URL)
    _creds(ctx, SSH_URL, ssh_private_key="KEY\n")
    result = gitrepo.set_url(ctx, "git@gitlab.com:me/test.git")
    assert result.credentials_need_refresh


def test_set_url_same_host_different_path_keeps_credentials(ctx):
    gitrepo.set_url(ctx, SSH_URL)
    _creds(ctx, SSH_URL, ssh_private_key="KEY\n")
    result = gitrepo.set_url(ctx, "git@github.com:me/other.git")
    assert not result.credentials_need_refresh


def test_set_url_without_stored_credentials_never_flags_refresh(ctx):
    result = gitrepo.set_url(ctx, SSH_URL)
    assert not result.credentials_need_refresh


def test_set_url_kind_mismatch_changes_nothing(ctx):
    gitrepo.set_url(ctx, SSH_URL)
    _creds(ctx, SSH_URL, ssh_private_key="KEY\n")
    before = {p: p.read_bytes() for p in ctx.repo.root.rglob("*") if p.is_file()}
    with pytest.raises(ValidationError) as exc:
        gitrepo.set_url(ctx, "https://github.com/me/test.git")
    assert "--remove" in (exc.value.hint or "")
    assert {
        p: p.read_bytes() for p in ctx.repo.root.rglob("*") if p.is_file()
    } == before


def test_set_url_without_git_block_and_no_placeholder_match_fails(ctx):
    root = ctx.repo.gitops_dir / "root.yaml"
    root.write_text(root.read_text().replace(PLACEHOLDER, "https://hand.example/x.git"))
    for app in (ctx.repo.gitops_dir / "apps").iterdir():
        app.write_text(
            app.read_text().replace(PLACEHOLDER, "https://hand.example/x.git")
        )
    with pytest.raises(ValidationError):
        gitrepo.set_url(ctx, SSH_URL)
    assert ctx.repo.load_settings().git is None


def test_set_url_rejects_bad_url_before_touching_files(ctx):
    before = (ctx.repo.gitops_dir / "root.yaml").read_text()
    with pytest.raises(ValidationError):
        gitrepo.set_url(ctx, "http://github.com/me/x.git")
    assert (ctx.repo.gitops_dir / "root.yaml").read_text() == before


def test_apply_credentials_then_root(ctx, spy):
    kubectl = spy("kubectl")
    _creds(ctx, SSH_URL, ssh_private_key="KEY\n")
    with kube_session(ctx) as kube:
        gitrepo.apply(ctx, kube)
    first, second = kubectl.calls
    assert (
        first["args"][:3] == ["apply", "--server-side", "-f"] and first["files"]["-f"]
    )
    assert second["args"] == ["apply", "-f", str(ctx.repo.gitops_dir / "root.yaml")]


def test_apply_without_credentials_only_root(ctx, spy):
    kubectl = spy("kubectl")
    with kube_session(ctx) as kube:
        gitrepo.apply(ctx, kube)
    assert kubectl.argvs == [["apply", "-f", str(ctx.repo.gitops_dir / "root.yaml")]]


def test_placeholder_warning(ctx):
    assert "talkub repo set-url" in gitrepo.placeholder_warning(ctx.repo)
    gitrepo.set_url(ctx, SSH_URL)
    assert gitrepo.placeholder_warning(ctx.repo) is None
