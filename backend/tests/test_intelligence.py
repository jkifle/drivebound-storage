import math

from app.services.intelligence import cosine_similarity, fallback_embedding


def test_fallback_embedding_is_deterministic_and_normalized() -> None:
    first = fallback_embedding("sunset at the lake")
    second = fallback_embedding("sunset at the lake")
    assert first == second
    assert len(first) == 384
    assert math.isclose(sum(value * value for value in first), 1.0)


def test_cosine_similarity_prefers_related_features() -> None:
    sunset = fallback_embedding("orange sunset beach")
    same = fallback_embedding("orange sunset beach")
    unrelated = fallback_embedding("tax document receipt")
    assert cosine_similarity(sunset, same) > cosine_similarity(sunset, unrelated)
