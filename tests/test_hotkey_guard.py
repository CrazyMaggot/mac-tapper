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
