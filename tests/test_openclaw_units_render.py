"""The ten openclaw-* systemd unit templates and their renderer.

`tests/fixtures/systemd/` holds the units as they run on the reference host (host paths and the
operator id replaced by placeholders); the templates must agree with them everywhere except the
two things this port changes on purpose: where the scripts live, and how the verify unit
notifies. `systemctl` is a recorder here: no test reaches a live unit. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402

TEMPLATES = REPO / "templates" / "systemd"
LIVE = REPO / "tests" / "fixtures" / "systemd"
LIBEXEC = "/opt/kit/libexec"


def _directives(text: str) -> list[str]:
    """Non-comment, non-blank lines, so prose translated in a comment is not a difference."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _relocate(lines: list[str]) -> list[str]:
    """Live scripts sat in ~/.local/bin; the kit's sit under the libexec scripts dir."""
    out = []
    for ln in lines:
        ln = ln.replace("file:///LIVE/bin/", f"file://{LIBEXEC}/scripts/openclaw/")
        ln = re.sub(r"^ExecStart=/LIVE/bin/(\S+)", rf"ExecStart=/bin/bash {LIBEXEC}/scripts/openclaw/\1", ln)
        out.append(ln)
    return out


def test_there_is_one_template_per_unit_and_nothing_else():
    on_disk = {p.name[: -len(".template")] for p in TEMPLATES.glob("*.template")}
    assert on_disk == set(host.UNIT_NAMES) and len(host.UNIT_NAMES) == 10


def test_the_gateway_unit_is_never_templated():
    assert not (TEMPLATES / "openclaw-gateway.service.template").exists()
    assert "openclaw-gateway.service" not in host.UNIT_NAMES


@pytest.mark.parametrize("name", [n for n in host.UNIT_NAMES if n != "openclaw-verify.service"])
def test_a_rendered_unit_matches_the_live_one_apart_from_the_script_location(name):
    rendered = host.render_unit(name, host.unit_markers(LIBEXEC))
    assert _directives(rendered) == _relocate(_directives((LIVE / name).read_text(encoding="utf-8")))


def test_the_verify_unit_defers_the_notice_to_the_script():
    rendered = host.render_unit("openclaw-verify.service", host.unit_markers(LIBEXEC))
    exec_lines = [ln for ln in rendered.splitlines() if ln.startswith("ExecStart=")]
    assert exec_lines == [f"ExecStart=/bin/bash {LIBEXEC}/scripts/openclaw/openclaw-verify.sh --notify"]
    assert "message send" not in rendered  # no operator id or CLI path is frozen into the unit


def test_the_live_verify_unit_was_the_one_with_the_inline_notice():
    """Guards the fixture: if this stops being true the deviation above needs re-reading."""
    assert "message send" in (LIVE / "openclaw-verify.service").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", host.UNIT_NAMES)
def test_no_marker_survives_and_every_execstart_is_absolute_under_libexec(name):
    rendered = host.render_unit(name, host.unit_markers(LIBEXEC))
    assert "@" not in re.sub(r"openclaw-backup@", "", rendered)
    for ln in rendered.splitlines():
        if ln.startswith("ExecStart="):
            argv = ln.split("=", 1)[1].split()
            assert argv[0] == "/bin/bash" and argv[1].startswith(f"{LIBEXEC}/scripts/openclaw/")


@pytest.mark.parametrize("name", host.UNIT_NAMES)
def test_the_bare_script_path_is_never_the_program(name):
    """The exec bit is not load-bearing: brew installs these files 0644."""
    rendered = host.render_unit(name, host.unit_markers(LIBEXEC))
    for ln in rendered.splitlines():
        if ln.startswith("ExecStart="):
            assert not ln.split("=", 1)[1].startswith("/opt/")


@pytest.mark.parametrize("name", host.TIMER_NAMES)
def test_timers_are_wanted_by_timers_target_and_point_at_a_real_service(name):
    text = host.render_unit(name, host.unit_markers(LIBEXEC))
    assert "WantedBy=timers.target" in text
    unit = re.search(r"^Unit=(\S+)$", text, re.M).group(1)
    service = unit.replace("@daily", "@").replace("@weekly", "@").replace("@monthly", "@")
    assert service in host.UNIT_NAMES
    if "OnCalendar" in text:
        assert "Persistent=true" in text


def test_a_missing_marker_raises_instead_of_leaving_a_literal(tmp_path):
    (tmp_path / "x.service.template").write_text("ExecStart=@NOPE@ run\n", encoding="utf-8")
    with pytest.raises(KeyError, match="NOPE"):
        host.render_unit("x.service", {"LIBEXEC": LIBEXEC}, tmp_path)


