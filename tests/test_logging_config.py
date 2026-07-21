import json
from pathlib import Path


def test_uvicorn_uses_single_line_praxis_formatter() -> None:
    config = json.loads(
        Path("app/uvicorn_log_config.json").read_text(encoding="utf-8")
    )

    assert config["formatters"]["praxis"]["()"] == (
        "app.logging_config.PraxisJsonFormatter"
    )
    assert set(config["loggers"]) == {"uvicorn", "uvicorn.error", "uvicorn.access"}
