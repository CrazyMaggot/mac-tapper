import pytest
from pynput import keyboard

from tapper import Hotkey, parse_cps, parse_delay, parse_hotkey, parse_minutes


class FakeKey:
    """Подражает KeyCode из pynput: у него есть только символ."""

    def __init__(self, char):
        self.char = char


def test_single_character_hotkey_matches_same_letter():
    hotkey = parse_hotkey("T")
    assert hotkey.label == "t"
    assert hotkey.matches(FakeKey("t"))
    assert hotkey.matches(FakeKey("T"))
    assert not hotkey.matches(FakeKey("y"))


def test_named_key_hotkey_matches_pynput_key():
    hotkey = parse_hotkey("F6")
    assert hotkey.matches(keyboard.Key.f6)
    assert not hotkey.matches(keyboard.Key.f7)


def test_named_aliases_resolve_to_same_key():
    assert parse_hotkey("enter").special is parse_hotkey("return").special


def test_hotkey_ignores_key_without_character():
    hotkey = parse_hotkey("t")
    assert not hotkey.matches(FakeKey(None))
    assert not hotkey.matches(keyboard.Key.shift)


def test_special_hotkey_ignores_character_key():
    hotkey = parse_hotkey("space")
    assert not hotkey.matches(FakeKey(" "))


@pytest.mark.parametrize("text", ["", "   ", "esc", "escape", "cmd", "shift", "option", "фыва", "f99"])
def test_rejected_hotkeys(text):
    with pytest.raises(ValueError):
        parse_hotkey(text)


def test_hotkey_is_hashable_and_frozen():
    hotkey = Hotkey(label="t", char="t")
    assert {hotkey}
    with pytest.raises(Exception):
        hotkey.label = "y"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1", 1.0), ("10.5", 10.5), ("10,5", 10.5), (" 1000 ", 1000.0), ("0.01", 0.01)],
)
def test_accepted_frequencies(text, expected):
    assert parse_cps(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "0", "-5", "1000.1", "2000", "быстро", None])
def test_rejected_frequencies(text):
    with pytest.raises(ValueError):
        parse_cps(text)


@pytest.mark.parametrize("text", ["0", "0.5", "60"])
def test_accepted_delays(text):
    parse_delay(text)


@pytest.mark.parametrize("text", ["-0.1", "61", ""])
def test_rejected_delays(text):
    with pytest.raises(ValueError):
        parse_delay(text)


@pytest.mark.parametrize("text", ["1", "0.5", "600"])
def test_accepted_limits(text):
    parse_minutes(text)


@pytest.mark.parametrize("text", ["0", "-1", "601", "потом"])
def test_rejected_limits(text):
    with pytest.raises(ValueError):
        parse_minutes(text)
