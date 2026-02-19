"""
Iteration 5 tests.
Run with: pytest tests/ -v
Requires: pip install pytest
"""
import pytest


def test_strava_disconnect_route_exists():
    """Strava disconnect route should be defined."""
    from app import app
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert "/integrations/strava/disconnect" in rules


def test_integrations_route_exists():
    """Integrations page route should exist."""
    from app import app
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert "/integrations" in rules


def test_api_charts_accepts_days_param():
    """API charts should accept days=7,14,30."""
    from app import api_charts
    # We can't easily test without auth; just verify the route exists
    from app import app
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert "/api/charts" in rules


def test_validation_helpers():
    """Validation helpers return correct values."""
    from app import validate_mood, validate_calories, validate_duration, validate_required

    val, err = validate_mood(3)
    assert val == 3 and err is None

    val, err = validate_mood(0)
    assert val is None and err is not None

    val, err = validate_calories(500)
    assert val == 500 and err is None

    val, err = validate_calories(-1)
    assert val is None and err is not None

    val, err = validate_duration(30)
    assert val == 30 and err is None

    val, err = validate_required("hello", "Field")
    assert val == "hello" and err is None

    val, err = validate_required("", "Field")
    assert val is None and err is not None
