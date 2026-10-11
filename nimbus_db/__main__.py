"""python -m nimbus_db -- sqlite-style shell entry point."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
