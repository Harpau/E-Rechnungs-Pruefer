import sys
from collections.abc import Sequence


def _run(argv: Sequence[str]) -> int:
    from app.processing.bootstrap import dispatch_if_requested

    status = dispatch_if_requested(argv)
    if status is not None:
        return status

    from app.windows_launcher import main

    return main(argv)


if __name__ == "__main__":
    raise SystemExit(_run(sys.argv[1:]))
