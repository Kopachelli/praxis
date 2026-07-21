"""Probe every M0 Qwen candidate without exposing credentials or response bodies."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.config import OPENROUTER_BASE_URL, Settings  # noqa: E402

EXPECTED_SENTINEL = "PRAXIS_M0_OK"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    provider: str
    model: str
    outcome: str
    detail: str


def _matches_sentinel(response) -> bool:
    if len(response.choices) != 1:
        return False
    content = response.choices[0].message.content
    return isinstance(content, str) and content.strip() == EXPECTED_SENTINEL


def _probe(provider: str, model: str, *, api_key: str, base_url: str) -> ProbeResult:
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0, max_retries=0)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": f"Reply with exactly {EXPECTED_SENTINEL} and nothing else.",
                }
            ],
            max_tokens=16,
            temperature=0,
        )
    except Exception as exc:  # The matrix must continue after any provider/model failure.
        status_code = getattr(exc, "status_code", None)
        detail = f"{type(exc).__name__}"
        if status_code is not None:
            detail += f" HTTP {status_code}"
        return ProbeResult(provider, model, "FAILED", detail)

    if not _matches_sentinel(response):
        return ProbeResult(provider, model, "FAILED", "sentinel mismatch")
    return ProbeResult(provider, model, "WORKING", "chat completion returned")


def run_matrix(settings: Settings) -> list[ProbeResult]:
    providers = (
        (
            "qwencloud",
            settings.qwencloud_models,
            settings.dashscope_api_key,
            settings.qwen_base_url,
        ),
        (
            "openrouter",
            settings.openrouter_models,
            settings.openrouter_api_key,
            OPENROUTER_BASE_URL,
        ),
    )
    results: list[ProbeResult] = []
    for provider, models, api_key, base_url in providers:
        if not api_key:
            results.extend(
                ProbeResult(provider, model, "SKIPPED", "credential not configured")
                for model in models
            )
            continue
        results.extend(
            _probe(provider, model, api_key=api_key, base_url=base_url)
            for model in models
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    settings = Settings.from_env()
    results = run_matrix(settings)

    print("provider\tmodel\toutcome\tdetail")
    for result in results:
        print(
            f"{result.provider}\t{result.model}\t{result.outcome}\t{result.detail}"
        )

    both_providers_work = all(
        any(result.provider == provider and result.outcome == "WORKING" for result in results)
        for provider in ("qwencloud", "openrouter")
    )
    return 0 if both_providers_work else 2


if __name__ == "__main__":
    raise SystemExit(main())
