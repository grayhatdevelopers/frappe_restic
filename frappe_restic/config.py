"""Deployment identity shared by runtime and standalone recovery commands."""
import os
import re
from pathlib import Path


def namespace() -> str:
    """Keep repository tags and durable lock names stable across app upgrades."""
    value = os.environ.get("RESTIC_NAMESPACE", "frappe")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value):
        raise ValueError("RESTIC_NAMESPACE must be a lowercase identifier")
    return value


def bench_root() -> Path:
    """Resolve the deployment's bench without depending on an ERP application."""
    return Path(os.environ.get("FRAPPE_BENCH_ROOT", "/home/frappe/frappe-bench"))
