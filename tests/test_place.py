import pytest

from onlybirds.dashboard.place import _parse_coords, parse_focus, within_radius


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
    assert parse_focus(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30.27, -97.74", (30.27, -97.74)),
        ("30.27 -97.74", (30.27, -97.74)),
        ("Austin, TX", None),
        ("95, 10", None),
        ("1, 2, 3", None),
    ],
)
def test_parse_coords(text, expected):
    assert _parse_coords(text) == expected


def test_within_radius():
    import pandas as pd

    # Austin; Round Rock (~27 km north); Houston (~235 km east).
    df = pd.DataFrame({"lat": [30.2672, 30.5083, 29.7604], "lon": [-97.7431, -97.6789, -95.3698]})
    assert within_radius(df, (30.2672, -97.7431, 25)).tolist() == [True, False, False]
    assert within_radius(df, (30.2672, -97.7431, 50)).tolist() == [True, True, False]
