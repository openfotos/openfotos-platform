"""Provider-independent face engine contracts."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    detector: str
    recognizer: str
    embedding_dimensions: int
    artifact_sha256: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Embedding:
    values: tuple[float, ...]
    model: ModelIdentity

    def __post_init__(self) -> None:
        if len(self.values) != self.model.embedding_dimensions:
            raise ValueError("embedding dimensions do not match the model contract")


class FaceEngine(Protocol):
    @property
    def model(self) -> ModelIdentity: ...

    def embed_prominent_face(self, image_bytes: bytes) -> Embedding: ...

    def embed_all_faces(self, image_bytes: bytes) -> Sequence[Embedding]: ...
