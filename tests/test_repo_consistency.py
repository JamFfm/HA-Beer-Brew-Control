"""Statische Repo-Prüfungen (Design §13.1 ``test_repo_consistency.py``, AK 5, 6, 29–31).

Die Tests lesen nur Dateien und brauchen keine HA-Instanz.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
INTEGRATION = REPO / "custom_components" / "brausteuerung"
FRONTEND = INTEGRATION / "frontend"
CARD = FRONTEND / "brausteuerung-card.js"

JSON_FILES = (
    INTEGRATION / "manifest.json",
    INTEGRATION / "strings.json",
    INTEGRATION / "icons.json",
    INTEGRATION / "translations" / "en.json",
    INTEGRATION / "translations" / "de.json",
    REPO / "hacs.json",
    REPO / "package.json",
)

# Anzeigename und Repository der Integration (Veröffentlichung).
DISPLAY_NAME = "HA-Beer-Brew-Control"
REPO_URL = "https://github.com/JamFfm/HA-Beer-Brew-Control"

# Verzeichnisse, die nicht zum Ergebnis gehören (gitignored bzw. Pipeline-Ablage).
IGNORED_DIRS = frozenset(
    {
        ".agents",
        "node_modules",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        "htmlcov",
        "__pycache__",
        ".git",
    }
)
# Einzelne Cache-Dateien (Coverage-Datenbank, macOS-Ordnerinfo).
IGNORED_FILES = frozenset({".coverage", ".DS_Store"})


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten(data: Any, prefix: str = "") -> set[str]:
    """Liefert alle Blatt-Schlüsselpfade eines verschachtelten Dicts."""
    if not isinstance(data, dict):
        return {prefix}
    keys: set[str] = set()
    for key, value in data.items():
        keys |= _flatten(value, f"{prefix}.{key}" if prefix else key)
    return keys


def _python_sources() -> list[Path]:
    return sorted(INTEGRATION.glob("*.py"))


def _repo_files() -> list[Path]:
    result: list[Path] = []
    for path in REPO.rglob("*"):
        rel = path.relative_to(REPO)
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        if path.is_file():
            result.append(rel)
    return result


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_json_valid(path: Path) -> None:
    """Alle JSON-Dateien sind gültig und haben ein Objekt als Wurzel."""
    assert isinstance(_load(path), dict)


def test_translation_keys_identical() -> None:
    """strings.json, en.json und de.json haben identische Schlüsselmengen (AK 5)."""
    strings = _flatten(_load(INTEGRATION / "strings.json"))
    en = _flatten(_load(INTEGRATION / "translations" / "en.json"))
    de = _flatten(_load(INTEGRATION / "translations" / "de.json"))
    assert strings == en
    assert strings == de


def test_every_error_code_has_exception_translation() -> None:
    """Jeder Fehlercode aus BrauError/LogicError hat einen ``exceptions``-Schlüssel."""
    exceptions = set(_load(INTEGRATION / "strings.json")["exceptions"])
    pattern = re.compile(r"""(?:BrauError|LogicError)\(\s*["']([a-z_]+)["']""")
    codes: set[str] = set()
    for path in _python_sources():
        if path.name == "migration.py":
            # migration.py nutzt LogicError("invalid") nur intern (immer abgefangen).
            text = path.read_text(encoding="utf-8").replace('LogicError("invalid")', "")
        else:
            text = path.read_text(encoding="utf-8")
        codes |= set(pattern.findall(text))
    assert "internal_error" in codes
    assert codes, "keine Fehlercodes gefunden"
    assert codes <= exceptions, f"fehlende exceptions-Schlüssel: {sorted(codes - exceptions)}"
    for lang in ("en", "de"):
        translated = set(_load(INTEGRATION / "translations" / f"{lang}.json")["exceptions"])
        assert codes <= translated


def test_config_schema_present() -> None:
    """Die Integration ist nur per UI konfigurierbar (CONFIG_SCHEMA vorhanden)."""
    import custom_components.brausteuerung as integration

    assert integration.CONFIG_SCHEMA is not None


def test_manifest_fields() -> None:
    """Pflichtfelder für hassfest/HACS, keine Fremdpakete (S9)."""
    manifest = _load(INTEGRATION / "manifest.json")
    assert manifest["domain"] == "brausteuerung"
    assert manifest["name"] == DISPLAY_NAME
    assert manifest["config_flow"] is True
    assert manifest["single_config_entry"] is True
    assert manifest["integration_type"] == "service"
    assert manifest["iot_class"] == "calculated"
    assert manifest["codeowners"] == ["@JamFfm"]
    assert set(manifest["dependencies"]) == {"frontend", "http", "websocket_api"}
    assert manifest["documentation"] == REPO_URL
    assert manifest["issue_tracker"] == f"{REPO_URL}/issues"
    assert manifest["requirements"] == []
    # hassfest: domain, name, danach alphabetisch.
    keys = list(manifest)
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


def test_hacs_json() -> None:
    hacs = _load(REPO / "hacs.json")
    assert hacs == {"name": DISPLAY_NAME, "homeassistant": "2026.6.0", "render_readme": True}


@pytest.mark.parametrize("needle", ["GITHUB" + "_OWNER", "COPYRIGHT" + "_HOLDER"])
def test_no_placeholders_left(needle: str) -> None:
    """Platzhalter für GitHub-Owner und Copyright sind überall ersetzt (Caches ausgenommen)."""
    # Zusammengesetzt, damit diese Testdatei nicht selbst als Treffer zählt.
    raw = needle.encode()
    offenders = [
        str(rel)
        for rel in _repo_files()
        if rel.name not in IGNORED_FILES and raw in (REPO / rel).read_bytes()
    ]
    assert offenders == []


def test_license_copyright() -> None:
    assert "Copyright (c) 2026 JamFfm" in (REPO / "LICENSE").read_text(encoding="utf-8")


def test_versions_match() -> None:
    """manifest = package.json = Karten-VERSION (AK 6)."""
    manifest_version = _load(INTEGRATION / "manifest.json")["version"]
    package_version = _load(REPO / "package.json")["version"]
    match = re.search(r'const VERSION = "([^"]+)";', CARD.read_text(encoding="utf-8"))
    assert match is not None
    assert manifest_version == package_version == match.group(1) == "3.0.3"


@pytest.mark.parametrize(
    "pattern",
    [
        r"\beval\(",
        r"\bexec\(",
        r"aiohttp\.ClientSession",
        r"\brequests\b",
        r"urllib\.request",
        r"\bsocket\b",
        r"https?://",
    ],
)
def test_no_unsafe_code_or_network(pattern: str) -> None:
    """Kein eval/exec, keine Netzwerkbibliotheken, keine URLs im Code (AK 29)."""
    regex = re.compile(pattern)
    offenders = [
        path.name for path in _python_sources() if regex.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_urls_only_in_manifest() -> None:
    """http(s)-Literale gibt es in der Integration nur im Manifest (AK 29)."""
    regex = re.compile(r"https?://")
    offenders = [
        str(path.relative_to(INTEGRATION))
        for path in INTEGRATION.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".json", ".yaml", ".js"}
        and path.name != "manifest.json"
        and regex.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.mark.parametrize("needle", ["input_text.brau_", "automation.brausteuerung_", "255"])
def test_card_without_legacy_references(needle: str) -> None:
    """Die Karte kennt keine Helfer, Automationen und keine 255-Grenze mehr (AK 30)."""
    assert needle not in CARD.read_text(encoding="utf-8")


def test_repo_structure() -> None:
    """Erwartete Repo-Struktur (AK 31)."""
    required = [
        "README.md",
        "LICENSE",
        "hacs.json",
        "package.json",
        "pyproject.toml",
        "requirements_test.txt",
        ".gitignore",
        ".github/workflows/validate.yml",
        "legacy/configuration.yaml",
        "legacy/automations.yaml",
        "custom_components/brausteuerung/__init__.py",
        "custom_components/brausteuerung/manifest.json",
        "custom_components/brausteuerung/strings.json",
        "custom_components/brausteuerung/services.yaml",
        "custom_components/brausteuerung/icons.json",
        "custom_components/brausteuerung/translations/de.json",
        "custom_components/brausteuerung/translations/en.json",
        "custom_components/brausteuerung/brand/icon.png",
        "custom_components/brausteuerung/frontend/brausteuerung-card.js",
        "custom_components/brausteuerung/frontend/brausteuerung-logic.js",
    ]
    missing = [name for name in required if not (REPO / name).is_file()]
    assert missing == []
    assert not (REPO / "www").exists()
    assert not (REPO / "configuration.yaml").exists()
    assert not (REPO / "automations.yaml").exists()
    # HACS liefert nur custom_components/brausteuerung aus; kein Namespace-__init__.
    assert not (REPO / "custom_components" / "__init__.py").exists()


def test_brand_icon_is_256_png() -> None:
    data = (INTEGRATION / "brand" / "icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(data[16:20]) == 256
    assert int.from_bytes(data[20:24]) == 256


def test_no_additional_markdown() -> None:
    """Außer README.md gibt es keine .md-Dateien (F5, AK 31)."""
    markdown = [str(p) for p in _repo_files() if p.suffix.lower() == ".md"]
    assert markdown == ["README.md"]


def test_workflow_validates_hacs_and_hassfest() -> None:
    text = (REPO / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    for needle in (
        "hacs/action@main",
        "category: integration",
        "actions/checkout@v7",
        "home-assistant/actions/hassfest@master",
        "workflow_dispatch",
        "schedule",
    ):
        assert needle in text
