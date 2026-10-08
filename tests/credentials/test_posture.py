import secrets
import typing

import pytest
from pydantic import SecretStr

from selector_drift.config import get_settings
from selector_drift.credentials import registry
from selector_drift.credentials.broker import ClientBroker
from selector_drift.credentials.provider import NotSupported
from selector_drift.credentials.session import SessionInjection
from selector_drift.credentials.store import EncryptedStore
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.record import CredentialPosture, ScopeRecord
from tests.credentials.conftest import SCOPE_YAML

POSTURES = {"session_injection", "client_broker", "encrypted_store"}


@pytest.fixture
def scope_with_posture(tmp_path, monkeypatch):
    d = tmp_path / "scopes"
    d.mkdir()
    monkeypatch.setenv("SD_SCOPE_DIR", str(d))
    monkeypatch.setenv("SD_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    monkeypatch.setenv("SD_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("SD_CREDENTIAL_STORE", str(tmp_path / "credentials"))
    for name in ("SD_BROKER_URL_CLAIMS_PORTAL", "SD_BROKER_TOKEN_CLAIMS_PORTAL"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    def write(target: str, posture: str) -> None:
        text = SCOPE_YAML.replace("target: testbench", f"target: {target}")
        (d / f"{target}.yaml").write_text(
            text + f"credential_posture: {posture}\n", encoding="utf-8"
        )

    yield write
    get_settings.cache_clear()


@pytest.mark.unit
def test_the_posture_defaults_to_the_default_offer():
    assert ScopeRecord.model_fields["credential_posture"].default == "session_injection"


@pytest.mark.unit
def test_no_posture_stores_a_second_factor():
    postures = typing.get_args(ScopeRecord.model_fields["credential_posture"].annotation)
    assert set(postures) == POSTURES == set(typing.get_args(CredentialPosture))
    assert not any("totp" in p or "seed" in p for p in postures)


@pytest.mark.unit
def test_every_posture_has_a_provider_at_import():
    """Task 31's own registration (session injection) survives, beside Task 32's two."""
    assert set(registry._FACTORIES) == POSTURES


@pytest.mark.unit
def test_an_unknown_posture_is_refused_at_load_not_downgraded(scope_with_posture):
    # Moved from Task 31's amendment: once the field is a Literal, the refusal is at load.
    scope_with_posture("testbench", "totp_seed_store")
    with pytest.raises(ScopeRefusal, match="credential_posture"):
        registry.provider_for("testbench")


@pytest.mark.unit
def test_a_known_posture_with_no_provider_is_refused_not_downgraded(
    scope_with_posture, monkeypatch
):
    scope_with_posture("testbench", "client_broker")
    monkeypatch.delitem(registry._FACTORIES, "client_broker")
    with pytest.raises(LookupError, match="refusing, not downgrading"):
        registry.provider_for("testbench")


@pytest.mark.unit
def test_a_posture_cannot_be_registered_twice(monkeypatch):
    """Controller ruling (a): a weaker posture can never quietly replace a stronger one."""
    monkeypatch.setattr(registry, "_FACTORIES", dict(registry._FACTORIES))
    before = dict(registry._FACTORIES)
    for posture in POSTURES:
        with pytest.raises(ValueError, match="already registered"):
            registry.register(posture, lambda record: EncryptedStore("x"))
    assert dict(registry._FACTORIES) == before


@pytest.mark.unit
def test_an_unknown_posture_cannot_be_registered(monkeypatch):
    monkeypatch.setattr(registry, "_FACTORIES", dict(registry._FACTORIES))
    with pytest.raises(ValueError, match="not a credential posture"):
        registry.register("totp_seed_store", lambda record: EncryptedStore("x"))  # type: ignore[arg-type]
    assert "totp_seed_store" not in registry._FACTORIES


@pytest.mark.unit
def test_the_default_posture_still_resolves_to_session_injection(scope_with_posture, tmp_path):
    scope_with_posture("testbench", "session_injection")
    provider = registry.provider_for("testbench")
    assert isinstance(provider, SessionInjection)
    assert provider.storage_state_path == tmp_path / "sessions" / "testbench.json"


@pytest.mark.unit
def test_the_broker_posture_reads_its_endpoint_and_token_from_the_environment(
    scope_with_posture, monkeypatch
):
    token = f"broker-test-only-{secrets.token_hex(16)}"
    scope_with_posture("claims-portal", "client_broker")
    monkeypatch.setenv("SD_BROKER_URL_CLAIMS_PORTAL", "https://broker.example.invalid/v1")
    monkeypatch.setenv("SD_BROKER_TOKEN_CLAIMS_PORTAL", token)
    provider = registry.provider_for("claims-portal")
    assert isinstance(provider, ClientBroker)
    assert token not in repr(provider)


@pytest.mark.unit
@pytest.mark.parametrize(
    "missing", ["SD_BROKER_URL_CLAIMS_PORTAL", "SD_BROKER_TOKEN_CLAIMS_PORTAL"]
)
def test_the_broker_posture_without_its_environment_is_refused(
    scope_with_posture, monkeypatch, missing
):
    scope_with_posture("claims-portal", "client_broker")
    monkeypatch.setenv("SD_BROKER_URL_CLAIMS_PORTAL", "https://broker.example.invalid/v1")
    monkeypatch.setenv("SD_BROKER_TOKEN_CLAIMS_PORTAL", f"t-{secrets.token_hex(8)}")
    monkeypatch.setenv(missing, "")  # set-but-empty is unset
    with pytest.raises(LookupError, match="SD_BROKER_URL_CLAIMS_PORTAL"):
        registry.provider_for("claims-portal")


@pytest.mark.unit
def test_the_store_posture_keys_by_client_not_by_target(scope_with_posture, tmp_path):
    scope_with_posture("claims-portal", "encrypted_store")
    provider = registry.provider_for("claims-portal")
    assert isinstance(provider, EncryptedStore) and provider.client_id == "acme-claims-ltd"
    assert not (tmp_path / "credentials").exists()  # resolving a provider writes nothing


@pytest.mark.unit
@pytest.mark.parametrize(
    ("party", "slug"),
    [("Acme Claims Ltd", "acme-claims-ltd"), ("  O'Brien & Sons, Inc. ", "o-brien-sons-inc")],
)
def test_the_client_slug(party, slug):
    assert registry.client_slug(party) == slug


@pytest.mark.unit
def test_only_the_client_broker_can_produce_a_totp_code(tmp_path, target, httpx_mock):
    for provider in (SessionInjection(tmp_path / "s.json"), EncryptedStore("a", root=tmp_path)):
        with pytest.raises(NotSupported):
            provider.get_totp_code(target)
    httpx_mock.add_response(json={"code": "123456"})
    token = SecretStr(f"t-{secrets.token_hex(8)}")
    assert ClientBroker("https://b.example.invalid", token).get_totp_code(target) == "123456"