def test_a_kit_path_with_whitespace_is_refused():
    with pytest.raises(ValueError):
        host.unit_markers("/opt/my kit/libexec")


def test_templates_carry_no_host_literal():
    for p in TEMPLATES.glob("*.template"):
        text = p.read_text(encoding="utf-8")
        for literal in ("/home/", "bitgandtter", "wildbit", "7961376547", "bithome"):
            assert literal not in text, f"{p.name}: {literal}"
        assert not [c for c in text if c.isalpha() and ord(c) > 127], p.name


# --- install ----------------------------------------------------------------------------------------------

class _Systemctl:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        return 0, ""


def test_install_writes_all_ten_units_and_reloads_once(tmp_path):
    rec = _Systemctl()
    res = host.install_units(tmp_path, markers=host.unit_markers(LIBEXEC), runner=rec)
    assert sorted(res["changed"]) == sorted(host.UNIT_NAMES) and res["reloaded"]
    assert {p.name for p in tmp_path.iterdir()} == set(host.UNIT_NAMES)
    assert rec.calls == [["systemctl", "--user", "daemon-reload"]]


def test_a_second_install_is_a_byte_level_no_op(tmp_path):
    rec = _Systemctl()
    markers = host.unit_markers(LIBEXEC)
    host.install_units(tmp_path, markers=markers, runner=rec)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    mtimes = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    rec.calls.clear()
    res = host.install_units(tmp_path, markers=markers, runner=rec)
    assert res["changed"] == [] and not res["reloaded"] and rec.calls == []
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()} == mtimes


def test_install_replaces_a_stale_unit_and_leaves_the_gateway_unit_alone(tmp_path):
    (tmp_path / "openclaw-gateway.service").write_text("[Service]\nExecStart=/x\n", encoding="utf-8")
    (tmp_path / "openclaw-watchdog.service").write_text("stale\n", encoding="utf-8")
    res = host.install_units(tmp_path, markers=host.unit_markers(LIBEXEC), runner=_Systemctl())
    assert "openclaw-watchdog.service" in res["changed"]
    assert (tmp_path / "openclaw-gateway.service").read_text(encoding="utf-8") == "[Service]\nExecStart=/x\n"
    assert LIBEXEC in (tmp_path / "openclaw-watchdog.service").read_text(encoding="utf-8")


def test_dry_run_writes_and_reloads_nothing(tmp_path):
    rec = _Systemctl()
    res = host.install_units(tmp_path, markers=host.unit_markers(LIBEXEC), dry_run=True, runner=rec)
    assert len(res["changed"]) == 10 and list(tmp_path.iterdir()) == [] and rec.calls == []


def test_enable_starts_only_the_timers_and_never_the_gateway(tmp_path):
    rec = _Systemctl()
    host.install_units(tmp_path, markers=host.unit_markers(LIBEXEC), enable=True, runner=rec)
    enabled = [c for c in rec.calls if "enable" in c]
    assert sorted(c[-1] for c in enabled) == sorted(host.TIMER_NAMES)
    assert not any("openclaw-gateway" in " ".join(c) for c in rec.calls)


def test_the_cli_registers_install_units():
    from ai_resources import cli
    args = cli.build_parser().parse_args(["openclaw", "install-units", "--render-only"])
    assert args.func is host.cmd_install_units and args.render_only


# --- kit-host.env ---------------------------------------------------------------------------------------------------

def test_host_env_round_trips_and_keeps_unrelated_lines(tmp_path):
    path = tmp_path / "kit-host.env"
    path.write_text("# mine\nCUSTOM=1\nOPENCLAW_NARRATION=milestones\n", encoding="utf-8")
    assert host.write_host_env({"OPENCLAW_NARRATION": "every-step", "OPENCLAW_GUARD": "1"}, path)
    assert path.read_text(encoding="utf-8") == "# mine\nCUSTOM=1\nOPENCLAW_NARRATION=every-step\nOPENCLAW_GUARD=1\n"
    assert host.read_host_env(path)["OPENCLAW_GUARD"] == "1"


def test_host_env_write_is_idempotent_and_none_removes_a_key(tmp_path):
    path = tmp_path / "kit-host.env"
    assert host.write_host_env({"A": "1"}, path)
    assert host.write_host_env({"A": "1"}, path) is False
    assert host.write_host_env({"A": None}, path)
    assert "A=" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["$(id)", "a;b", "`x`", "a\nb", '"q"'])
def test_host_env_refuses_values_bash_would_execute(tmp_path, bad):
    with pytest.raises(ValueError):
        host.write_host_env({"A": bad}, tmp_path / "e")
    assert not (tmp_path / "e").exists()


# --- GitOps backup templates (C08) ------------------------------------------------------------------------

