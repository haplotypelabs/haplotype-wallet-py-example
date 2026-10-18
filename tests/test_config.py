import pytest

from wallet_example.config import load_settings


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLET_CLIENT_ID", "client")
    monkeypatch.setenv("WALLET_CLIENT_SECRET", "secret")


@pytest.mark.parametrize(("value", "on"), [("1", True), ("true", False), ("0", False), ("", False)])
def test_test_hooks_only_for_exactly_1(
    monkeypatch: pytest.MonkeyPatch, value: str, on: bool
) -> None:
    monkeypatch.setenv("EXAMPLE_TEST_HOOKS", value)
    assert load_settings().test_hooks is on


def test_quotes_around_values_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLET_SCOPES", '"openid samples:read"')
    assert load_settings().scopes == "openid samples:read"
