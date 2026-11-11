"""Run database setup and the API without requiring a shell in the image."""
import os
import subprocess
import sys


def main():
    commands = (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "-m", "scripts.run_seeders"],
        [
            sys.executable,
            "-m",
            "uvicorn",
            "src.main:app",
            "--host",
            os.environ.get("HOST", "0.0.0.0"),
            "--port",
            os.environ.get("PORT", "8000"),
        ],
    )
    for command in commands:
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
