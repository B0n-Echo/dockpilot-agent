"""Single source of truth for where DockPilot's data lives.

Other modules import these paths instead of hard-coding folders.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
"""The repository root: two levels above src/dockpilot/."""

DATA_SOURCE = REPO_ROOT / "data" / "source"
"""The client's original data package. Read only; never written to."""

DATA_WORK = REPO_ROOT / "data" / "work"
"""Working copy of the database that the code writes to (created in dp-02, ignored by git)."""

SOURCE_DB = DATA_SOURCE / "setuhaul_freight_operations.db"
"""The original snapshot database."""
