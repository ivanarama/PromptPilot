"""Actual temporary registry/key files and GET-only loopback HTTP fixtures."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from promptpilot import task_source_settings as settings

REAL_WINDOWS_ACL = settings._windows_acl


@pytest.fixture(autouse=True)
def private_storage(monkeypatch):
    # CI POSIX exercises actual uid/mode checks. Portable Windows fixtures mock
    # only ACL transport, not registry locking/CAS/files or HTTP operations.
    if os.name == "nt":
        monkeypatch.setattr(settings, "_windows_acl", lambda *a, **kw: None)


@pytest.fixture
def registry(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path / "task-sources.json"


def request(path, source_id="studio", **updates):
    return settings.SourceUpdate(id=source_id, label="Studio", base_url="https://studio.example.invalid",
                                 revision=settings.get_settings(sources_file=path)["revision"], **updates)


def store(path, entries):
    path.write_text(json.dumps({"version": 1, "sources": entries}), encoding="utf-8")


def saved(path):
    return json.loads(path.read_bytes())["sources"]


def assert_no_private(response, *extra):
    text = json.dumps(response)
    for secret in ["WRITE-ONLY-SECRET", "ROTATED-SECRET", "PRIVATE_ENV_NAME", "token_file", "token_env", *extra]:
        assert secret not in text


@contextmanager
def peer(reply):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path, self.headers.get("Authorization")))
            status, value = reply(self.path)
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_POST(self):
            requests.append((self.command, self.path, None))
            self.send_error(405)
        def log_message(self, *unused):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_missing_optional_registry_is_editable_readonly(registry, monkeypatch):
    monkeypatch.setattr(settings.peers, "_Client", lambda *_: pytest.fail("GET settings must not probe"))
    response = settings.get_settings(sources_file=registry)
    assert response == {"revision": "missing", "editable": True, "sources": [], "errors": []}
    assert list(registry.parent.iterdir()) == []


def test_managed_secret_create_keep_rotate_unbind_delete(registry):
    response = settings.upsert_source("studio", request(registry, token="WRITE-ONLY-SECRET"), sources_file=registry)
    descriptor = response["sources"][0]
    assert descriptor["has_token"] and descriptor["auth_kind"] == "managed"
    first = Path(saved(registry)[0]["token_file"])
    assert first.read_text() == "WRITE-ONLY-SECRET"
    assert first.parent == registry.parent / (registry.name + ".credentials")
    if os.name != "nt":
        assert first.stat().st_mode & 0o777 == 0o600
        assert first.parent.stat().st_mode & 0o777 == 0o700
    assert_no_private(response, str(first))
    keep = request(registry, token="").model_copy(update={"label": "Renamed studio"})
    response = settings.upsert_source("studio", keep, sources_file=registry)
    assert saved(registry)[0]["token_file"] == str(first)
    assert len(list(first.parent.iterdir())) == 1
    response = settings.upsert_source("studio", request(registry, token="ROTATED-SECRET"), sources_file=registry)
    second = Path(saved(registry)[0]["token_file"])
    assert second != first and first.read_text() == "WRITE-ONLY-SECRET"
    response = settings.upsert_source("studio", request(registry, auth_mode="none"), sources_file=registry)
    assert not response["sources"][0]["has_token"] and "token_file" not in saved(registry)[0]
    response = settings.delete_source("studio", response["revision"], sources_file=registry)
    assert response["sources"] == [] and first.exists() and second.exists()
    assert_no_private(response)


def test_external_env_and_file_are_compatible_never_exposed_or_deleted(registry):
    external = registry.parent / "external.key"
    external.write_text("WRITE-ONLY-SECRET")
    entries = [{"id": "studio", "label": "Studio", "base_url": "https://studio.example.invalid", "token_env": "PRIVATE_ENV_NAME"},
               {"id": "other", "label": "Other", "base_url": "https://other.example.invalid", "token_file": "external.key"}]
    store(registry, entries)
    response = settings.get_settings(sources_file=registry)
    assert [s["auth_kind"] for s in response["sources"]] == ["env", "file"]
    assert_no_private(response, str(external))
    settings.upsert_source("studio", request(registry), sources_file=registry)
    assert saved(registry) == entries
    response = settings.delete_source("other", settings.get_settings(sources_file=registry)["revision"], sources_file=registry)
    assert external.read_text() == "WRITE-ONLY-SECRET" and len(response["sources"]) == 1


def test_stale_revision_never_creates_credential_or_drops_other_record(registry):
    stale = request(registry, token="WRITE-ONLY-SECRET")
    other = {"id": "other", "label": "Other", "base_url": "https://other.example.invalid"}
    store(registry, [other])
    before = registry.read_bytes()
    with pytest.raises(settings.SettingsError) as failure:
        settings.upsert_source("studio", stale, sources_file=registry)
    assert failure.value.status_code == 409 and registry.read_bytes() == before
    assert not registry.with_name(registry.name + ".credentials").exists()


@pytest.mark.parametrize("raw", [
    b'{"version":1,"sources":[],"unknown":true}', b'{"sources":[null]}',
    b'{"sources":[{"id":"broken"}]}', b'{"sources":[],"sources":[]}',
    b'{"version":99,"sources":[]}', b'{broken',
    b'{"sources":[{"id":"a","label":"A","base_url":"https://a.test","unknown":1}]}',
    b'{"sources":[{"id":"a","label":"A","base_url":"https://a.test"},{"id":"a","label":"A","base_url":"https://a.test"}]}',
])
def test_malformed_registry_get_noneditable_and_mutations_preserve_raw(registry, raw):
    registry.write_bytes(raw)
    response = settings.get_settings(sources_file=registry)
    assert not response["editable"] and response["errors"]
    draft = settings.SourceUpdate(id="studio", label="Studio", base_url="https://studio.test", revision="missing")
    for operation in [lambda: settings.upsert_source("studio", draft, sources_file=registry),
                      lambda: settings.delete_source("studio", "missing", sources_file=registry)]:
        with pytest.raises(settings.SettingsError):
            operation()
        assert registry.read_bytes() == raw


def test_limit_and_unrelated_entry_preservation(registry):
    entries = [{"id": f"source{i}", "label": f"Source {i}", "base_url": f"https://source{i}.test"} for i in range(16)]
    store(registry, entries)
    with pytest.raises(settings.SettingsError, match="предел"):
        settings.upsert_source("studio", request(registry), sources_file=registry)
    update = settings.SourceUpdate(**entries[0], revision=settings.get_settings(sources_file=registry)["revision"])
    settings.upsert_source("source0", update.model_copy(update={"label": "Updated"}), sources_file=registry)
    assert saved(registry)[1:] == entries[1:]


@pytest.mark.parametrize("url", ["http://remote.example.test", "https://user:pass@example.test", "https://example.test/?token=x", "http://127.0.0.1:8420"])
def test_invalid_or_local_urls_fail_before_secret_write(registry, url, monkeypatch):
    monkeypatch.setattr(settings.peers.config, "PORT", 8420)
    update = request(registry, token="WRITE-ONLY-SECRET").model_copy(update={"base_url": url})
    with pytest.raises(settings.SettingsError):
        settings.upsert_source("studio", update, sources_file=registry)
    assert not registry.exists() and not registry.with_name(registry.name + ".credentials").exists()


def test_url_change_needs_new_token_or_explicit_none_for_save_and_draft(registry):
    settings.upsert_source("studio", request(registry, token="WRITE-ONLY-SECRET"), sources_file=registry)
    update = request(registry).model_copy(update={"base_url": "https://different.test"})
    before = registry.read_bytes()
    for action in [lambda: settings.upsert_source("studio", update, sources_file=registry),
                   lambda: settings.probe_source(draft=settings.SourceDraft(**update.model_dump(exclude={"revision"})), sources_file=registry)]:
        with pytest.raises(settings.SettingsError, match="нового адреса"):
            action()
    assert registry.read_bytes() == before
    settings.upsert_source("studio", update.model_copy(update={"auth_mode": "none"}), sources_file=registry)
    assert not settings.get_settings(sources_file=registry)["sources"][0]["has_token"]


def test_modern_draft_probe_get_only_and_secret_never_persisted(registry):
    with peer(lambda _: (200, {"items": [], "total": 0})) as (url, calls):
        response = settings.probe_source(draft=settings.SourceDraft(id="studio", label="Studio", base_url=url,
                                          token="WRITE-ONLY-SECRET"), sources_file=registry)
    assert response == {"status": "online", "message": "Полный список задач доступен", "api": "task-feed",
                        "capabilities": {"tasks": True, "workflows": True}}
    assert len(calls) == 1 and calls[0][0] == "GET" and "local_only=true" in calls[0][1]
    assert calls[0][2] == "Bearer WRITE-ONLY-SECRET"
    assert not registry.exists() and list(registry.parent.iterdir()) == []
    assert_no_private(response)


def test_legacy_saved_probe_404_only_clear_reason_and_no_mutation(registry, monkeypatch):
    monkeypatch.setenv("PRIVATE_ENV_NAME", "WRITE-ONLY-SECRET")
    with peer(lambda p: (404, {}) if p.startswith('/api/task-feed') else (200, [{"id": "wf_one"}])) as (url, calls):
        store(registry, [{"id": "studio", "label": "Studio", "base_url": url, "token_env": "PRIVATE_ENV_NAME"}])
        before = registry.read_bytes()
        response = settings.probe_source(source_id="studio", sources_file=registry)
    assert response["api"] == "workflows" and response["status"] == "partial"
    assert response["capabilities"] == {"tasks": False, "workflows": True}
    assert response["message"] == settings.peers.LEGACY_MESSAGE
    assert len(calls) == 2 and all(c[0] == "GET" for c in calls)
    assert registry.read_bytes() == before
    assert_no_private(response)


def test_non404_probe_never_falls_back_and_invalid_modern_is_not_empty(registry):
    for reply in [(401, {"secret": "WRITE-ONLY-SECRET"}), (200, {"items": [], "total": 3})]:
        with peer(lambda _, r=reply: r) as (url, calls):
            response = settings.probe_source(draft=settings.SourceDraft(id="studio", label="Studio", base_url=url), sources_file=registry)
        assert response["status"] == "unavailable" and response["api"] is None and len(calls) == 1
        assert_no_private(response)


def test_registry_replace_unknown_keeps_referenced_key_no_rollback(registry, monkeypatch):
    real_replace = settings.os.replace
    def lost_ack(old, new):
        real_replace(old, new)
        raise OSError("simulated acknowledgement loss")
    monkeypatch.setattr(settings.os, "replace", lost_ack)
    with pytest.raises(settings.SettingsError) as failure:
        settings.upsert_source("studio", request(registry, token="WRITE-ONLY-SECRET"), sources_file=registry)
    assert failure.value.status_code == 503
    key = Path(saved(registry)[0]["token_file"])
    assert key.read_text() == "WRITE-ONLY-SECRET"
    assert settings.get_settings(sources_file=registry)["sources"][0]["has_token"]


def test_late_external_registry_change_rejected_not_overwritten(registry, monkeypatch):
    update = request(registry, token="WRITE-ONLY-SECRET")
    real_exclusive = settings._exclusive
    external = b'{"version":1,"sources":[]}\n'
    def drift(path, raw):
        real_exclusive(path, raw)
        if path.suffix == '.writing':
            registry.write_bytes(external)
    monkeypatch.setattr(settings, "_exclusive", drift)
    with pytest.raises(settings.SettingsError) as failure:
        settings.upsert_source("studio", update, sources_file=registry)
    assert failure.value.status_code == 409 and registry.read_bytes() == external
    keys = list(registry.with_name(registry.name + ".credentials").glob('*.key'))
    assert len(keys) == 1 and keys[0].read_text() == "WRITE-ONLY-SECRET"


def test_cross_process_writer_lock_is_not_bypassed(registry):
    code = '''import sys
from pathlib import Path
from promptpilot import task_source_settings as m
m._windows_acl=lambda *a,**k:None
with m._locked(Path(sys.argv[1])):
 print('locked',flush=True)
 sys.stdin.readline()
'''
    child = subprocess.Popen([sys.executable, '-B', '-c', code, str(registry)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(settings.SettingsError) as failure:
            settings.upsert_source('studio', request(registry), sources_file=registry)
        assert failure.value.status_code == 409 and not registry.exists()
    finally:
        child.communicate('\n', timeout=15)
    assert child.returncode == 0 and registry.with_name(registry.name + '.lock').exists()


def test_symlink_registry_refuses_writes_without_touching_target(registry):
    target = registry.parent / 'target.json'
    target.write_text('{"version":1,"sources":[]}')
    try:
        registry.symlink_to(target)
    except OSError:
        pytest.skip('OS account cannot create symbolic links')
    before = target.read_bytes()
    assert not settings.get_settings(sources_file=registry)["editable"]
    with pytest.raises(settings.SettingsError):
        settings.delete_source('studio', 'missing', sources_file=registry)
    assert target.read_bytes() == before


def test_probe_requires_exact_saved_or_draft_choice(registry):
    for kwargs in [{}, {'source_id': 'unknown', 'draft': settings.SourceDraft(id='studio', label='Studio', base_url='https://studio.test')}]:
        with pytest.raises(settings.SettingsError):
            settings.probe_source(**kwargs, sources_file=registry)
    with pytest.raises(settings.SettingsError) as failure:
        settings.probe_source(source_id='unknown', sources_file=registry)
    assert failure.value.status_code == 404


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows ACL implementation")
def test_real_windows_inherited_temp_acl_public_save_works_without_changing_parent(registry, monkeypatch):
    monkeypatch.setattr(settings, "_windows_acl", REAL_WINDOWS_ACL)
    parent_acl = subprocess.check_output(['icacls', str(registry.parent)])
    assert settings.get_settings(sources_file=registry)['editable']
    response = settings.upsert_source('studio', request(registry, token='WRITE-ONLY-SECRET'), sources_file=registry)
    assert response['editable'] and response['sources'][0]['has_token']
    key = Path(saved(registry)[0]['token_file'])
    settings._private(key.parent, directory=True)
    settings._private(key)
    assert subprocess.check_output(['icacls', str(registry.parent)]) == parent_acl
    assert_no_private(response, str(key))
