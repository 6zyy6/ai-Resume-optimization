import argparse
import compileall
import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "packages" / "api"
sys.path.insert(0, str(API_ROOT))


def lint() -> None:
    if not compileall.compile_dir(API_ROOT / "app", quiet=1):
        raise SystemExit(1)
    importlib.import_module("app.contracts")


def test() -> None:
    subprocess.run([sys.executable, "-m", "pytest"], check=True, cwd=API_ROOT)


def build() -> None:
    if not compileall.compile_dir(API_ROOT / "app", quiet=1):
        raise SystemExit(1)


def migrate() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "packages/api/alembic.ini",
            "upgrade",
            "head",
        ],
        check=True,
        cwd=ROOT,
    )


def dev() -> None:
    if not (API_ROOT / "app" / "main.py").exists():
        raise SystemExit("app.main is not available until Task 2")
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["lint", "test", "build", "dev", "migrate"],
    )
    command = parser.parse_args().command
    globals()[command]()


if __name__ == "__main__":
    main()
