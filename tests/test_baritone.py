"""Tests for bridge.baritone — pure string formatting, no Minecraft needed."""

from bridge.baritone import goto, mine, follow, explore, stop


def test_goto():
    assert goto(100, 64, -200) == "#goto 100 64 -200"


def test_goto_truncates_floats():
    assert goto(100.7, 64.3, -200.9) == "#goto 100 64 -200"


def test_mine():
    assert mine("oak_log") == "#mine oak_log"


def test_mine_diamond():
    assert mine("diamond_ore") == "#mine diamond_ore"


def test_follow():
    assert follow("Steve") == "#follow Steve"


def test_explore():
    assert explore() == "#explore"


def test_stop():
    assert stop() == "#stop"