GITOPS = REPO / "templates" / "gitops" / "openclaw-backups"
GITOPS_FILES = ("bucket.yaml", "uploader.yaml", "guard.yaml", "alerts.yaml")
# What the reference infrastructure calls things. None of it may appear in the kit: those values
# live in the infrastructure repo, and a template that carries them is a copy, not a template.
INFRA_LITERALS = ("wildbit", "bithome", "k3s-backup", "namespace: monitoring", "199022639860", "k3s-oidc", "us-central1",
                  "grafanacloud-prom", "100.76.", "192.168.")
FIXTURE_MARKERS = {
    "GCP_PROJECT": "acme-platform", "BUCKET_NAME": "acme-openclaw-backups", "BUCKET_LOCATION": "EUROPE-WEST1",
    "PROVIDER_CONFIG": "acme-gcp", "UPLOADER_SA_EMAIL": "backup-uploader@acme-platform.iam.gserviceaccount.com",
    "UPLOADER_KSA": "backup-uploader", "CRED_CONFIGMAP": "backup-uploader-cred", "WIF_PROJECT_NUMBER": "123456789012",
    "WIF_POOL": "acme-pool", "WIF_PROVIDER": "acme-oidc", "NODE_NAME": "node-a", "BACKUP_DIR": "/srv/openclaw-backups",
    "NAMESPACE": "observability", "GRAFANA_FOLDER_UID": "alerting", "PROM_DATASOURCE_UID": "prom-uid",
    "ALERT_SERVICE": "acme-platform", "RUNBOOK_URL": "https://git.example.org/acme/gitops/blob/main/alerts.yaml",
}


def _gitops_docs() -> dict[str, list]:
    import yaml
    rendered = host.render_gitops_backups(FIXTURE_MARKERS)
    return {name: list(yaml.safe_load_all(text)) for name, text in rendered.items()}


def test_there_are_exactly_the_four_gitops_templates():
    assert {p.name for p in GITOPS.glob("*.template")} == {f"{n}.template" for n in GITOPS_FILES}


def test_the_fixture_covers_every_marker_and_nothing_extra():
    assert set(host.gitops_marker_names()) == set(FIXTURE_MARKERS)


@pytest.mark.parametrize("name", GITOPS_FILES)
def test_a_rendered_gitops_manifest_parses_and_leaves_no_marker(name):
    text = host.render_gitops_backups(FIXTURE_MARKERS)[name]
    assert not re.findall(r"@[A-Z][A-Z0-9_]*@", text)
    assert [d for d in _gitops_docs()[name] if d]


@pytest.mark.parametrize("path", sorted(GITOPS.glob("*.template")), ids=lambda p: p.name)
def test_the_gitops_templates_carry_no_infrastructure_literal_and_are_english(path):
    text = path.read_text(encoding="utf-8").lower()
    for literal in INFRA_LITERALS:
        assert literal not in text, f"{path.name}: {literal}"
    assert not [c for c in text if c.isalpha() and ord(c) > 127], path.name


def test_a_missing_gitops_marker_raises_instead_of_leaving_a_literal():
    partial = dict(FIXTURE_MARKERS)
    partial.pop("BUCKET_NAME")
    with pytest.raises(KeyError, match="BUCKET_NAME"):
        host.render_gitops_backups(partial)


@pytest.mark.parametrize("bad", ["two words", "quo'te", 'dq"', "new\nline", "", "a: b"])
def test_a_value_that_could_change_the_yaml_structure_is_refused(bad):
    with pytest.raises(ValueError):
        host.render_gitops_backups({**FIXTURE_MARKERS, "NODE_NAME": bad})


def test_the_bucket_keeps_per_prefix_lifecycle_and_never_deletes_the_data_with_the_manifest():
    bucket, member = _gitops_docs()["bucket.yaml"]
    assert bucket["spec"]["deletionPolicy"] == "Orphan" and member["spec"]["deletionPolicy"] == "Orphan"
    rules = bucket["spec"]["forProvider"]["lifecycleRule"]
    ages = {r["condition"]["matchesPrefix"][0]: r["condition"]["age"] for r in rules}
    assert ages == {"daily/": 8, "weekly/": 35, "monthly/": 100, "manual/": 8}
    assert bucket["spec"]["forProvider"]["uniformBucketLevelAccess"] is True
    assert bucket["spec"]["forProvider"]["forceDestroy"] is False


