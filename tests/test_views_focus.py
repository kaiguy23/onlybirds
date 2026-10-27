import pytest

from onlybirds.dashboard.views import _parse_focus


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30.2672,-97.7431,40", (30.2672, -97.7431, 40.0)),
        ("30.2672,-97.7431", (30.2672, -97.7431, 25.0)),
        (None, None),
        ("", None),
        ("abc,1", None),
        ("95,10,5", None),
        ("30", None),
    ],
)
def test_parse_focus(raw, expected):
    assert _parse_focus(raw) == expected
