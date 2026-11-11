"""Import-Robustheit (Bugfix gegen fehlende Moduldateien, siehe CHANGELOG).

v3.0.0 brach in HA mit "cannot import name 'validation' from partially initialized
module … (circular import)" ab, weil im veröffentlichten Repository Dateien fehlten.
Die Tests importieren in frischen Interpretern in allen Reihenfolgen (auch parallel),
simulieren fehlende Dateien und fehlendes ``probatio`` und laden den Config Flow über
den HA-Loader ohne vorheriges Setup.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

REPO = Path(__file__).resolve().parent.parent
INTEGRATION = REPO / "custom_components" / "brausteuerung"
PKG = "custom_components.brausteuerung"
MODULE_NAMES = sorted(p.stem for p in INTEGRATION.glob("*.py") if p.stem != "__init__")


def _run(code: str, root: Path = REPO) -> subprocess.CompletedProcess[str]:
    """Führt ``code`` in einem frischen Interpreter mit ``root`` im Suchpfad aus."""
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(  # noqa: S603 - fester Interpreter, eigener Code
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_required_modules_match_files() -> None:
    """Die Vollständigkeitsliste in __init__.py deckt genau alle Untermodule ab."""
    import custom_components.brausteuerung as integration

    assert sorted(integration._REQUIRED_MODULES) == MODULE_NAMES


@pytest.mark.parametrize("first", ["", *MODULE_NAMES])
def test_fresh_import_each_module_first(first: str) -> None:
    """Jedes Modul lässt sich als ERSTES Modul importieren, danach alle anderen."""
    target = f"{PKG}.{first}" if first else PKG
    result = _run(
        f"""
        import importlib
        importlib.import_module({target!r})
        for name in {MODULE_NAMES!r}:
            importlib.import_module({PKG!r} + "." + name)
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize(
    ("first", "second"),
    [("config_flow", ""), ("config_flow", "services"), ("config_flow", "websocket")],
)
def test_parallel_import_from_two_threads(first: str, second: str) -> None:
    """Paralleler Import aus zwei Threads (wie HAs Import-Executor) bricht nicht ab."""
    result = _run(
        f"""
        import importlib, threading
        names = [{PKG!r} + ".{first}", {PKG!r} + ("." + {second!r} if {second!r} else "")]
        barrier = threading.Barrier(2)
        errors = []
        def worker(name):
            barrier.wait()
            try:
                importlib.import_module(name)
            except BaseException as err:
                errors.append(repr(err))
        threads = [threading.Thread(target=worker, args=(n,)) for n in names]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors, errors
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize("missing", ["validation.py", "const.py", "config_flow.py"])
def test_missing_file_gives_clear_error(tmp_path: Path, missing: str) -> None:
    """Fehlt eine Datei (Fall v3.0.0), nennt der Fehler sie statt 'circular import'."""
    target = tmp_path / "custom_components" / "brausteuerung"
    shutil.copytree(INTEGRATION, target, ignore=shutil.ignore_patterns("__pycache__"))
    (target / missing).unlink()
    result = _run(f"import {PKG}.config_flow", root=tmp_path)
    assert result.returncode != 0
    assert "incompletely installed" in result.stderr
    assert missing in result.stderr
    assert "circular import" not in result.stderr


def test_without_probatio_voluptuous_is_used() -> None:
    """HA 2026.6–2026.8 ohne ``probatio``: Fallback auf ``voluptuous``."""
    result = _run(
        f"""
        import importlib, sys
        import {PKG}.config_flow  # HA selbst braucht probatio ab 2026.9
        sys.modules["probatio"] = None  # ab jetzt: import probatio -> ImportError
        del sys.modules[{PKG!r} + ".validation"]
        validation = importlib.import_module({PKG!r} + ".validation")
        # Ab HA 2026.9 ist ``voluptuous`` ein Shim aus probatio; entscheidend ist der Fallback.
        print(validation.vol is sys.modules["voluptuous"])
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


async def test_flow_via_loader_with_fresh_import(hass: HomeAssistant, mock_frontend: None) -> None:
    """HA-Loader lädt config_flow (Executor-Import) ohne vorheriges Setup und Import."""
    saved = {k: v for k, v in sys.modules.items() if k == PKG or k.startswith(f"{PKG}.")}
    for name in saved:
        del sys.modules[name]
    try:
        result = await hass.config_entries.flow.async_init(
            "brausteuerung", context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        assert f"{PKG}.config_flow" in sys.modules
        hass.config_entries.flow.async_abort(result["flow_id"])
    finally:
        # Ursprüngliche Module wiederherstellen, damit andere Tests dieselben Klassen sehen.
        for name in [k for k in sys.modules if k == PKG or k.startswith(f"{PKG}.")]:
            del sys.modules[name]
        sys.modules.update(saved)
