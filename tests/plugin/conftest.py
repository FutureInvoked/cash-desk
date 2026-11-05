"""Fixtures for the Hermes half.

The plugin folder is loaded the way Hermes loads it (a package whose search path is the
folder), so the lazy relative imports in ``__init__.py`` resolve. ``plugins.plugin_storage``,
``hermes_cli.config`` and ``agent.secret_scope`` are replaced by small fakes so the tests run
outside Hermes and every ledger lives under the test's temporary directory.
"""

from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from tests.plugin.support import ROOT, FakeCtx


@pytest.fixture
def approvals_mode():
    return ["manual"]


@pytest.fixture
def hermes_fakes(tmp_path, monkeypatch, approvals_mode):
    monkeypatch.setenv("CASHDESK_TODAY", "2026-11-10")  # the demo's own day, so dates stay as written
    plugins = types.ModuleType("plugins")
    plugins.__path__ = []  # a package, so "plugins.plugin_storage" resolves
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: str(tmp_path / "plugin-data" / name)
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"approvals": {"mode": approvals_mode[0]}}
    agent = types.ModuleType("agent")
    agent.__path__ = []
    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.get_secret = lambda name: "fake-secret" if name == "CASHDESK_SMTP_PASSWORD" else None
    for name, module in (("plugins", plugins), ("plugins.plugin_storage", storage), ("hermes_cli", hermes_cli),
                         ("hermes_cli.config", config), ("agent", agent), ("agent.secret_scope", secret_scope)):
        monkeypatch.setitem(sys.modules, name, module)
    return tmp_path


@pytest.fixture
def plugin(hermes_fakes):
    name = "cashdesk_plugin_under_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        ctx = FakeCtx(config={"sender_name": "Example Studio s.r.o.", "send_mode": "eml", "smtp_from": "billing@example.invalid"})
        module.register(ctx)
        yield module, ctx
    finally:
        for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
            sys.modules.pop(key, None)
