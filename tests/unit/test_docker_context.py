from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


def test_docker_context_is_deny_by_default_with_only_build_sources_allowed() -> None:
    rules = [
        line.strip()
        for line in (REPOSITORY / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert rules[0] == "**"
    allowed_roots = {rule.removeprefix("!").split("/", 1)[0] for rule in rules[1:]}
    assert allowed_roots.isdisjoint({"private_benchmark_media", "models", ".env", ".git"})
    assert "!apps/**" in rules
    assert "!packages/**" in rules
    assert "!scripts/fetch_face_models.py" in rules
