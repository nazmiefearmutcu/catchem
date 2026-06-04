from unittest.mock import patch

import pytest
import typer
from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

from catchem.cli import _wrapped_app_call, app

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


def test_wrapped_app_call_success():
    with patch("catchem.cli._original_call") as mock_call:
        _wrapped_app_call("arg1", kw="val")
        mock_call.assert_called_once_with("arg1", kw="val")


def test_wrapped_app_call_validation_error():
    class DummyModel(BaseModel):
        x: int

    try:
        DummyModel(x="not an int")
    except ValidationError as dummy_exc:
        with patch("catchem.cli._original_call", side_effect=dummy_exc), pytest.raises(typer.Exit) as exc:
            _wrapped_app_call()
        assert exc.value.exit_code == 1

