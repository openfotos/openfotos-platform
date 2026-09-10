import pytest

from openfotos_vision import Embedding, ModelIdentity


def test_embedding_dimensions_must_match_the_model() -> None:
    model = ModelIdentity(
        detector="scrfd-2.5gf",
        recognizer="w600k-r50",
        embedding_dimensions=2,
        artifact_sha256=("a" * 64,),
    )

    with pytest.raises(ValueError, match="dimensions"):
        Embedding(values=(0.1,), model=model)
