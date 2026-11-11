"""Check the built candidate without starting servers or contacting providers."""
from importlib.metadata import distributions, version
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
    for package in (
        "redis", "openai", "langgraph", "fastapi", "starlette", "authlib",
        "mako", "litellm", "setuptools", "wheel", "google-adk", "google-genai",
        "pydantic", "pydantic-settings",
    ):
        expected = requirements[package]
        if declared.get(package, expected) != expected or version(package) != expected:
            raise RuntimeError(f"Candidate dependency mismatch for {package}")

    # Setuptools vendors its own copies. Updating only top-level packages does
    # not remove vulnerabilities in the build backend's embedded dependencies.
    import setuptools
    from packaging.version import Version

    vendor_path = Path(setuptools.__file__).parent / "_vendor"
    minimums = {"jaraco.context": "6.1.0", "wheel": "0.46.2"}
    for package in distributions(path=[str(vendor_path)]):
        name = package.metadata["Name"].lower()
        if name in minimums and Version(package.version) < Version(minimums[name]):
            raise RuntimeError(f"Vulnerable vendored dependency in setuptools: {name}")

    # Exercise the SDK integration imports used by actual model/tool execution.
    import redis.asyncio
    from openai import AsyncOpenAI
    from langgraph.graph import StateGraph
    from google.adk.models.lite_llm import LiteLlm

    print("Candidate dependency versions and SDK imports verified")


if __name__ == "__main__":
    main()
