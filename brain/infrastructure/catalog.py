"""The curated list of agents, shipped with the program so it works with no internet."""

import json
from importlib import resources

from brain.domain.agents import AgentName, AgentSource, AgentSpec, parse_capabilities


def _load_frozen(text: str):
    # Deeply immutable, so the catalog can be shared across request threads without copies.
    return json.loads(text, object_pairs_hook=frozendict, array_hook=tuple)


class JsonCatalog:
    def __init__(self, document: str | None = None) -> None:
        raw = document or (
            resources.files("brain.infrastructure")
            .joinpath("catalog.json")
            .read_text(encoding="utf-8")
        )
        self._specs = {
            spec.name: spec
            for spec in (
                *self._family_specs(family) for family in _load_frozen(raw)["families"]
            )
        }

    def all(self) -> tuple[AgentSpec, ...]:
        return tuple(self._specs.values())

    def find(self, name: AgentName) -> AgentSpec | None:
        return self._specs.get(name)

    @staticmethod
    def _family_specs(family: frozendict) -> list[AgentSpec]:
        return [
            AgentSpec(
                name=AgentName.parse(f"{family['family']}:{entry['tag']}"),
                family=family["family"],
                summary=family["summary"],
                capabilities=parse_capabilities(entry.get("capabilities", family["capabilities"])),
                use_cases=family["use_cases"],
                size_bytes=entry["size_bytes"],
                parameter_size=entry.get("parameter_size"),
                source=AgentSource.CATALOG,
            )
            for entry in family["agents"]
        ]
