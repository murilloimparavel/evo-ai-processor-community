"""Check the built candidate without starting servers or contacting providers."""
from importlib.metadata import version
from pathlib import Path
import tomllib


def main():
    root = Path(__file__).resolve().parents[1]
    requirements = dict(
        line.strip().split("==", 1)
        for line in (root / "requirements.txt").read_text().splitlines()
        if "==" in line and not line.lstrip().startswith("#")
    )
    project = tomllib.loads((root / "pyproject.toml").read_text())
    declared = dict(item.split("==", 1) for item in project["project"]["dependencies"] if "==" in item)
    for package in ("redis", "openai", "langgraph"):
        expected = requirements[package]
        if declared[package] != expected or version(package) != expected:
            raise RuntimeError(f"Candidate dependency mismatch for {package}")

    # Exercise the SDK integration imports used by actual model/tool execution.
    import redis.asyncio
    from openai import AsyncOpenAI
    from langgraph.graph import StateGraph
    from google.adk.models.lite_llm import LiteLlm

    print("Candidate dependency versions and SDK imports verified")


if __name__ == "__main__":
    main()
