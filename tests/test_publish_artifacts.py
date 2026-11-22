# Artifact publish pipeline tests (issue #76): package builders produce real
# archives with pinned payloads; registry validator enforces schema v2 pins.

import json
import tarfile
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from publish_artifacts import (  # noqa: E402
    apply_artifacts,
    build_all,
    validate_registry,
)

from fusion_plugins_ecosystem import __version__  # noqa: E402


@pytest.fixture()
def dist(tmp_path):
    return build_all(tmp_path / "dist")


class TestBuilders:
    def test_build_all_version_tracks_package(self, dist):
        assert len(dist) == 2
        for r in dist:
            assert r["version"] == __version__
            assert len(r["sha256"]) == 64
            assert r["size"] > 0
        assert all(Path(r["path"]).exists() for r in dist)

    def test_caveman_archive_payload(self, dist, tmp_path):
        record = next(r for r in dist if r["id"] == "caveman_compressor")
        with tarfile.open(record["path"]) as tar:
            names = tar.getnames()
            assert "plugin.json" in names
            assert "caveman_compress.tool.json" in names
            manifest = json.load(tar.extractfile("plugin.json"))
        assert manifest["id"] == "caveman_compress"
        assert manifest["version"] == __version__
        # entry point serialized as dotted path, not callable repr
        assert isinstance(manifest["entry_point"], str)

    def test_connector_archive_payload(self, dist):
        record = next(r for r in dist if r["id"] == "mlx_inference")
        with tarfile.open(record["path"]) as tar:
            doc = json.load(tar.extractfile("connector.json"))
        assert doc["id"] == "mlx_inference"
        assert doc["kind"] == "rest"
        channel_ids = [c["id"] for c in doc["channels"]]
        assert "list_models" in channel_ids and "chat" in channel_ids

    def test_sha256_matches_actual_file(self, dist):
        import hashlib

        for r in dist:
            h = hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest()
            assert h == r["sha256"]


def _registry():
    return {
        "schemaVersion": 2,
        "version": 1,
        "entries": [
            {
                "id": "caveman_compressor",
                "kind": "plugin",
                "category": "context",
                "source": "fusion-plugins-ecosystem",
                "description": {"zh": "压缩", "en": "compress"},
                "doc": "plugins/caveman_compressor.md",
            },
            {
                "id": "web_search",
                "kind": "skill",
                "category": "web",
                "source": "fusion-plugins-ecosystem",
                "description": {"zh": "检索", "en": "search"},
                "doc": "skills/web_search.md",
            },
        ],
    }


class TestValidator:
    def test_clean_registry_passes(self):
        assert validate_registry(_registry()) == []

    def test_duplicate_id_kind_rejected(self):
        reg = _registry()
        reg["entries"].append(dict(reg["entries"][0]))
        problems = validate_registry(reg)
        assert any("duplicate" in p for p in problems)

    def test_bad_artifact_pins_rejected(self):
        reg = _registry()
        reg["entries"][0]["artifacts"] = [
            {
                "os": "any",
                "arch": "any",
                "url": "http://x/y.tar.gz",
                "sha256": "xyz",
                "size": 1,
            }
        ]
        problems = validate_registry(reg)
        assert any("https" in p for p in problems)
        assert any("sha256" in p for p in problems)

    def test_artifact_sha_verified_against_local_dist(self, dist, tmp_path):
        reg = _registry()
        apply_artifacts(
            reg,
            dist,
            release_tag="registry-artifacts",
            skills_repo="dahai80/fusion-skills",
        )
        dist_dir = tmp_path / "d"
        dist_dir.mkdir()
        for r in dist:
            import shutil

            shutil.copy(r["path"], dist_dir / r["file"])
        assert validate_registry(reg, dist_dir) == []
        # corrupt the pin → mismatch detected
        reg["entries"][0]["artifacts"][0]["sha256"] = "0" * 64
        problems = validate_registry(reg, dist_dir)
        assert any("mismatch" in p for p in problems)


class TestApplyArtifacts:
    def test_pins_written_and_ids_returned(self, dist):
        reg = _registry()
        changed = apply_artifacts(
            reg,
            dist,
            release_tag="registry-artifacts",
            skills_repo="dahai80/fusion-skills",
        )
        assert "caveman_compressor" in changed
        entry = next(e for e in reg["entries"] if e["id"] == "caveman_compressor")
        artifact = entry["artifacts"][0]
        assert artifact["url"].startswith(
            "https://github.com/dahai80/fusion-skills/releases/download/registry-artifacts/"
        )
        assert artifact["url"].endswith(".tar.gz")
        assert entry["version"] == __version__
        # unrelated entries untouched
        assert "artifacts" not in next(
            e for e in reg["entries"] if e["id"] == "web_search"
        )
