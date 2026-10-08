import pytest

from selector_drift.config import get_settings
from selector_drift.credentials import registry
from selector_drift.credentials.session import SessionInjection
from selector_drift.scope.errors import ScopeRefusal
from tests.credentials.conftest import SCOPE_YAML


@pytest.fixture
def scopes(tmp_path, monkeypatch):
    d = tmp_path / "scopes"
    d.mkdir()
    (d / "testbench.yaml").write_text(SCOPE_YAML, encoding="utf-8")
    monkeypatch.setenv("SD_SCOPE_DIR", str(d))
    monkeypatch.setenv("SD_SESSION_DIR", str(tmp_path / "sessions"))  # Settings.session_dir
    get_settings.cache_clear()
    yield d
    get_settings.cache_clear()


@pytest.mark.unit
def test_the_default_posture_is_session_injection(scopes, tmp_path):
    provider = registry.provider_for("testbench")
    assert isinstance(provider, SessionInjection)
    assert provider.storage_state_path == tmp_path / "sessions" / "testbench.json"


@pytest.mark.unit
def test_an_unknown_target_is_refused(scopes):
    with pytest.raises(ScopeRefusal, match="unknown target"):
        registry.provider_for("nowhere")


@pytest.mark.unit
def test_a_scope_file_naming_another_target_is_refused(scopes):
    (scopes / "other.yaml").write_text(SCOPE_YAML, encoding="utf-8")  # says target: testbench
    with pytest.raises(ScopeRefusal, match="names target"):
        registry.provider_for("other")


@pytest.mark.unit
@pytest.mark.parametrize("name", ["../evidence/x", "..", "a/b", "Testbench", "-x", ""])
def test_a_target_name_cannot_escape_the_sessions_directory(name):
    with pytest.raises(ValueError):
        registry.session_path(name)


@pytest.mark.unit
def test_provider_for_refuses_a_path_shaped_target_before_reading_anything(scopes):
    with pytest.raises(ValueError):
        registry.provider_for("../scopes/testbench")


@pytest.mark.unit
def test_a_registered_factory_receives_the_whole_scope_record(scopes, monkeypatch):
    seen = []

    def factory(record):
        seen.append(record)
        return SessionInjection(registry.session_path(record.target))

    monkeypatch.setitem(registry._FACTORIES, "session_injection", factory)
    registry.provider_for("testbench")
    assert [r.target for r in seen] == ["testbench"]
    assert seen[0].authorising_party == "Acme Claims Ltd"


@pytest.mark.unit
def test_register_adds_a_posture(monkeypatch):
    # Task 32: every posture is registered at import and register() refuses a re-registration
    # (controller ruling a), so the slot is emptied first to exercise the add path.
    monkeypatch.setattr(registry, "_FACTORIES", dict(registry._FACTORIES))
    del registry._FACTORIES["client_broker"]

    def factory(record):
        return SessionInjection(registry.session_path(record.target))

    registry.register("client_broker", factory)
    assert registry._FACTORIES["client_broker"] is factory
