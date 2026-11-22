---
doc_id: fusion-plugins-ecosystem/distribution
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: architecture
title: {zh: "制品发布管线", en: "Artifact publish pipeline"}
audience: developer
status: draft
version: 0.12.0
updated: 2026-11-22
owner: "@dahai"
source_of_truth:
  - scripts/publish_artifacts.py
  - .github/workflows/publish-artifacts.yml
---

# Artifact publish pipeline (issue #76)

The distribution model is settled by
`architecture/plugin-distribution-design-1122.md`: the marketplace publishes
(metadata + artifacts + checksums), consumers install (resolve, verify sha256,
unpack, register). The catalog source of truth is the static `registry.json`
(schema v2) in [dahai80/fusion-skills](https://github.com/dahai80/fusion-skills).

This repo owns the **publish pipeline**: it turns ecosystem-owned entries into
installable, checksum-pinned packages and back-fills the registry.

## Package payloads

| Entry | Kind | Payload | Consumer path |
|---|---|---|---|
| `caveman_compressor` | plugin | `plugin.json` (PluginManifest, issue #68 interchange) + `caveman_compress.tool.json` | register via the PluginManifest runtime |
| `mlx_inference` | connector | `connector.json` (declarative REST connector doc) | console `POST /api/extensions/connectors` → validate → persist → bridge |
| `web_search` | skill | in-repo source `skills/web_search/` in fusion-skills (`SKILL.md` + files) | hosts fetch the in-repo path and register the skill |

Skill source content ownership: **fusion-skills owns skill content** (it is the
content/mapping repo per the design doc); fusion-plugins-ecosystem owns the
catalog tooling, packaging, and artifact pins. cowork / fusion-code are
consumers, not content owners.

## Ingestion contract ("who runs what, which PR, which checks")

1. **Source change lands** in this repo (plugin/connector payload, or a skill
   PR in fusion-skills under `skills/<id>/`).
2. **Build** — `python scripts/publish_artifacts.py build --out dist/artifacts`
   produces `<id>-v<version>-<os>-<arch>.tar.gz` packages plus
   `artifacts.json` (sha256 + size per asset). Package version always equals
   the package `__version__`. Build **once** and reuse the same dist for
   upload AND registry pins — archives are gzip (mtime in header), so a
   rebuild changes the sha256 and invalidates pins.
3. **Upload** — assets go to the unified release
   `dahai80/fusion-skills@registry-artifacts`:
   `python scripts/publish_artifacts.py upload --dist dist/artifacts`.
   CI (`.github/workflows/publish-artifacts.yml`, manual dispatch) does
   build → validate → upload when `FUSION_SKILLS_TOKEN` is configured.
4. **Registry PR** — a PR against `dahai80/fusion-skills` updates the entry:
   `artifacts: [{os, arch, url, sha256, size}]` + `version` (and `source_path`
   for in-repo skills). Until automation lands, run
   `apply_artifacts()` (see `scripts/publish_artifacts.py`) and open the PR
   manually against the checklist below.
5. **Checks on the PR** — `validate` must pass:
   id+kind unique, category present, `{zh,en}` description, artifact URLs
   https-only, sha256 64-hex (verified against the local dist when provided),
   size present, `source_path` in-repo. Run:
   `python scripts/publish_artifacts.py validate --registry registry.json --dist dist/artifacts`
6. **Merge = published.** Consumers (fusion-console, see
   dahai80/fusion-console#107) pick the entry up on the next registry refresh.

### PR checklist

- [ ] `validate` exits 0 against the updated `registry.json`
- [ ] every pinned URL resolves on the `registry-artifacts` release
- [ ] install docs updated (no longer "managed inside fusion-web-shell" only)
- [ ] version bumped on every changed entry

## Out of scope here

- Console-side consumption and checksum enforcement — dahai80/fusion-console#107.
- Site marketplace UI — dahai80/fusion-site.
