"""Directory-plugin adapter for Hermes Agent.

Hermes loads a cloned plugin repository as ``hermes_plugins.<name>`` while
the wheel exposes the regular ``hermes_galileo`` package. Keep this adapter
small so both installation modes execute the same implementation.
"""

try:
    from .hermes_galileo import force_flush, health_snapshot, initialize, register
except ImportError:  # Imported by test tooling as a top-level ``__init__`` module.
    from hermes_galileo import force_flush, health_snapshot, initialize, register

__all__ = ["force_flush", "health_snapshot", "initialize", "register"]
