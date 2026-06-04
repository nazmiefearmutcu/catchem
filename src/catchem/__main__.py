"""Allow `python -m catchem ...` to invoke the Typer CLI.

The `catchem` console script registered in pyproject.toml is the primary
entry point; this shim mirrors it so the module-form works too (useful in
virtualenvs where the entry-point bin isn't on PATH).
"""

from .cli import app

if __name__ == "__main__":
    app()
