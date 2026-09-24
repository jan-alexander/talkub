import pytest

from talkub import gitrepo
from talkub.errors import ValidationError
from talkub.gitrepo import GitUrl, classify_url


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
