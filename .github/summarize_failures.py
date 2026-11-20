"""Turn a pytest log into a GitHub annotation, so failures show up without log access."""

import sys
from pathlib import Path

LIMIT = 3500


def main(path: str) -> None:
    lines = Path(path).read_text(errors="replace").splitlines()
    picked = [ln for ln in lines if ln.startswith(("FAILED", "ERROR"))]
    # Short tracebacks help when the summary lines are truncated.
    tail = lines[-60:] if not picked else []
    text = "\n".join(picked + tail)[:LIMIT] or "pytest failed before producing a summary"
    escaped = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::error title=pytest failures::{escaped}")


if __name__ == "__main__":
    main(sys.argv[1])
