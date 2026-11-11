"""Run database setup and the API without requiring a shell in the image."""
import os
import subprocess
import sys


def main():
    commands = (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "-m", "scripts.run_seeders"],
    )
    for command in commands:
        subprocess.run(command, check=True)
    # Replace PID 1 with Uvicorn so Docker/Kubernetes signals reach the server
    # directly and graceful shutdown works without a shell/init wrapper.
    os.execv(
        sys.executable,
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


if __name__ == "__main__":
    main()
