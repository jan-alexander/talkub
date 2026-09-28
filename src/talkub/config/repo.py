"""Locating a cluster repo and reading/writing its settings (.talkub.yaml)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub.config.fields import MARKER
from talkub.config.settings import Settings
from talkub.core import yamlio
from talkub.core.errors import RepoNotFound, ValidationError
from talkub.core.fsutil import atomic_write


@dataclass(frozen=True)
class Repo:
    root: Path

    @property
    def settings_file(self) -> Path:
        return self.root / MARKER

    @property
    def sops_config(self) -> Path:
        return self.root / ".sops.yaml"

    @property
    def keys_dir(self) -> Path:
        return self.root / "keys"

    @property
    def recipients_file(self) -> Path:
        return self.keys_dir / "recipients.txt"

    @property
    def master_age(self) -> Path:
        return self.keys_dir / "master.age"

    @property
    def master_next_age(self) -> Path:
        return self.keys_dir / "master.next.age"

    @property
    def master_pub(self) -> Path:
        return self.keys_dir / "master.pub"

    @property
    def talos_dir(self) -> Path:
        return self.root / "talos"

    @property
    def talos_secrets(self) -> Path:
        return self.talos_dir / "secrets.sops.yaml"

    @property
    def talosconfig(self) -> Path:
        return self.talos_dir / "talosconfig.sops.yaml"

    @property
    def kubeconfig(self) -> Path:
        return self.root / "kubernetes" / "kubeconfig.sops.yaml"

    @property
    def gitops_dir(self) -> Path:
        return self.root / "gitops"

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def load_settings(self) -> Settings:
        try:
            data = yamlio.load(self.settings_file.read_text())
        except FileNotFoundError:
            raise RepoNotFound(f"{self.settings_file} does not exist") from None
        except YAMLError as err:
            raise ValidationError(f"{MARKER} is not valid YAML: {err}") from err
        return Settings.from_dict(data)

    def write_settings(self, settings: Settings) -> None:
        atomic_write(self.settings_file, yamlio.dump(settings.to_dict()).encode())

    def remove_shortcut(self, name: str) -> None:
        """Drop shortcut `name` from .talkub.yaml in place, keeping the file's comments (unlike
        write_settings, which re-serializes everything)."""
        doc = yamlio.load(self.settings_file.read_text())
        entries = doc.get("shortcuts") if isinstance(doc, dict) else None
        if not isinstance(entries, dict) or name not in entries:
            raise ValidationError(f"no shortcut {name!r} in {MARKER}")
        del entries[name]
        if not entries:
            del doc["shortcuts"]
        Settings.from_dict(doc)  # still valid before anything is written
        atomic_write(self.settings_file, yamlio.dump(doc).encode())

    def sops_files(self) -> list[Path]:
        """All SOPS-encrypted files. `.sops.yaml` (the SOPS config) is not one of them."""
        return sorted(
            path
            for path in self.root.rglob("*.sops.yaml")
            if path.name != ".sops.yaml" and ".git" not in path.relative_to(self.root).parts
        )


def find_repo(start: Path, explicit: Path | None = None) -> Repo:
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not (root / MARKER).is_file():
            raise RepoNotFound(f"{root} is not a talkub repo (no {MARKER})")
        return Repo(root)
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / MARKER).is_file():
            return Repo(candidate)
    raise RepoNotFound(
        f"no {MARKER} found in {current} or any parent directory",
        hint="cd into a cluster repo, pass --repo, or create one with `talkub init`",
    )
