"""Shared helpers for the plugin-half tests: a fake Hermes plugin context and the demo loader."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demo"
AS_OF = "2026-11-10"


class FakeCtx:
    def __init__(self, config: dict | None = None):
        self.tools: dict[str, dict] = {}
        self.hooks: dict[str, list] = {}
        self.commands: dict[str, object] = {}
        self.blueprints: list = []
        self.config = dict(config or {})
        self.cron = None  # set to any object to simulate a scheduled run

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kw}

    def register_hook(self, event, callback, **kw):
        self.hooks.setdefault(event, []).append(callback)
        self.hook_options = getattr(self, "hook_options", {})
        self.hook_options[event] = kw

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_automation_blueprint(self, key, **kw):
        self.blueprints.append((key, kw))

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def current_cron_execution(self):
        return self.cron

    # test helpers
    def call(self, tool: str, **args) -> dict:
        raw = self.tools[tool]["handler"](dict(args), task_id="t-1")
        assert isinstance(raw, str), f"{tool} must return a JSON string"
        return json.loads(raw)

    def gate(self, tool: str, args: dict | None = None):
        (hook,) = self.hooks["pre_tool_call"]
        return hook(tool, args or {}, task_id="t-1")

    def command(self, raw: str) -> str:
        return self.commands["cashdesk"](raw)


def load_demo(ctx: FakeCtx) -> None:
    """Import the fake customers, invoices and both bank days, then match."""
    assert ctx.call("cashdesk_import", kind="customers", path=str(DEMO / "customers.csv"))["ok"]
    assert ctx.call("cashdesk_import", kind="invoices", path=str(DEMO / "invoices.csv"), source="generic")["ok"]
    assert ctx.call("cashdesk_import", kind="bank", path=str(DEMO / "bank_day1.csv"))["ok"]
    assert ctx.call("cashdesk_import", kind="bank", path=str(DEMO / "bank_day2.csv"))["ok"]
    assert ctx.call("cashdesk_match", as_of=AS_OF)["ok"]
