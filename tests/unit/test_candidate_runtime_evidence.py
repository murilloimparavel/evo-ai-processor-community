"""Vendored runtime evidence must fail closed without importing SDKs or a server."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

path = Path(__file__).resolve().parents[2] / 'scripts/verify_candidate_runtime.py'
spec = importlib.util.spec_from_file_location('candidate_evidence', path)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class VendorEvidenceTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.vendor = Path(folder.name)
        metadata = self.vendor / 'fixture-6.1.0.dist-info/METADATA'
        metadata.parent.mkdir()
        metadata.write_text('Name: fixture\nVersion: 6.1.0\n')
        self.source = self.vendor / 'fixture/__init__.py'
        self.source.parent.mkdir()
        self.source.write_bytes(b'patched source\n')
        self.expected = hashlib.sha256(b'__init__.py\0patched source\n\0').hexdigest()

    def verify(self):
        verifier.verify_vendor_package(self.vendor, 'fixture', '6.1.0',
            'fixture-6.1.0.dist-info/METADATA', 'fixture', 1, self.expected)

    def test_complete_patched_evidence_passes(self):
        self.verify()

    def test_empty_vendor_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, 'evidence failed'):
                verifier.verify_setuptools_vendor(Path(folder))

    def test_missing_metadata_fails(self):
        (self.vendor / 'fixture-6.1.0.dist-info/METADATA').unlink()
        with self.assertRaises(RuntimeError):
            self.verify()

    def test_old_or_duplicate_version_metadata_fails(self):
        metadata = self.vendor / 'fixture-6.1.0.dist-info/METADATA'
        for value in ('5.3.0', '6.1.0\nVersion: 5.3.0'):
            metadata.write_text(f'Name: fixture\nVersion: {value}\n')
            with self.assertRaises(RuntimeError):
                self.verify()

    def test_missing_source_fails(self):
        self.source.unlink()
        with self.assertRaises(RuntimeError):
            self.verify()

    def test_claimed_patched_version_with_old_source_fails(self):
        self.source.write_bytes(b'old vulnerable source\n')
        with self.assertRaises(RuntimeError):
            self.verify()

    def test_extra_old_source_fails(self):
        (self.source.parent / 'old.py').write_text('old code')
        with self.assertRaises(RuntimeError):
            self.verify()

    def test_single_expected_distribution_in_runtime_site_packages_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dist = SimpleNamespace(
                metadata={"Name": "setuptools"}, version="84.2.1",
                locate_file=lambda _: root,
            )
            with patch.object(verifier, "distributions", return_value=[dist]), patch.object(
                verifier.sysconfig, "get_paths", return_value={"purelib": str(root)}
            ):
                self.assertIs(verifier.require_single_distribution("setuptools", "84.2.1"), dist)

    def test_duplicate_or_shadowed_distribution_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            good = SimpleNamespace(
                metadata={"Name": "setuptools"}, version="84.2.1",
                locate_file=lambda _: root,
            )
            shadow = SimpleNamespace(
                metadata={"Name": "setuptools"}, version="79.0.1",
                locate_file=lambda _: Path("/usr/lib/python3/dist-packages"),
            )
            with patch.object(verifier, "distributions", return_value=[good, shadow]), patch.object(
                verifier.sysconfig, "get_paths", return_value={"purelib": str(root)}
            ):
                with self.assertRaisesRegex(RuntimeError, "79.0.1"):
                    verifier.require_single_distribution("setuptools", "84.2.1")

    def test_stale_wheel_metadata_reports_version_and_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stale = SimpleNamespace(
                metadata={"Name": "wheel"}, version="0.45.1",
                locate_file=lambda _: root,
            )
            with patch.object(verifier, "distributions", return_value=[stale]), patch.object(
                verifier.sysconfig, "get_paths", return_value={"purelib": str(root)}
            ):
                with self.assertRaisesRegex(RuntimeError, "wheel==0.45.1"):
                    verifier.require_single_distribution("wheel", "0.48.0")

    def test_duplicate_packaging_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidates = [
                SimpleNamespace(
                    metadata={"Name": "packaging"}, version=version,
                    locate_file=lambda _: root,
                )
                for version in ("25.0", "26.3")
            ]
            with patch.object(verifier, "distributions", return_value=candidates), patch.object(
                verifier.sysconfig, "get_paths", return_value={"purelib": str(root)}
            ):
                with self.assertRaisesRegex(RuntimeError, "packaging==26.3"):
                    verifier.require_single_distribution("packaging", "25.0")
