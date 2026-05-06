"""Unit tests for playback controller HTTP routes."""

from unittest.mock import MagicMock, patch

import pytest
import werkzeug
from flask import Flask

# Monkeypatch werkzeug.__version__ for Flask compatibility if missing
if not hasattr(werkzeug, "__version__"):
    werkzeug.__version__ = "3.0.0"

from pikaraoke.karaoke import Karaoke
from pikaraoke.routes.controller import controller_bp


@pytest.fixture
def app():
    test_app = Flask(__name__)
    test_app.register_blueprint(controller_bp)
    # /skip etc. redirect to home.home; register a stub so url_for resolves.
    test_app.add_url_rule("/", endpoint="home.home", view_func=lambda: "")
    return test_app


@pytest.fixture
def client(app):
    return app.test_client()


class TestSkipBreakRoute:
    def test_skip_splash_delay_class_default_is_false(self):
        """Karaoke ships with the splash-delay short-circuit flag off."""
        assert Karaoke.skip_splash_delay is False

    @patch("pikaraoke.routes.controller.broadcast_event")
    @patch("pikaraoke.routes.controller.get_karaoke_instance")
    def test_skip_break_sets_flag_and_returns_204(
        self, mock_get_instance, mock_broadcast, client
    ):
        k = MagicMock()
        k.skip_splash_delay = False
        mock_get_instance.return_value = k

        response = client.post("/skip_break")

        assert response.status_code == 204
        assert k.skip_splash_delay is True
        mock_broadcast.assert_called_once_with("skip_break", "user command")

    @patch("pikaraoke.routes.controller.broadcast_event")
    @patch("pikaraoke.routes.controller.get_karaoke_instance")
    def test_skip_break_get_not_allowed(self, mock_get_instance, _mock_broadcast, client):
        """Only POST is accepted to avoid stray GET prefetches flipping the flag."""
        mock_get_instance.return_value = MagicMock()

        response = client.get("/skip_break")

        assert response.status_code == 405
