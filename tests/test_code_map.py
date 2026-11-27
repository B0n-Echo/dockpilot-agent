"""The code map (dp-25) stays true: every file:line link still starts the function it names."""

import re

from dockpilot import paths

LINK = re.compile(r"\]\(\.\./(src/dockpilot/\w+\.py)#L(\d+)\)\s*`([\w.]+)`")


def test_every_map_link_points_at_its_function():
    """Each [file.py:N](../src/dockpilot/file.py#LN) `name` link: line N must be `def name`."""
    links = LINK.findall((paths.REPO_ROOT / "docs" / "06_code_map.md").read_text())
    assert len(links) >= 30
    stale = []
    for path, line, name in links:
        text = (paths.REPO_ROOT / path).read_text().splitlines()[int(line) - 1]
        if not re.match(rf"\s*def {name.split('.')[-1]}\(", text):
            stale.append(f"{path}:{line} is no longer {name}")
    assert not stale, "\n".join(stale)
