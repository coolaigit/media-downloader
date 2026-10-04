# -*- coding: utf-8 -*-
from app import main


def test_old_boolean_without_version_requires_current_wizard(monkeypatch):
    monkeypatch.setattr(main.store, "load", lambda: {
        "onboarding_complete": True,
        "onboarding_version": "",
    })
    r = main.api_onboarding()
    assert r["complete"] is False
    assert r["required_onboarding_version"] == main.ONBOARDING_VERSION


def test_current_version_is_complete(monkeypatch):
    monkeypatch.setattr(main.store, "load", lambda: {
        "onboarding_complete": True,
        "onboarding_version": main.ONBOARDING_VERSION,
    })
    assert main.api_onboarding()["complete"] is True


def test_finish_wizard_persists_version(monkeypatch):
    saved = {}
    monkeypatch.setattr(main.store, "save", lambda patch: saved.update(patch) or patch)
    r = main.api_onboarding_complete()
    assert saved["onboarding_complete"] is True
    assert saved["onboarding_version"] == main.ONBOARDING_VERSION
    assert r["complete"] is True
