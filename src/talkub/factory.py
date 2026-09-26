"""Client for the Talos Image Factory (https://factory.talos.dev)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence

from talkub.errors import CommandFailed
from talkub.schematic import FACTORY_HOST, schematic_document

DEFAULT_URL = f"https://{FACTORY_HOST}"
_BOOT_MEDIA = {"metal": "iso", "hcloud": "raw.xz"}


class FactoryClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = (
            base_url or os.environ.get("TALKUB_FACTORY_URL") or DEFAULT_URL
        ).rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: bytes | None = None) -> object:
        headers = {"Content-Type": "application/yaml"} if body is not None else {}
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise CommandFailed(
                f"Talos Image Factory request failed: {method} {path}",
                hint=f"{err}; cached schematic IDs in .talkub.yaml keep working offline",
            ) from err

    def schematic_id(
        self, extensions: Sequence[str], kernel_args: Sequence[str]
    ) -> str:
        data = self._request(
            "POST", "/schematics", schematic_document(extensions, kernel_args).encode()
        )
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise CommandFailed(
                "unexpected answer from the Talos Image Factory", hint=str(data)
            )
        return data["id"]

    def official_extensions(self, talos_version: str) -> list[str]:
        data = self._request("GET", f"/version/{talos_version}/extensions/official")
        if not isinstance(data, list):
            raise CommandFailed(
                "unexpected answer from the Talos Image Factory", hint=str(data)[:200]
            )
        return sorted(
            str(item["name"])
            for item in data
            if isinstance(item, dict) and "name" in item
        )


def boot_media_url(
    schematic_id: str, talos_version: str, platform: str, arch: str
) -> str:
    """Download URL for the boot medium: ISO for bare metal, raw.xz disk image for Hetzner."""
    return f"{DEFAULT_URL}/image/{schematic_id}/{talos_version}/{platform}-{arch}.{_BOOT_MEDIA[platform]}"
