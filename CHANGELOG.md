---
doc_id: fusion-plugins-ecosystem/changelog
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: changelog
title: {zh: "变更日志", en: "Changelog"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - pyproject.toml
  - README.md
---

# Changelog

All notable changes are tracked via git history; entries below summarize released versions. Versions follow `pyproject.toml` (`version`).

## 0.11.0 (current, per pyproject.toml)

- Marketplace backend API (issue #74): `GET /api/marketplace/plugins` listing (id, EN+CN descriptions, source module, category, tags, icon, install count, rating, official flag, stable/beta stage), `GET /api/marketplace/plugins/{id}` detail with permissions derived from plugin capabilities (file access / network / vRAM / long task / sub-agent) and connector tool risk, install / uninstall / update endpoints with idempotency and per-tenant install state (X-Tenant header)
- Publish/review queue: `POST /api/marketplace/submissions` with manifest validation (id/version/category/permissions/https source_repo), review queue listing + approve/reject; approved plugins appear in the marketplace listing as community entries
- Presentational metadata catalog `builtin/marketplace_catalog.json` merged over the registry + marketplace index (single source of truth unchanged); new `marketplace_api.py` module, 22 tests (785 total)

## 0.10.0

- Distribution support (issue #72): PyPI publishing via GitHub Actions trusted publishing (tag-driven, dry-run dispatch available) and a Homebrew formula auto-synced to `dahai80/homebrew-fusion-mlx` (pinned macOS arm64 wheels)
- Packaging fixes: `[tool.setuptools.package-data]` ships runtime files (`builtin/*.tool.json`, `web/`) in wheel + sdist; English PyPI metadata with README; dependency floor `fusion-core>=0.12.1`
- Both CLIs gain `--version`

## 0.9.1

- CI retrofit per fusion#2 checklist (issue #70): `concurrency` cancel-in-progress grouped by ref on both workflows; pip download cache keyed on `ci/requirements-ci.txt` + `pyproject.toml`; suite sharding and nightly tier assessed and declined (763 tests finish in about 60s)

## 0.9.0

- Declarative tool-manifest interchange format (issue #68): one static JSON file per plugin (`identifier` + `meta` + JSON-Schema `api[]` + `systemRole`), shipped JSON-Schema validator, deterministic manifest-to-MCP and MCP-to-manifest converters with identity round-trip tests; builtin `caveman_compress` migrated, validated at startup (fail-loudly)

## 0.8.x

- Docs-system v1.1 adoption: docs/ tree, contract validator, docs CI (PR #67)
- Global audit browser + used-by lookup in the extensions console (issue #64, #65)
- API server unknown paths return 404 fast instead of 200-then-hang (issue #62, #63)

## 0.7.x and earlier

Recent merged work (see `git log` for the full list):

- Global audit browser + used-by lookup in the extensions console (issue #64, #65)
- API server unknown paths return 404 fast instead of 200-then-hang (issue #62, #63)
- Shell registration category fixed platform -> governance (issue #60, #61)
- Extensions console four tabs (Connectors / Plugins·MCP / Skills / Sub-agents) with capabilities hub (issues #30, #41)
- Bundle install / OAuth / update / uninstall flows with scope grants (issue #42)
- Authorization audit events piped to the fusion-security JSONL pipeline (issue #43)
- Stable registry read contract registry/1.0 (issue #44)
- Config single-point resolution via fusion-core (issue #38), port single-point resolution (port-registry 8765 / 8795)
- External MCP connector framework with first-party packs (Feishu/DingTalk/WeCom/Mail/Calendar/Notion) and marketplace (issue #27)

## Earlier versions

See the repository git history (`git log --oneline`) for changes before 0.8.0.
