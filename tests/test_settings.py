import pytest

from tender_scraper.settings import boolean_setting


@pytest.mark.parametrize("value", ["true", "TRUE", " 1 ", "yes"])
def test_boolean_setting_true_values(monkeypatch, value):
    monkeypatch.setenv("FLAG", value)
    assert boolean_setting("FLAG") is True


@pytest.mark.parametrize("value", ["false", "0", "No"])
def test_boolean_setting_false_values(monkeypatch, value):
    monkeypatch.setenv("FLAG", value)
    assert boolean_setting("FLAG", default=True) is False


@pytest.mark.parametrize("value", [None, "", "  "])
def test_boolean_setting_uses_default_when_unset(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("FLAG", raising=False)
    else:
        monkeypatch.setenv("FLAG", value)
    assert boolean_setting("FLAG") is False
    assert boolean_setting("FLAG", default=True) is True


def test_boolean_setting_rejects_ambiguous_value(monkeypatch):
    monkeypatch.setenv("FLAG", "on")
    with pytest.raises(ValueError, match="FLAG must be true or false"):
        boolean_setting("FLAG")
