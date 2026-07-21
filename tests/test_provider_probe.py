from types import SimpleNamespace

from scripts.verify_providers import _matches_sentinel


def test_provider_probe_requires_exact_sentinel() -> None:
    matching = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="PRAXIS_M0_OK"))]
    )
    mismatch = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="not the marker"))]
    )

    assert _matches_sentinel(matching) is True
    assert _matches_sentinel(mismatch) is False