def test_the_iam_reuses_an_existing_service_account_scoped_to_this_bucket_only():
    _bucket, member = _gitops_docs()["bucket.yaml"]
    forp = member["spec"]["forProvider"]
    assert forp["role"] == "roles/storage.objectAdmin"
    assert forp["bucketRef"]["name"] == FIXTURE_MARKERS["BUCKET_NAME"]
    assert forp["member"] == "serviceAccount:" + FIXTURE_MARKERS["UPLOADER_SA_EMAIL"]
    kinds = {d["kind"] for docs in _gitops_docs().values() for d in docs if d}
    assert "ServiceAccount" in kinds  # only the guard's own, read-only
    assert not {"GoogleServiceAccount", "ServiceAccountKey", "Secret"} & kinds


def test_there_is_no_json_key_anywhere():
    for path in GITOPS.glob("*.template"):
        text = path.read_text(encoding="utf-8")
        for needle in ("private_key", "GOOGLE_APPLICATION_CREDENTIALS", "service_account.json", "kind: Secret"):
            assert needle not in text, f"{path.name}: {needle}"
    uploader = _gitops_docs()["uploader.yaml"][0]
    volumes = {v["name"]: v for v in uploader["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]}
    token = volumes["gcp-wif-token"]["projected"]["sources"][0]["serviceAccountToken"]
    assert token["audience"].startswith("https://iam.googleapis.com/projects/123456789012/locations/global/"
                                        "workloadIdentityPools/acme-pool/providers/acme-oidc")


def _uploader_script() -> str:
    uploader = _gitops_docs()["uploader.yaml"][0]
    return uploader["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]


def test_the_uploader_skips_a_tarball_without_its_checksum_and_asserts_the_daily_arrived():
    script = _uploader_script()
    assert '[ -f "$f.sha256" ] ||' in script and "continue" in script.split('[ -f "$f.sha256" ]')[1].splitlines()[0]
    assert script.index("gcloud storage cp \"$f\"") < script.index("gcloud storage cp \"$f.sha256\""), \
        "the checksum is uploaded after the tarball"
    tail = script.split("newest=")[1]
    assert 'gcloud storage ls "$BUCKET/daily/$(basename "$newest")"' in tail and "exit 1" in tail
    assert "set -eu" in script and "|| true" not in script.replace("2>/dev/null | head -1 || true", "")
    assert "gcloud storage ls" in script.split("UPLOAD")[0], "idempotent: it looks before it copies"


def test_the_uploader_mounts_the_backups_read_only():
    uploader = _gitops_docs()["uploader.yaml"][0]
    pod = uploader["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["backups"]["readOnly"] is True and pod["automountServiceAccountToken"] is False


def test_the_guard_is_hard_on_daily_and_weekly_and_soft_on_monthly():
    guard = _gitops_docs()["guard.yaml"][1]
    script = guard["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert 'LIMITS = {"daily": 36, "weekly": 216, "monthly": 780}' in script
    assert 'SOFT = {"monthly"}' in script and "MIN_MB = 5.0" in script and ".sha256" in script
    pod = guard["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["containers"][0]["volumeMounts"][0]["readOnly"] is True


def test_the_alert_models_are_valid_json_and_watch_both_cronjobs_for_failure_and_silence():
    import json
    (group,) = _gitops_docs()["alerts.yaml"]
    rules = group["spec"]["forProvider"]["rule"]
    assert len(rules) == 4
    exprs = []
    for rule in rules:
        models = [json.loads(q["model"]) for q in rule["data"]]
        assert [m["refId"] for m in models] == ["A", "B", "C"] and rule["condition"] == "C"
        exprs.append(models[0]["expr"])
        assert rule["data"][0]["datasourceUid"] == FIXTURE_MARKERS["PROM_DATASOURCE_UID"]
    joined = "\n".join(exprs)
    for cronjob in ("openclaw-backup-guard", "openclaw-backup-uploader"):
        assert joined.count(cronjob) == 3  # failure uses it twice, silence once
    assert "kube_job_status_failed" not in joined, "a failed Job object keeps that alert firing forever"
    assert sum("} - kube_cronjob_status_last_successful_time" in e for e in exprs) == 2 and sum("last_(successful|schedule)" in e for e in exprs) == 2


def test_render_gitops_backups_writes_four_files_and_lists_markers(tmp_path, capsys):
    import argparse
    args = argparse.Namespace(list_markers=True, set=[], out="")
    assert host.cmd_render_gitops_backups(args) == 0
    assert "WIF_POOL" in capsys.readouterr().out
    args = argparse.Namespace(list_markers=False, set=[f"{k}={v}" for k, v in FIXTURE_MARKERS.items()],
                              out=str(tmp_path / "out"))
    assert host.cmd_render_gitops_backups(args) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(GITOPS_FILES)
    args.set = ["BUCKET_NAME=x"]
    assert host.cmd_render_gitops_backups(args) == 2
    args.set = ["novalue"]
    assert host.cmd_render_gitops_backups(args) == 2
