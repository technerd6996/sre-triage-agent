"""An adapter ships a Manifest. The Configuration pane renders its form from it,
so a new adapter needs no UI work."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "text"            # text | secret | number | bool | select
    required: bool = False
    default: str | None = None
    help: str = ""
    options: tuple = ()
    live: bool = True             # False = needs a restart to apply


@dataclass(frozen=True)
class Manifest:
    port: str                     # "ticketing", "inventory", ...
    provider: str                 # "servicenow", ...
    title: str
    fields: tuple = ()
    capabilities: frozenset = frozenset()

    @property
    def secret_keys(self) -> list:
        return [f.key for f in self.fields if f.kind == "secret"]