from __future__ import annotations

from .harness import render_markdown, run_calibration


def main() -> int:
    print(render_markdown(run_calibration()), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
