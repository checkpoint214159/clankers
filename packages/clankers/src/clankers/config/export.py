"""Export robots.yaml to JSON for the studio frontend.

The studio must never parse YAML or duplicate topology; it imports the generated
`apps/studio/src/generated/robotsConfig.json`. Regenerate after any robots.yaml change:

    uv run clankers-export-config
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .loader import load_robots, robots_yaml_path


def repo_root() -> Path:
    # packages/clankers/src/clankers/config/export.py -> repo root is 5 levels up
    return Path(__file__).resolve().parents[5]


def default_output() -> Path:
    return repo_root() / "apps" / "studio" / "src" / "generated" / "robotsConfig.json"


def main() -> int:
    cfg = load_robots()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else default_output()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_generated_from": str(robots_yaml_path()),
        "_command": "uv run clankers-export-config",
        **cfg.raw,
    }
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
