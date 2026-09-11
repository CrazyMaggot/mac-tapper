"""Сторож, который решает, молчит ли горячая клавиша."""
import pytest

from tapper import hotkey_suppressed


class Widget:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"Widget({self.name})"


@pytest.fixture
def entries():
    return (Widget("частота"), Widget("клавиша"))


def test_hotkey_works_when_another_application_is_in_front(entries):
    """Главный случай: мы набрали частоту, ушли в браузер и жмём клавишу."""
    assert hotkey_suppressed(False, entries[0], entries) is False


def test_hotkey_silent_while_typing_in_our_own_field(entries):
    assert hotkey_suppressed(True, entries[1], entries) is True


def test_hotkey_works_when_our_window_is_front_but_field_not_focused(entries):
    assert hotkey_suppressed(True, Widget("кнопка"), entries) is False


def test_hotkey_works_when_nothing_is_focused(entries):
    assert hotkey_suppressed(True, None, entries) is False


def test_guard_compares_widgets_by_identity(entries):
    """Разные виджеты не должны считаться одним из-за сравнения на равенство."""
    twin = Widget(entries[0].name)
    assert hotkey_suppressed(True, twin, entries) is False


# --- независимость от раскладки -------------------------------------------

from tapper import parse_hotkey  # noqa: E402


class KeyCode:
    """Подражает KeyCode из pynput: символ зависит от раскладки, код клавиши нет."""

    def __init__(self, char, vk=None):
        self.char = char
        self.vk = vk


def test_latin_hotkey_matches_same_physical_key_in_russian_layout():
    """Клавиша T в русской раскладке присылает «е», физический код тот же."""
    hotkey = parse_hotkey("t")
    assert hotkey.vk == 17
    assert hotkey.matches(KeyCode("е", 17))
    assert hotkey.matches(KeyCode("t", 17))


def test_hotkey_does_not_match_other_physical_key():
    hotkey = parse_hotkey("t")
    assert not hotkey.matches(KeyCode("y", 16))


def test_hotkey_falls_back_to_character_when_code_unknown():
    hotkey = parse_hotkey("t")
    assert hotkey.matches(KeyCode("t"))


def test_cyrillic_hotkey_has_no_code_and_matches_by_character():
    hotkey = parse_hotkey("ё")
    assert hotkey.vk is None
    assert hotkey.matches(KeyCode("Ё"))
