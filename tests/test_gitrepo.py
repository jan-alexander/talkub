import pytest

from talkub import gitrepo
from talkub.errors import ValidationError
from talkub.gitrepo import GitUrl, classify_url
from talkub.fsutil import atomic_write
from talkub.repo import Repo
from talkub.scaffold import TEMPLATES


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:me/cluster.git", GitUrl("git@github.com:me/cluster.git", "ssh", "github.com", None)),
        ("  git@git.example.com:/srv/cluster.git ", GitUrl("git@git.example.com:/srv/cluster.git", "ssh", "git.example.com", None)),
        ("ssh://git@git.example.com/me/c.git", GitUrl("ssh://git@git.example.com/me/c.git", "ssh", "git.example.com", None)),
        ("ssh://git@git.example.com:2222/me/c.git", GitUrl("ssh://git@git.example.com:2222/me/c.git", "ssh", "git.example.com", 2222)),
        ("ssh://git@git.example.com:22/me/c.git", GitUrl("ssh://git@git.example.com:22/me/c.git", "ssh", "git.example.com", None)),
        ("https://github.com/me/cluster.git", GitUrl("https://github.com/me/cluster.git", "https", "github.com", None)),
        ("https://git.example.com:8443/me/c.git", GitUrl("https://git.example.com:8443/me/c.git", "https", "git.example.com", 8443)),
    ],
)
def test_classify_url_accepts(url, expected):
    assert classify_url(url) == expected


@pytest.mark.parametrize(
    "url",
    ["", "http://github.com/me/c.git", "file:///srv/c.git", "github.com/me/c.git", "https://github.com",
     "https://github.com/", "ssh://git@host:notaport/x.git", "git@:path", "https://github.com/me/c.git?x=1"],
)
def test_classify_url_rejects(url):
    with pytest.raises(ValidationError) as exc:
        classify_url(url)
    assert "e.g. git@github.com:" in (exc.value.hint or "")


def test_classify_url_rejects_embedded_credentials_without_echoing_them():
    with pytest.raises(ValidationError) as exc:
        classify_url("https://jam:ghp_SECRET123@github.com/me/c.git")
    assert "ghp_SECRET123" not in str(exc.value)
    assert "ghp_SECRET123" not in (exc.value.hint or "")
    assert "talkub repo credentials" in (exc.value.hint or "")


def test_placeholder_url():
    assert gitrepo.placeholder_url("homelab") == "https://example.invalid/CHANGE-ME/homelab.git"


OLD = "https://example.invalid/CHANGE-ME/test.git"
NEW = "git@github.com:me/test.git"


def _gitops(tmp_path) -> Repo:
    """root.yaml + the two app templates rendered with OLD, as `init` writes them."""
    from string import Template
    from talkub.charts import DEFAULT_CHARTS

    repo = Repo(tmp_path)
    values = {"git_url": OLD, "name": "test"}
    for key, (repo_url, chart_name) in DEFAULT_CHARTS.items():
        prefix = key.replace("-", "_")
        values |= {f"{prefix}_repo": repo_url, f"{prefix}_chart": chart_name, f"{prefix}_version": "0.0.0"}
    for rel in ("gitops/root.yaml", "gitops/apps/argocd.yaml", "gitops/apps/sealed-secrets.yaml"):
        template = (TEMPLATES / "repo" / (rel + ".tmpl")).read_text()
        atomic_write(repo.root / rel, Template(template).safe_substitute(values).encode())
    return repo


def test_rewrite_changes_only_matching_repo_urls_and_keeps_comments(tmp_path):
    repo = _gitops(tmp_path)
    before = {p: p.read_text() for p in (repo.gitops_dir).rglob("*.yaml")}
    changed = gitrepo.rewrite_repo_urls(repo, OLD, NEW)
    assert [repo.rel(p) for p in changed] == [
        "gitops/root.yaml", "gitops/apps/argocd.yaml", "gitops/apps/sealed-secrets.yaml",
    ]
    for path, text in before.items():
        assert path.read_text() == text.replace(OLD, NEW)  # byte-identical apart from the URL
    assert "argoproj.github.io/argo-helm" in (repo.gitops_dir / "apps/argocd.yaml").read_text()


def test_rewrite_handles_multi_document_files_and_skips_untouched_files(tmp_path):
    repo = _gitops(tmp_path)
    other = repo.gitops_dir / "apps" / "extra.yml"
    other.write_text(
        "# two apps\n"
        "apiVersion: argoproj.io/v1alpha1\nkind: Application\nspec:\n  source:\n    repoURL: " + OLD + "\n"
        "---\n"
        "apiVersion: argoproj.io/v1alpha1\nkind: Application\nspec:\n  source:\n    repoURL: https://charts.example.com\n"
    )
    unrelated = repo.gitops_dir / "apps" / "unrelated.yaml"
    unrelated.write_text("kind: ConfigMap\ndata: {a: b}\n")
    mtime = unrelated.stat().st_mtime_ns
    gitrepo.rewrite_repo_urls(repo, OLD, NEW)
    text = other.read_text()
    assert text.startswith("# two apps\n") and text.count(NEW) == 1 and "https://charts.example.com" in text
    assert unrelated.stat().st_mtime_ns == mtime


def test_rewrite_without_matches_writes_nothing(tmp_path):
    repo = _gitops(tmp_path)
    assert gitrepo.rewrite_repo_urls(repo, "https://other.example/x.git", NEW) == []


def test_rewrite_aborts_on_invalid_yaml_before_writing_anything(tmp_path):
    repo = _gitops(tmp_path)
    root_before = (repo.gitops_dir / "root.yaml").read_text()
    (repo.gitops_dir / "apps" / "broken.yaml").write_text("spec: [unclosed\n")
    with pytest.raises(ValidationError) as exc:
        gitrepo.rewrite_repo_urls(repo, OLD, NEW)
    assert "gitops/apps/broken.yaml" in str(exc.value)
    assert (repo.gitops_dir / "root.yaml").read_text() == root_before
