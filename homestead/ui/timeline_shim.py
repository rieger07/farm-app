"""Compatibility shim for streamlit-vis-timeline and headless AppTest fallback."""

import importlib.util
import os
import sys
import types
import streamlit as st

# Provide a lightweight stdlib shim for pkg_resources (removed in setuptools>=82),
# which streamlit-vis-timeline imports to locate its frontend/build directory.
if "pkg_resources" not in sys.modules:
    try:
        import pkg_resources  # type: ignore # noqa: F401
    except ModuleNotFoundError:
        _pkg_res = types.ModuleType("pkg_resources")

        def _resource_filename(
            package_or_requirement: str, resource_name: str
        ) -> str:
            spec = importlib.util.find_spec(package_or_requirement)
            if spec and spec.origin:
                return os.path.join(os.path.dirname(spec.origin), resource_name)
            raise ModuleNotFoundError(
                f"No module named {package_or_requirement!r}"
            )

        _pkg_res.resource_filename = _resource_filename  # type: ignore[attr-defined]
        sys.modules["pkg_resources"] = _pkg_res

try:
    from streamlit_timeline import st_timeline
except ModuleNotFoundError:

    def st_timeline(  # type: ignore[misc]
        items: list,
        groups: list | None = None,
        options: dict | None = None,
        height: str = "350px",
    ) -> dict | str | None:
        return st.session_state.get("_mock_selected_tl_item")
