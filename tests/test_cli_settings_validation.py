from unittest.mock import patch

from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

from catchem.cli import app

runner = CliRunner()


def test_cli_graceful_exit_on_validation_error():
    # Mock _real_load_settings to raise a ValidationError
    with patch("catchem.cli._real_load_settings") as mock_load:
        class DummyModel(BaseModel):
            x: int

        try:
            DummyModel(x="not an int")
        except ValidationError as dummy_exc:
            mock_load.side_effect = dummy_exc

        # Run any command
        result = runner.invoke(app, ["run"])

        # Should exit with code 1
        assert result.exit_code == 1
        # Should print graceful error message to stderr
        assert "Configuration error:" in result.stderr or "Configuration error:" in result.stdout
