# Artifact publish pipeline (issue #76): build installable plugin/connector
# packages, validate registry.json schema v2 artifact pins, upload archives to
# the unified fusion-skills release, and emit the registry PR payload.
#
# Usage:
#   python scripts/publish_artifacts.py build --out dist/artifacts
#   python scripts/publish_artifacts.py validate --registry registry.json --dist dist/artifacts
#   python scripts/publish_artifacts.py upload --dist dist/artifacts \
#       --repo dahai80/fusion-skills --tag registry-artifacts
#
# The unified release on dahai80/fusion-skills hosts the archives (design doc
# plugin-distribution-design-1122: "a unified fusion-skills release if
# per-module assets are impractical"); registry.json pins url + sha256.

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("publish_artifacts")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RELEASE_TAG = "registry-artifacts"
DEFAULT_SKILLS_REPO = "dahai80/fusion-skills"

from fusion_plugins_ecosystem import __version__ as PACKAGE_VERSION  # noqa: E402

# entry id -> builder producing (archive_name, tar member files)
PLUGIN_ID = "caveman_compressor"
CONNECTOR_ID = "mlx_inference"
SKILL_ID = "web_search"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_caveman_plugin(out_dir: Path, version: str) -> dict[str, Any]:
    """Plugin package: PluginManifest JSON + declarative tool manifest.

    Payload is fully declarative — consumers register the manifest via the
    PluginManifest runtime (issue #68 interchange format).
    """
    from fusion_plugins_ecosystem.registry import PluginRegistry

    registry = PluginRegistry(desk=None)
    registry.register_builtin()
    manifest = registry.get("caveman_compress")
    if manifest is None:
        raise RuntimeError("builtin caveman_compress not registered")

    tool_json = (
        REPO_ROOT
        / "fusion_plugins_ecosystem"
        / "builtin"
        / "caveman_compress.tool.json"
    )
    if not tool_json.exists():
        raise RuntimeError(f"tool manifest missing: {tool_json}")

    archive = out_dir / f"{PLUGIN_ID}-v{version}-any-any.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        plugin_json = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)
        import io
        import time

        info = tarfile.TarInfo("plugin.json")
        payload = plugin_json.encode("utf-8")
        info.size = len(payload)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(payload))
        tar.add(tool_json, arcname="caveman_compress.tool.json")
    return _asset_record(PLUGIN_ID, archive, os="any", arch="any")


def build_mlx_inference_connector(out_dir: Path, version: str) -> dict[str, Any]:
    """Connector package: declarative REST connector document (rest_connector
    schema) bridging the local fusion-mlx OpenAI-compatible API.

    Installable via the console `POST /api/extensions/connectors` — validate,
    persist, bridge read channels.
    """
    connector_doc = {
        "id": CONNECTOR_ID,
        "name": "mlx-inference",
        "description": {
            "en": "Local MLX inference daemon (OpenAI-compatible API on 127.0.0.1:11434)",
            "zh": "本地 MLX 推理服务（OpenAI 兼容 API，127.0.0.1:11434）",
        },
        "kind": "rest",
        "base_url": "http://127.0.0.1:11434",
        "channels": [
            {
                "id": "list_models",
                "title": "List loaded models",
                "description": "GET /api/tags — loaded model inventory (read)",
                "method": "GET",
                "path": "/api/tags",
            },
            {
                "id": "chat",
                "title": "Chat completion",
                "description": "POST /v1/chat/completions — OpenAI-compatible inference (write)",
                "method": "POST",
                "path": "/v1/chat/completions",
            },
        ],
    }
    archive = out_dir / f"{CONNECTOR_ID}-v{version}-any-any.tar.gz"
    _write_tar(
        archive,
        {"connector.json": json.dumps(connector_doc, ensure_ascii=False, indent=2)},
    )
    return _asset_record(CONNECTOR_ID, archive, os="any", arch="any")


def _write_tar(archive: Path, files: dict[str, str]) -> None:
    import io
    import time

    with tarfile.open(archive, "w:gz") as tar:
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name)
            payload = content.encode("utf-8")
            info.size = len(payload)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(payload))


def _asset_record(entry_id: str, archive: Path, os: str, arch: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "file": archive.name,
        "path": str(archive),
        "version": PACKAGE_VERSION,
        "os": os,
        "arch": arch,
        "sha256": _sha256(archive),
        "size": archive.stat().st_size,
    }


def build_all(out_dir: Path) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    records = [
        build_caveman_plugin(out_dir, PACKAGE_VERSION),
        build_mlx_inference_connector(out_dir, PACKAGE_VERSION),
    ]
    (out_dir / "artifacts.json").write_text(
        json.dumps({"version": PACKAGE_VERSION, "assets": records}, indent=2),
        encoding="utf-8",
    )
    logger.info("built %d artifacts in %s", len(records), out_dir)
    return records


