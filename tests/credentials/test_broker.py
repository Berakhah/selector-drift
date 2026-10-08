import builtins
import io
import logging
import os
import secrets

import pytest
from pydantic import SecretStr

from selector_drift.config import get_settings
from selector_drift.credentials.broker import BrokerError, ClientBroker
from selector_drift.credentials.provider import Credentials

ENDPOINT = "https://broker.client.example.invalid/v1"


@pytest.fixture
def token() -> SecretStr:
    # Generated per test: no token-shaped literal in the repo.
    return SecretStr(f"broker-test-only-{secrets.token_hex(16)}")


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Every place the product writes, pointed at one tree -- the broker's real storage paths."""
    root = tmp_path / "all-storage"
    root.mkdir()
    monkeypatch.setenv("SD_EVIDENCE_ROOT", str(root / "evidence"))
    monkeypatch.setenv("SD_LAKE_ROOT", str(root / "lake"))
    monkeypatch.setenv("SD_SESSION_DIR", str(root / "sessions"))
    monkeypatch.setenv("SD_CREDENTIAL_STORE", str(root / "credentials"))
    monkeypatch.chdir(root)
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def writes(monkeypatch):
    """Every file opened for writing, by any code path, during the test: `open`, `io.open`
    (what pathlib's write_text/write_bytes/open use) and the low-level `os.open`."""
    opened: list[str] = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def _spy(real):
        def spy_open(file, mode="r", *args, **kwargs):
            if any(ch in mode for ch in "wax+"):
                opened.append(str(file))
            return real(file, mode, *args, **kwargs)

        return spy_open

    def spy_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            opened.append(str(path))
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _spy(real_open))
    monkeypatch.setattr(io, "open", _spy(real_io_open))
    monkeypatch.setattr(os, "open", spy_os_open)
    return opened


@pytest.mark.unit
def test_the_write_spy_sees_every_kind_of_write(tmp_path, writes):
    """Non-vacuity of the next test: the spy records each write path the product could use."""
    (tmp_path / "a").write_text("x", encoding="utf-8")
    with open(tmp_path / "b", "wb"):
        pass
    os.close(os.open(tmp_path / "c", os.O_WRONLY | os.O_CREAT, 0o600))
    assert [p.rsplit(os.sep, 1)[-1] for p in writes] == ["a", "b", "c"]


@pytest.mark.unit
def test_broker_fetches_a_code_and_stores_nothing(httpx_mock, target, storage, writes, token):
    # Fixed (M19): asserted against the product's real storage roots, not an unrelated tmp_path.
    httpx_mock.add_response(url=f"{ENDPOINT}/totp?target=testbench", json={"code": "123456"})
    broker = ClientBroker(ENDPOINT, token)
    assert broker.get_totp_code(target) == "123456"
    assert writes == []
    assert list(storage.rglob("*")) == []
    assert "123456" not in repr(vars(broker))


@pytest.mark.unit
def test_the_token_travels_in_a_header_and_never_in_the_url(httpx_mock, target, token):
    httpx_mock.add_response(url=f"{ENDPOINT}/totp?target=testbench", json={"code": "654321"})
    ClientBroker(ENDPOINT, token).get_totp_code(target)
    (request,) = httpx_mock.get_requests()
    assert request.headers["Authorization"] == f"Bearer {token.get_secret_value()}"
    assert token.get_secret_value() not in str(request.url)


@pytest.mark.unit
def test_broker_token_is_never_logged(caplog, httpx_mock, target, token):
    httpx_mock.add_response(
        url=f"{ENDPOINT}/credentials?target=testbench",
        json={"username": "u", "password": "p-test-only"},
    )
    with caplog.at_level(logging.DEBUG):
        creds = ClientBroker(ENDPOINT, token).get(target)
    assert token.get_secret_value() not in caplog.text
    assert "p-test-only" not in caplog.text
    assert isinstance(creds, Credentials) and creds.password is not None
    assert creds.password.get_secret_value() == "p-test-only"


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 500])
def test_a_broker_error_does_not_echo_the_token(httpx_mock, target, token, status):
    httpx_mock.add_response(url=f"{ENDPOINT}/totp?target=testbench", status_code=status)
    with pytest.raises(BrokerError) as exc:
        ClientBroker(ENDPOINT, token).get_totp_code(target)
    assert token.get_secret_value() not in str(exc.value)
    # Nothing chained: neither the cause nor the implicit context carries httpx's request.
    assert exc.value.__cause__ is None and exc.value.__context__ is None


@pytest.mark.unit
def test_an_unreachable_broker_is_a_broker_error(httpx_mock, target, token):
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("refused"))
    with pytest.raises(BrokerError, match="unreachable") as exc:
        ClientBroker(ENDPOINT, token).get_totp_code(target)
    assert exc.value.__context__ is None


@pytest.mark.unit
@pytest.mark.parametrize("answer", [{"code": "12345"}, {"code": "abcdef"}, {"code": 123456}, {}])
def test_a_malformed_code_is_refused(httpx_mock, target, answer, token):
    httpx_mock.add_response(url=f"{ENDPOINT}/totp?target=testbench", json=answer)
    with pytest.raises(BrokerError, match="6-8 digit"):
        ClientBroker(ENDPOINT, token).get_totp_code(target)


@pytest.mark.unit
def test_a_non_object_answer_is_refused(httpx_mock, target, token):
    httpx_mock.add_response(url=f"{ENDPOINT}/totp?target=testbench", json=["123456"])
    with pytest.raises(BrokerError, match="not an object"):
        ClientBroker(ENDPOINT, token).get_totp_code(target)


@pytest.mark.unit
def test_a_malformed_credential_answer_does_not_print_the_password(httpx_mock, target, token):
    httpx_mock.add_response(
        url=f"{ENDPOINT}/credentials?target=testbench",
        json={"username": ["not", "a", "string"], "password": "p-test-only"},
    )
    with pytest.raises(BrokerError) as exc:
        ClientBroker(ENDPOINT, token).get(target)
    assert "p-test-only" not in str(exc.value)
    assert exc.value.__context__ is None  # the ValidationError (which echoes input) is gone


@pytest.mark.unit
@pytest.mark.parametrize("answer", [{}, {"password": ["p-test-only"]}, {"username": "u"}])
def test_an_answer_with_no_usable_credential_is_refused(httpx_mock, target, token, answer):
    httpx_mock.add_response(url=f"{ENDPOINT}/credentials?target=testbench", json=answer)
    with pytest.raises(BrokerError) as exc:
        ClientBroker(ENDPOINT, token).get(target)
    assert "p-test-only" not in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://broker.example.invalid",
        "https://u:p@broker.example.invalid",
        "https://broker.example.invalid/?token=x",
        "https://broker.example.invalid/#frag",
        "broker.example.invalid",
    ],
)
def test_a_broker_endpoint_must_be_plain_https(url, token):
    with pytest.raises(ValueError) as exc:
        ClientBroker(url, token)
    assert token.get_secret_value() not in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize("timeout", [0, -1.0])
def test_a_broker_timeout_must_be_positive(timeout, token):
    with pytest.raises(ValueError):
        ClientBroker(ENDPOINT, token, timeout=timeout)


@pytest.mark.unit
def test_the_repr_hides_the_token(token):
    r = repr(ClientBroker(ENDPOINT, token))
    assert token.get_secret_value() not in r and "broker.client.example.invalid" in r
