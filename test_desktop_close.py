# -*- coding: utf-8 -*-
from app import desktop_shell as ds


class FakeWindow:
    def __init__(self):
        self.hidden = False

    def hide(self):
        self.hidden = True


def test_close_exit_allows_real_close(monkeypatch):
    w = FakeWindow()
    state = {"force_exit": False, "tray_icon": None}
    monkeypatch.setattr(ds, "_close_behavior", lambda: "exit")
    monkeypatch.setattr(ds, "_ensure_tray", lambda window, state: object())
    assert ds._handle_window_close(w, state) is None
    assert w.hidden is False


def test_close_tray_hides_and_cancels_close(monkeypatch):
    w = FakeWindow()
    state = {"force_exit": False, "tray_icon": None}
    called = []
    monkeypatch.setattr(ds, "_close_behavior", lambda: "tray")
    monkeypatch.setattr(ds, "_ensure_tray", lambda window, state: called.append(True) or object())
    assert ds._handle_window_close(w, state) is False
    assert w.hidden is True
    assert state["hidden_to_tray"] is True
    assert called == [True]


def test_tray_exit_bypasses_minimize(monkeypatch):
    w = FakeWindow()
    state = {"force_exit": True, "tray_icon": object()}
    monkeypatch.setattr(ds, "_close_behavior", lambda: "tray")
    assert ds._handle_window_close(w, state) is None
    assert w.hidden is False


def test_tray_failure_falls_back_to_exit(monkeypatch):
    w = FakeWindow()
    state = {"force_exit": False, "tray_icon": None}

    def fail(window, state):
        raise RuntimeError("tray init failed")

    monkeypatch.setattr(ds, "_close_behavior", lambda: "tray")
    monkeypatch.setattr(ds, "_ensure_tray", fail)
    assert ds._handle_window_close(w, state) is None
    assert w.hidden is False
