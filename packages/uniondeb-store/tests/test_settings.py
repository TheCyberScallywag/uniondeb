import pytest
from pydantic import ValidationError

from uniondeb.store import StoreSettings


def test_defaults_target_local_enterprise():
    s = StoreSettings()
    assert s.backend == "neo4j" and s.target_database == "neo4j" and not s.is_aura


def test_from_env_reads_prefixed_variables_and_hides_password():
    env = {
        "UNIONDEB_NEO4J_URI": "neo4j+s://abc123.databases.neo4j.io",
        "UNIONDEB_NEO4J_USER": "svc_ingest",
        "UNIONDEB_NEO4J_PASSWORD": "s3cret-value",
        "UNIONDEB_NEO4J_DATABASE": "uniondeb",
    }
    s = StoreSettings.from_env(env)
    assert s.is_aura and s.user == "svc_ingest" and s.target_database == "uniondeb"
    assert "s3cret-value" not in repr(s)
    assert s.password.get_secret_value() == "s3cret-value"


def test_overrides_win_and_none_is_ignored():
    s = StoreSettings.from_env({"UNIONDEB_NEO4J_DATABASE": "a"}, database=None, synthetic=True)
    assert s.database == "a" and s.target_database == "uniondeb-synthetic"


@pytest.mark.parametrize("uri", ["http://localhost:7474", "localhost:7687", "neo4j+x://h"])
def test_rejects_unknown_schemes(uri):
    with pytest.raises(ValidationError):
        StoreSettings(uri=uri)


@pytest.mark.parametrize(
    ("uri", "path"), [("relate::memory:", ":memory:"), ("relate:/tmp/x.rl", "/tmp/x.rl"), ("relate:", ":memory:")]
)
def test_relate_uris(uri, path):
    s = StoreSettings(uri=uri)
    assert s.backend == "relate" and s.relate_path == path
