"""Build the FC custom-runtime dependency bundle with the official fc3 image."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

FC_BUILD_IMAGE = (
    "registry.hub.docker.com/aliyunfc/runtime-custom.debian10:build-3.1.0"
)
FC_PYTHON = "/var/fc/lang/python3.10/bin/python3"


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    requirements = repository / "requirements.txt"
    app_dir = repository / "app"
    output = repository / "python"
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker CLI is required")
    if not requirements.is_file() or not app_dir.is_dir():
        raise RuntimeError("Run this script from the Praxis repository")
    if output.resolve().parent != repository.resolve():
        raise RuntimeError("Refusing to write outside the Praxis repository")

    container = f"praxis-fc-build-{uuid.uuid4().hex[:12]}"
    if output.exists():
        shutil.rmtree(output)
    try:
        _run(
            [
                docker,
                "create",
                "--platform",
                "linux/amd64",
                "--name",
                container,
                "-v",
                f"{requirements.resolve()}:/work/requirements.txt:ro",
                "--entrypoint",
                FC_PYTHON,
                FC_BUILD_IMAGE,
                "-m",
                "pip",
                "install",
                "--target",
                "/work/python",
                "--requirement",
                "/work/requirements.txt",
                "--upgrade",
                "--ignore-installed",
            ]
        )
        _run([docker, "start", "--attach", container])
        _run([docker, "cp", f"{container}:/work/python", str(output)])
    finally:
        subprocess.run(
            [docker, "rm", "--force", container],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    _run(
        [
            docker,
            "run",
            "--platform",
            "linux/amd64",
            "--rm",
            "-e",
            "PYTHONPATH=/work/python:/work",
            "-v",
            f"{app_dir.resolve()}:/work/app:ro",
            "-v",
            f"{output.resolve()}:/work/python:ro",
            "-w",
            "/work",
            FC_BUILD_IMAGE,
            FC_PYTHON,
            "-c",
            (
                "import fastapi, httpx, openai, pydantic, tablestore, uvicorn; "
                "import app.main; print('fc_imports=ok')"
            ),
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
