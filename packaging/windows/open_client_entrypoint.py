import sys


def _run(argv: list[str]) -> int:
    try:
        from app.windows_open_client import main

        return main(argv)
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_run(sys.argv[1:]))