# ── registry validation (schema v2 artifact pins) ──

VALID_KINDS = {"app", "plugin", "connector", "skill"}
SHA_RE_LEN = 64


def validate_registry(
    registry: dict[str, Any], dist_dir: Path | None = None
) -> list[str]:
    problems: list[str] = []
    entries = registry.get("entries")
    if not isinstance(entries, list):
        return ["registry.entries must be a list"]
    seen_ids: set[str] = set()
    for entry in entries:
        eid = str(entry.get("id") or "")
        if not eid:
            problems.append("entry missing id")
            continue
        key = (eid, str(entry.get("kind")))
        if key in seen_ids:
            problems.append(f"duplicate id+kind {key}")
        seen_ids.add(key)
        if entry.get("kind") not in VALID_KINDS:
            problems.append(f"{eid}: invalid kind {entry.get('kind')!r}")
        if not entry.get("category"):
            problems.append(f"{eid}: missing category")
        if not isinstance(entry.get("description"), dict):
            problems.append(f"{eid}: description must be a zh/en object")
        for artifact in entry.get("artifacts") or []:
            url = str(artifact.get("url") or "")
            sha = str(artifact.get("sha256") or "")
            if not url.startswith("https://"):
                problems.append(f"{eid}: artifact url must be https ({url})")
            if len(sha) != SHA_RE_LEN or not all(
                c in "0123456789abcdef" for c in sha.lower()
            ):
                problems.append(f"{eid}: artifact sha256 must be 64 hex chars")
            if not artifact.get("size"):
                problems.append(f"{eid}: artifact size missing")
            if dist_dir is not None:
                local = dist_dir / Path(url).name
                if local.exists() and _sha256(local) != sha.lower():
                    problems.append(f"{eid}: sha256 mismatch for {local.name}")
        source_path = entry.get("source_path")
        if source_path is not None:
            if not str(source_path).startswith(
                ("skills/", "plugins/", "connectors/", "apps/")
            ):
                problems.append(f"{eid}: source_path must be an in-repo path")
    return problems


def apply_artifacts(
    registry: dict[str, Any],
    records: list[dict[str, Any]],
    release_tag: str,
    skills_repo: str,
) -> list[str]:
    """Mutate registry entries with artifact pins; return changed entry ids."""
    changed: list[str] = []
    by_id = {r["id"]: r for r in records}
    for entry in registry.get("entries") or []:
        record = by_id.get(str(entry.get("id")))
        if record is None:
            continue
        url = (
            f"https://github.com/{skills_repo}/releases/download/"
            f"{release_tag}/{record['file']}"
        )
        entry["artifacts"] = [
            {
                "os": record["os"],
                "arch": record["arch"],
                "url": url,
                "sha256": record["sha256"],
                "size": record["size"],
            }
        ]
        entry["version"] = record["version"]
        changed.append(entry["id"])
    return changed


def upload(dist_dir: Path, repo: str, tag: str) -> None:
    assets = sorted(dist_dir.glob("*.tar.gz"))
    if not assets:
        raise RuntimeError(f"no .tar.gz artifacts in {dist_dir}")
    subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            "gh",
            "release",
            "upload",
            tag,
            *[str(a) for a in assets],
            "--repo",
            repo,
            "--clobber",
        ],
        check=True,
    )
    logger.info("uploaded %d assets to %s@%s", len(assets), repo, tag)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="publish_artifacts")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build")
    p_build.add_argument("--out", default="dist/artifacts")

    p_val = sub.add_parser("validate")
    p_val.add_argument("--registry", required=True)
    p_val.add_argument("--dist", default=None)

    p_up = sub.add_parser("upload")
    p_up.add_argument("--dist", default="dist/artifacts")
    p_up.add_argument("--repo", default=DEFAULT_SKILLS_REPO)
    p_up.add_argument("--tag", default=DEFAULT_RELEASE_TAG)

    args = parser.parse_args()
    if args.cmd == "build":
        records = build_all(Path(args.out))
        for r in records:
            print(f"{r['file']}  sha256={r['sha256'][:12]}...  {r['size']} bytes")
        return 0
    if args.cmd == "validate":
        registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
        problems = validate_registry(registry, Path(args.dist) if args.dist else None)
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        print(f"validated: {len(problems)} problems")
        return 1 if problems else 0
    if args.cmd == "upload":
        upload(Path(args.dist), args.repo, args.tag)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
