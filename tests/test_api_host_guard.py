"""The unauthenticated API must not be reachable through the user's browser.

Without PP_API_TOKEN the loopback bind is the only boundary. DNS rebinding
makes a hostile page same-origin with the server under the attacker's own
hostname, and a page on another localhost port is merely "same-site", so the
old Sec-Fetch-Site=cross-site check alone let both create a provider with an
arbitrary command.
"""

import asyncio

import httpx
import pytest

from promptpilot import api


def call(method, path, *, base_url="http://127.0.0.1:8420", headers=None, **kwargs):
    async def _run():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            return await client.request(method, path, headers=headers, **kwargs)

    return asyncio.run(_run())


@pytest.fixture
def no_token(monkeypatch, isolated_db):
    monkeypatch.setattr(api, "API_TOKEN", "")
    monkeypatch.setattr(api, "ALLOWED_HOSTS", [])
    return isolated_db


def test_rebinding_host_cannot_create_provider_or_read_tasks(no_token):
    rebinding = {"Host": "attacker.example:8420", "Sec-Fetch-Site": "same-origin"}

    created = call("POST", "/api/providers", headers=rebinding,
                   json={"name": "probe", "cmd": "echo {prompt}"})
    listed = call("GET", "/api/tasks", headers=rebinding)

    assert created.status_code == 403
    assert listed.status_code == 403
    assert "probe" not in call("GET", "/api/providers").json()


@pytest.mark.parametrize("host", [
    "127.0.0.1:8420",
    "localhost:8420",
    "localhost:9000",        # ssh -L 9000:127.0.0.1:8420
    "[::1]:8420",
    "pp.localhost:8420",
    "192.168.1.5:8420",      # IP literal: a rebinding page cannot send it
])
def test_local_names_and_ip_literals_are_served(no_token, host):
    assert call("GET", "/api/tasks", headers={"Host": host}).status_code == 200


def test_other_localhost_port_cannot_post(no_token):
    response = call(
        "POST", "/api/tasks", json={"prompt": "x"},
        headers={"Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-site",
                 "Host": "localhost:8420"},
    )

    assert response.status_code == 403
    assert call("GET", "/api/tasks").json() == []


def test_own_page_and_scripts_can_post(no_token):
    from_ui = call("POST", "/api/tasks", json={"prompt": "from ui"},
                   headers={"Origin": "http://127.0.0.1:8420"})
    from_script = call("POST", "/api/tasks", json={"prompt": "from curl"})

    assert from_ui.status_code == 201
    assert from_script.status_code == 201


def test_null_origin_is_refused(no_token):
    response = call("POST", "/api/tasks", json={"prompt": "x"}, headers={"Origin": "null"})

    assert response.status_code == 403


def test_allowed_hosts_cover_reverse_proxy_names(no_token, monkeypatch):
    monkeypatch.setattr(api, "ALLOWED_HOSTS", ["pp.example.com"])

    direct = call("GET", "/api/tasks", headers={"Host": "pp.example.com"})
    # The proxy talks to the loopback upstream; the browser keeps the public Origin.
    proxied = call("POST", "/api/tasks", json={"prompt": "via proxy"},
                   headers={"Origin": "https://pp.example.com"})
    other = call("GET", "/api/tasks", headers={"Host": "evil.example.com"})

    assert direct.status_code == 200
    assert proxied.status_code == 201
    assert other.status_code == 403


def test_wildcard_disables_only_the_host_check(no_token, monkeypatch):
    monkeypatch.setattr(api, "ALLOWED_HOSTS", ["*"])

    read = call("GET", "/api/tasks", headers={"Host": "anything.example"})
    foreign_post = call("POST", "/api/tasks", json={"prompt": "x"},
                        headers={"Host": "anything.example", "Origin": "http://other.example"})

    assert read.status_code == 200
    assert foreign_post.status_code == 403


def test_token_mode_is_unchanged(isolated_db, monkeypatch):
    monkeypatch.setattr(api, "API_TOKEN", "secret-token")
    monkeypatch.setattr(api, "ALLOWED_HOSTS", [])
    headers = {"Host": "pp.lan:8420"}

    anonymous = call("GET", "/api/tasks", headers=headers)
    authorized = call("GET", "/api/tasks",
                      headers={**headers, "Authorization": "Bearer secret-token"})

    assert anonymous.status_code == 401
    assert authorized.status_code == 200


def test_cross_site_post_is_refused_even_with_token(isolated_db, monkeypatch):
    monkeypatch.setattr(api, "API_TOKEN", "secret-token")

    response = call("POST", "/api/tasks", json={"prompt": "x"},
                    headers={"Authorization": "Bearer secret-token",
                             "Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403


@pytest.mark.parametrize(("authority", "expected"), [
    ("127.0.0.1:8420", "127.0.0.1"),
    ("LOCALHOST", "localhost"),
    ("[::1]:8420", "::1"),
    ("::1", "::1"),
    ("pp.example.com.", "pp.example.com"),
    ("", ""),
])
def test_hostname_parsing(authority, expected):
    assert api._hostname(authority) == expected
