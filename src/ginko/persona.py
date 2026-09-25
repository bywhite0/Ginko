"""Load a maintainer-reviewed persona as read-only text; runtime never writes it."""

import hashlib
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

RESOURCE_KEYS = ("identity", "style", "world")


class PersonaError(ValueError):
    """Safe to display: names the problem, never the persona text."""


@dataclass(frozen=True)
class Persona:
    persona_id: str
    version: str
    display_name: str
    identity: str
    style: str
    world: str
    # Forms of address reserved for a maintainer-configured relationship.
    relationship_terms: tuple[str, ...] = ()
    # SHA-256 over the manifest and resource texts; approval binds to this content.
    digest: str = ""


def load_ginko() -> Persona:
    return load_persona(files("ginko").joinpath("personas", "ginko"))


def load_persona(root: Traversable | Path) -> Persona:
    try:
        manifest_text = _read(root, "manifest.toml")
        manifest = tomllib.loads(manifest_text)
    except tomllib.TOMLDecodeError:
        raise PersonaError("persona manifest is not valid TOML") from None
    if manifest.get("change_policy") != "maintainer_review":
        raise PersonaError("persona must require maintainer review")
    fields = {key: manifest.get(key) for key in ("id", "version", "display_name")}
    if not all(isinstance(value, str) and value.strip() for value in fields.values()):
        raise PersonaError("persona manifest needs id, version and display_name")
    terms = manifest.get("relationship_terms", [])
    if not isinstance(terms, list) or not all(isinstance(t, str) and t.strip() for t in terms):
        raise PersonaError("relationship_terms must be a list of nonempty strings")
    digest = hashlib.sha256()
    texts = {}
    for name, text in (("manifest.toml", manifest_text), *_resources(root, manifest)):
        digest.update(name.encode() + b"\0" + text.encode() + b"\0")
        texts[name] = text
    return Persona(
        persona_id=fields["id"],
        version=fields["version"],
        display_name=fields["display_name"],
        identity=texts[manifest["identity"]],
        style=texts[manifest["style"]],
        world=texts[manifest["world"]],
        relationship_terms=tuple(terms),
        digest=digest.hexdigest(),
    )


def _resources(root: Traversable | Path, manifest: dict) -> list[tuple[str, str]]:
    names = [manifest.get(key) for key in RESOURCE_KEYS]
    for name in names:
        # Resources live beside the manifest; no paths may escape the persona directory.
        if not isinstance(name, str) or not name or any(c in name for c in "/\\:") or ".." in name:
            raise PersonaError("persona resources must be file names in the persona directory")
    return [(name, _read(root, name)) for name in names]


def _read(root: Traversable | Path, name: str) -> str:
    try:
        # Normalize line endings so a checkout's newline style cannot change the digest.
        return root.joinpath(name).read_text(encoding="utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeError):
        raise PersonaError(f"cannot read persona resource {name}") from None
