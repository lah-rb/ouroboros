"""Module-import smoke: every production module must import.

The cheapest possible net for the deleted-symbol class of breakage: on
2026-07-16 two methods were deleted from the TB agents while their call
sites survived, and nothing failed until runtime — adapter tests import
concrete symbols inside test functions (to dodge heavy deps), so
module-level breakage slips through. This walks the production packages
and imports each module once; heavy external harness deps are skip-guarded
by module, not silently swallowed.
"""

import importlib
import pkgutil

import pytest

# Modules whose imports require external harness packages that are absent
# in the repo venv (present only in their runtime environments).
_OPTIONAL_DEP_PREFIXES = {
    "adapters.tb.agent": "terminal_bench",
    "adapters.tb.harbor_agent": "harbor.agents.base",
    "adapters.tau.env": "tau_bench",
    "adapters.tau.episode": "tau_bench",
    "adapters.tau.runner": "tau_bench",
    "adapters.swe.evaluate": "swebench",
}


def _walk(package_name: str):
    pkg = importlib.import_module(package_name)
    for m in pkgutil.walk_packages(pkg.__path__, prefix=package_name + "."):
        yield m.name


def _importable(name: str):
    dep = _OPTIONAL_DEP_PREFIXES.get(name)
    if dep is not None and importlib.util.find_spec(dep) is None:
        pytest.skip(f"{name} needs optional harness dep {dep!r}")
    importlib.import_module(name)


@pytest.mark.parametrize("module", sorted(_walk("agent")))
def test_agent_module_imports(module):
    _importable(module)


@pytest.mark.parametrize("module", sorted(_walk("adapters")))
def test_adapter_module_imports(module):
    _importable(module)
