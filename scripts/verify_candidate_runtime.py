"""Check the built candidate without starting servers or contacting providers."""
from email.parser import BytesParser
import hashlib
from importlib.metadata import distributions, version
from pathlib import Path
import sysconfig
import tomllib


# Fingerprints from the official setuptools-84.2.1 wheel on PyPI, verified against
# its published SHA256 ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2.
# Metadata paths use normalized underscores, unlike the distribution names.
VENDORED_PACKAGES = (
    ("jaraco.context", "6.1.0", "jaraco_context-6.1.0.dist-info/METADATA",
     "jaraco/context", 1, "4cc349977003d1ab979421149175d016568b4dcd5c0c1b68295744ffe08e8238"),
    ("wheel", "0.46.3", "wheel-0.46.3.dist-info/METADATA",
     "wheel", 14, "5571c0eff39f10cb6fe8de696bcc6b1628bb2f5bbc9bbf5c4b42852c8b094515"),
)


def verify_vendor_package(vendor, name, expected_version, metadata_path, source_path, expected_count, expected_digest):
    """Require explicit metadata and exact patched Python source evidence."""
    try:
        metadata = BytesParser().parsebytes((vendor / metadata_path).read_bytes())
        if metadata.get_all("Name") != [name] or metadata.get_all("Version") != [expected_version]:
            raise ValueError("unexpected vendored metadata")
        source = vendor / source_path
        files = sorted(source.rglob("*.py"))
        if len(files) != expected_count or not files:
            raise ValueError("missing or unexpected vendored sources")
        digest = hashlib.sha256()
        for path in files:
            digest.update(path.relative_to(source).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
        if digest.hexdigest() != expected_digest:
            raise ValueError("vendored source fingerprint mismatch")
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Vendored dependency evidence failed for {name}") from error


def verify_setuptools_vendor(setuptools_root):
    vendor = setuptools_root / "_vendor"
    for package in VENDORED_PACKAGES:
        verify_vendor_package(vendor, *package)


def require_single_distribution(name, expected_version):
    """Reject shadowed/duplicate metadata instead of trusting first-match lookup."""
    normalized = name.lower().replace("-", "_").replace(".", "_")
    matches = [
        item for item in distributions()
        if (item.metadata.get("Name") or "").lower().replace("-", "_").replace(".", "_") == normalized
    ]
    details = [
        f"{item.metadata.get('Name')}=={item.version} at {item.locate_file('')}"
        for item in matches
    ]
    expected_root = Path(sysconfig.get_paths()["purelib"]).resolve()
    if (
        len(matches) != 1
        or matches[0].version != expected_version
        or Path(matches[0].locate_file("")).resolve() != expected_root
    ):
        raise RuntimeError(
            f"Expected one {name}=={expected_version} in {expected_root}; "
            f"found: {details or 'none'}"
        )
    return matches[0]


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
        "mako", "litellm", "google-adk", "google-genai",
        "pydantic", "pydantic-settings",
    ):
        expected = requirements[package]
        if declared.get(package, expected) != expected or version(package) != expected:
            raise RuntimeError(f"Candidate dependency mismatch for {package}")

    setuptools_dist = require_single_distribution("setuptools", requirements["setuptools"])
    setuptools_root = Path(setuptools_dist.locate_file("setuptools"))
    verify_setuptools_vendor(setuptools_root)
    require_single_distribution("wheel", requirements["wheel"])
    require_single_distribution("packaging", requirements["packaging"])

    # Exercise the SDK integration imports used by actual model/tool execution.
    import redis.asyncio
    from openai import AsyncOpenAI
    from langgraph.graph import StateGraph
    from google.adk.models.lite_llm import LiteLlm

    print("Candidate versions, vendored source evidence and SDK imports verified")


if __name__ == "__main__":
    main()
