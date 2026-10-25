"""The off-box uploader's federated credential (2.8.0): derived from the host's agent federation, never asked."""
from __future__ import annotations

import json
import stat

import pytest

from ai_resources import openclaw_host as host
from ai_resources.setup import state, ui
from ai_resources.setup.cockpits import _openclaw_host as oh

SA = "k3s-backup@wildbit-iac.iam.gserviceaccount.com"
AGENT = {"type": "external_account", "audience": "//iam.googleapis.com/projects/1/locations/global/"
         "workloadIdentityPools/agent-hosts/providers/bithome",
         "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
         "ai-ops-ro@wildbit-iac.iam.gserviceaccount.com:generateAccessToken",
         "credential_source": {"executable": {"command": "/h/subject-token.sh", "timeout_millis": 30000}}}


@pytest.fixture
def source(tmp_path):
    p = tmp_path / "agent-wif" / "cred-config.json"
    p.parent.mkdir()
    p.write_text(json.dumps(AGENT), encoding="utf-8")
    return p


def test_the_derived_file_is_the_same_federation_impersonating_the_backup_account(source, tmp_path):
    target = tmp_path / "out" / "cred.json"
    assert host.offbox_wif_cred(SA, env={}, source=source, target=target) == ("written", target)
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["service_account_impersonation_url"].endswith(f"/serviceAccounts/{SA}:generateAccessToken")
    assert doc["audience"] == AGENT["audience"] and doc["credential_source"] == AGENT["credential_source"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert host.offbox_wif_cred(SA, env={}, source=source, target=target) == ("same", target)


def test_the_exported_variable_is_preferred_over_the_default_path(source, tmp_path):
    target = tmp_path / "cred.json"
    env = {"CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": str(source)}
    assert host.offbox_wif_cred(SA, env=env, source=tmp_path / "nope.json", target=target)[0] == "written"


@pytest.mark.parametrize("doc", [{"type": "service_account"}, {"type": "external_account", "credential_source":
                                                                {"file": "/x"}}, None])
def test_no_executable_federation_means_nothing_is_written(tmp_path, doc):
    src = tmp_path / "c.json"
    if doc is not None:
        src.write_text(json.dumps(doc), encoding="utf-8")
    target = tmp_path / "cred.json"
    assert host.offbox_wif_cred(SA, env={}, source=src, target=target) == ("absent", None)
    assert not target.exists()


def test_a_bad_service_account_is_refused(source, tmp_path):
    assert host.offbox_wif_cred("x; rm -rf /", env={}, source=source, target=tmp_path / "c.json")[0] == "absent"


@pytest.fixture
def hostenv(monkeypatch, source, tmp_path):
    env: dict = {"OPENCLAW_OFFBOX_BUCKET": "gs://b"}
    monkeypatch.setattr(host, "read_host_env", lambda path=None: dict(env))
    monkeypatch.setattr(host, "write_host_env", lambda values, path=None: [
        env.pop(k, None) if v is None else env.__setitem__(k, v) for k, v in values.items()] and True)
    monkeypatch.setattr(host, "OFFBOX_WIF_CRED_PATH", tmp_path / "derived.json")
    monkeypatch.setattr(host, "_WIF_DEFAULT_SOURCE", source)
    for v in host._WIF_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(ui, "ok", lambda m: None)
    monkeypatch.setattr(ui, "detail", lambda m: None)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("asked"))
    return env


def test_setup_points_the_uploader_at_the_derived_file_without_asking(hostenv, tmp_path):
    o = state.OpenClawState()
    assert oh._configure_offbox_wif(o, dry_run=False) is True
    assert hostenv["OPENCLAW_OFFBOX_CRED_FILE"] == str(tmp_path / "derived.json")
    assert o.host_env_previous == {"OPENCLAW_OFFBOX_CRED_FILE": None}
    assert SA in (tmp_path / "derived.json").read_text(encoding="utf-8")


def test_an_operator_credential_file_is_left_alone(hostenv, tmp_path):
    hostenv["OPENCLAW_OFFBOX_CRED_FILE"] = "/mine.json"
    o = state.OpenClawState()
    assert oh._configure_offbox_wif(o, dry_run=False) is True
    assert hostenv["OPENCLAW_OFFBOX_CRED_FILE"] == "/mine.json" and not (tmp_path / "derived.json").exists()


def test_without_a_bucket_nothing_is_derived(hostenv, tmp_path):
    hostenv.pop("OPENCLAW_OFFBOX_BUCKET")
    assert oh._configure_offbox_wif(state.OpenClawState(), dry_run=False) is False
    assert not (tmp_path / "derived.json").exists()


def test_a_dry_run_writes_nothing(hostenv, tmp_path):
    assert oh._configure_offbox_wif(state.OpenClawState(), dry_run=True) is True
    assert "OPENCLAW_OFFBOX_CRED_FILE" not in hostenv and not (tmp_path / "derived.json").exists()
