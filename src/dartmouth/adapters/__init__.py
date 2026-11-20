"""Adapter registry.

`adapter:` in the config accepts a registered name, "package.module:Class", or "path/to/file.py:Class"
(a file outside the package, resolved relative to the config file).
"""

from __future__ import annotations

import importlib
from typing import Any

from .base import Adapter, AdapterError, Reply, TurnContext

REGISTRY: dict[str, str] = {
    "dummy": "dartmouth.adapters.dummy:DummyAdapter",
    "claude_code": "dartmouth.adapters.claude_code:ClaudeCodeAdapter",
    "gemini_cli": "dartmouth.adapters.gemini_cli:GeminiCLIAdapter",
    "antigravity_cli": "dartmouth.adapters.antigravity_cli:AntigravityCLIAdapter",
    "gemini_api": "dartmouth.adapters.gemini_api:GeminiAPIAdapter",
}


def _load_from_file(path: str, name: str):
    import importlib.util
    import sys
    from pathlib import Path

    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"adapter file not found: {p}")
    mod_name = f"dartmouth_ext_{p.stem}_{abs(hash(str(p)))}"
    if mod_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(mod_name, p)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    return getattr(sys.modules[mod_name], name)


def load_adapter_class(spec: str) -> type[Adapter]:
    target = REGISTRY.get(spec, spec)
    if ":" not in target:
        raise ValueError(f"unknown adapter '{spec}' (known: {', '.join(REGISTRY)})")
    mod, cls = target.rsplit(":", 1)
    klass = _load_from_file(mod, cls) if mod.endswith(".py") else getattr(importlib.import_module(mod), cls)
    if not (isinstance(klass, type) and issubclass(klass, Adapter)):
        raise TypeError(f"{target} is not an Adapter")
    return klass


def create_adapter(name: str, spec: str, role: str, timeout_s: float, options: dict[str, Any]) -> Adapter:
    return load_adapter_class(spec)(name, role, timeout_s, options)


__all__ = ["Adapter", "AdapterError", "Reply", "TurnContext", "REGISTRY", "create_adapter", "load_adapter_class"]
