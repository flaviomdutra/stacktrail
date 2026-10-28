"""Small, provider-independent records shared by extraction and storage."""

from dataclasses import asdict, dataclass, field
from hashlib import sha256


def digest(*parts: str) -> str:
    return sha256("\0".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    kind: str
    repository: str
    environment: str = "unknown"
    technology: str = ""


@dataclass(frozen=True)
class Relation:
    source: str
    target: str
    kind: str
    line: int
    excerpt: str
    certainty: str = "source-backed"


@dataclass
class Extraction:
    entities: dict[str, Entity] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)

    def add(self, entity: Entity) -> Entity:
        self.entities[entity.id] = entity
        return entity


@dataclass(frozen=True)
class SourceFile:
    id: str
    path: str
    repository: str
    component: str
    content_hash: str
    content: str


@dataclass
class SyncReport:
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    pending: int = 0
    architecture_pending: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)
