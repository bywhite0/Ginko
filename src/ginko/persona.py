"""Load the curated persona bundled with this version, with no runtime writes."""

import tomllib
from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class Persona:
    persona_id: str
    version: str
    display_name: str
    identity: str
    style: str
    world: str


def load_ginko() -> Persona:
    root = files("ginko").joinpath("personas", "ginko")
    manifest = tomllib.loads(root.joinpath("manifest.toml").read_text(encoding="utf-8"))
    return Persona(
        persona_id=manifest["id"],
        version=manifest["version"],
        display_name=manifest["display_name"],
        identity=root.joinpath("identity.md").read_text(encoding="utf-8"),
        style=root.joinpath("style.md").read_text(encoding="utf-8"),
        world=root.joinpath("world.md").read_text(encoding="utf-8"),
    )
