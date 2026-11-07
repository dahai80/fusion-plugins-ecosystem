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

## 0.8.0 (current, per pyproject.toml)

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
