"""retypeset UI package -- Streamlit panels shared by the wizard and the classic console.

Each module here exposes plain functions that take the manuscript and the target
profile and draw one panel. Nothing in `ui` owns state: session state lives in
`ui.common`, so the same panel can be drawn as a wizard step or as a tab without
being written twice.
"""

from . import common  # noqa: F401

__all__ = ["common"]
