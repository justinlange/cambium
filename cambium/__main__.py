"""`python -m cambium` behaves exactly like the `cambium` console script."""

from cambium.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
