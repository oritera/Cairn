"""Allow ``python -m cairn ...`` as an alternative to the ``cairn`` console script."""

from cairn.cli import main

if __name__ == "__main__":
    main()
