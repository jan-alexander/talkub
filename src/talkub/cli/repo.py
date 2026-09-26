"""repo set-url, repo credentials, repo apply."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from talkub import bootstrap, gitrepo
from talkub.cli.prompts import read_secret
from talkub.core.errors import ValidationError
from talkub.kube.session import kube_session

app = typer.Typer(
    help="The git repo Argo CD pulls from: URL and credentials.", no_args_is_help=True
)
NEXT = "next: commit, push, then `talkub repo apply`"


@app.command("set-url")
def set_url(
    ctx: typer.Context, url: str = typer.Argument(..., help="SSH or HTTPS git URL.")
) -> None:
    """Store the git URL and point the gitops/ repoURLs at it (files only)."""
    context = ctx.obj.context()
    result = gitrepo.set_url(context, url)
    if result.unchanged:
        print("git URL already set; nothing to do")
        return
    for path in result.changed:
        print(f"updated {context.repo.rel(path)}")
    if result.credentials_updated:
        print(f"updated {context.repo.rel(gitrepo.credentials_path(context.repo))}")
    if result.credentials_need_refresh:
        print(
            "note: the repo host changed — run talkub repo credentials to trust the new host "
            "and register a deploy key or token there"
        )
    stored_url = context.repo.load_settings().git.url
    if hint := gitrepo.origin_hint(context.repo.root, stored_url, context.runner):
        print(hint)
    print(NEXT)


@app.command("credentials")
def credentials(
    ctx: typer.Context,
    username: str = typer.Option(
        None, "--username", help="HTTPS user name (default: prompt)."
    ),
    token_file: Path = typer.Option(
        None, "--token-file", help="File with the HTTPS token (default: hidden prompt)."
    ),
    remove: bool = typer.Option(
        False,
        "--remove",
        help="Delete the stored credentials (the cluster keeps its copy).",
    ),
) -> None:
    """Create Argo CD credentials for the repo: SSH deploy key or HTTPS token."""
    context = ctx.obj.context()
    repo = context.repo
    path = gitrepo.credentials_path(repo)
    settings = repo.load_settings()
    if remove:
        if not path.exists():
            print("no repo credentials stored")
            return
        path.unlink()
        print(f"removed {repo.rel(path)}")
        print(
            f"note: the cluster keeps its copy; `talkub k -n argocd delete secret repo-{settings.name}` removes it"
        )
        return
    if settings.git is None:
        raise ValidationError(
            "no git URL in .talkub.yaml", hint="talkub repo set-url <url>"
        )
    url = gitrepo.classify_url(settings.git.url)
    if url.kind == "ssh" and (username or token_file):
        raise ValidationError("--username/--token-file are for https URLs")
    if path.exists() and not typer.confirm(
        "Replace the existing repo credentials?", default=False
    ):
        raise typer.Exit(1)
    if url.kind == "ssh":
        result = gitrepo.create_ssh_credentials(
            context,
            settings.name,
            url,
            confirm=lambda question: typer.confirm(question, default=False),
        )
        print(f"wrote {repo.rel(result.path)}")
        if result.known_hosts_written:
            print("updated gitops/values/argocd.yaml (configs.ssh.extraHosts)")
            print("talkub repo apply also upgrades Argo CD with the new known hosts")
        print("add this public key as a read-only deploy key to the repo:")
        print(result.public_key)
    else:
        user = username or typer.prompt("User name")
        token = read_secret(token_file, "Token: ", repo_root=repo.root)
        print(
            f"wrote {repo.rel(gitrepo.create_https_credentials(context, settings.name, url, user, token))}"
        )
    print(NEXT)


@app.command("apply")
def apply(ctx: typer.Context) -> None:
    """Apply the repo credentials and gitops/root.yaml to the cluster."""
    context = ctx.obj.context()
    if warning := gitrepo.placeholder_warning(context.repo):
        print(f"warning: {warning}", file=sys.stderr)
    with kube_session(context) as kube:
        if gitrepo.has_extra_hosts(context.repo):
            print(
                "upgrading Argo CD with the known hosts from gitops/values/argocd.yaml"
            )
            bootstrap.upgrade_argocd(context, kube)
        gitrepo.apply(context, kube)
    print("applied the repo credentials (if any) and gitops/root.yaml")
