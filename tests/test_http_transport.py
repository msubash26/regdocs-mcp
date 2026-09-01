"""Tests for the Streamable HTTP transport and the resource-server auth half.

Driven through `httpx2.ASGITransport` against the same `http_app()` the process
serves, so they run in CI with no network and no live port.

Two kinds of test live here, and the distinction is deliberate (ADR-006):

  - **Guards on the SDK.** Spec 2026-07-28 makes the header ladder, the 405 and
    the Origin check MUSTs, and `mcp` 2.1.1 already enforces all of them. We do
    not reimplement those; we pin them, so an SDK upgrade that quietly drops one
    fails here rather than in production.
  - **Guards on our own code.** Statelessness, the empty origin allowlist, and
    every auth assertion below are ours.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from contextlib import asynccontextmanager

import httpx2
import pytest

from regdocs_mcp import auth as auth_mod
from regdocs_mcp.server import http_app

pytestmark = pytest.mark.anyio

PV = "2026-07-28"
HOST = "127.0.0.1"
PORT = 8000
PATH = "/mcp"
BASE = f"http://{HOST}:{PORT}"
RESOURCE = f"{BASE}{PATH}"
PRM_URL = f"/.well-known/oauth-protected-resource{PATH}"

# 40 bytes: over RFC 7518 §3.2's 32-byte HS256 floor, which the verifier enforces.
SECRET = "test-signing-secret-not-a-real-one-12345"

EXPECTED_TOOLS = ["search_notices", "get_document_section", "list_obligations", "diff_versions"]

_ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": PV,
    "io.modelcontextprotocol/clientCapabilities": {},
}


def body(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    """A well-formed modern request. The envelope is required on every one."""
    merged = dict(params or {})
    merged["_meta"] = _ENVELOPE
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": merged}


def headers(
    method: str | None, *, name: str | None = None, pv: str | None = PV, **extra: str
) -> dict:
    h = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "host": f"{HOST}:{PORT}",
    }
    if pv is not None:
        h["mcp-protocol-version"] = pv
    if method is not None:
        h["mcp-method"] = method
    if name is not None:
        h["mcp-name"] = name
    h.update({k.replace("_", "-"): v for k, v in extra.items()})
    return h


@asynccontextmanager
async def asgi_client(app):
    """An httpx client bound to `app`, with the app's lifespan entered."""
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as client:
            yield client


@pytest.fixture
async def http(server):
    """Unauthenticated HTTP app. `server` points the module at the fixture index."""
    async with asgi_client(http_app(path=PATH, host=HOST, port=PORT)) as client:
        yield client


@pytest.fixture
async def secured(server):
    """The same app with `--auth` on."""
    app = http_app(path=PATH, host=HOST, port=PORT, auth_secret=SECRET)
    async with asgi_client(app) as client:
        yield client


def token(**overrides) -> str:
    """A valid token unless an override makes it otherwise."""
    kwargs = {
        "secret": SECRET,
        "issuer": auth_mod.DEFAULT_ISSUER,
        "audience": RESOURCE,
        "scopes": [auth_mod.DEFAULT_SCOPE],
    }
    kwargs.update(overrides)
    return auth_mod.mint_token(**kwargs)


def bearer(tok: str) -> dict:
    return headers("tools/list", authorization=f"Bearer {tok}")


def error_code(response: httpx2.Response) -> int | None:
    return json.loads(response.text).get("error", {}).get("code")


def challenge(response: httpx2.Response) -> dict[str, str]:
    """Parse `WWW-Authenticate: Bearer k="v", k="v"` into a dict."""
    raw = response.headers.get("www-authenticate", "")
    assert raw.startswith("Bearer "), raw
    return dict(re.findall(r'(\w+)="([^"]*)"', raw))


class TestMethodRestriction:
    """2026-07-28 is POST-only; GET and DELETE were removed with the session."""

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    async def test_rejected_with_allow_post(self, http, method):
        response = await http.request(method, PATH, headers=headers("tools/list"))
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"


class TestHeaderLadder:
    """SDK guards. Each is a spec MUST we pin rather than reimplement."""

    async def test_happy_path(self, http):
        response = await http.post(PATH, json=body("tools/list"), headers=headers("tools/list"))
        assert response.status_code == 200
        names = [t["name"] for t in json.loads(response.text)["result"]["tools"]]
        assert sorted(names) == sorted(EXPECTED_TOOLS)

    async def test_mcp_method_must_match_the_body(self, http):
        response = await http.post(PATH, json=body("tools/list"), headers=headers("resources/list"))
        assert response.status_code == 400
        assert error_code(response) == -32020

    async def test_mcp_method_is_required(self, http):
        response = await http.post(PATH, json=body("tools/list"), headers=headers(None))
        assert response.status_code == 400
        assert error_code(response) == -32020

    async def test_mcp_name_must_match_the_called_tool(self, http):
        call = body("tools/call", {"name": "search_notices", "arguments": {"query": "x"}})
        response = await http.post(
            PATH, json=call, headers=headers("tools/call", name="diff_versions")
        )
        assert response.status_code == 400
        assert error_code(response) == -32020

    async def test_protocol_version_header_must_match_the_envelope(self, http):
        """`2026-01-01`, not `2025-06-18`, and the difference is the point.

        Era routing reads this header first: a value in the *handshake* set
        (2024-11-05..2025-11-25) is dispatched to the legacy transport and never
        reaches the modern classifier, so it cannot produce -32020 no matter what
        the body says. Only an unrecognised value routes modern, where the header
        is then checked against the envelope.
        """
        response = await http.post(
            PATH, json=body("tools/list"), headers=headers("tools/list", pv="2026-01-01")
        )
        assert response.status_code == 400
        assert error_code(response) == -32020

    async def test_a_handshake_version_header_routes_to_the_legacy_path(self, http):
        """The other half of the routing rule, pinned so the asymmetry is visible."""
        response = await http.post(
            PATH, json=body("tools/list"), headers=headers("tools/list", pv="2025-06-18")
        )
        assert response.status_code == 200
        assert "mcp-session-id" not in response.headers

    async def test_request_envelope_is_required(self, http):
        naked = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        response = await http.post(PATH, json=naked, headers=headers("tools/list"))
        assert response.status_code == 400
        assert error_code(response) == -32602

    async def test_unknown_method_is_404_not_a_bare_404(self, http):
        """404 distinguishes an unknown method from a legacy server with no endpoint."""
        response = await http.post(PATH, json=body("no/such"), headers=headers("no/such"))
        assert response.status_code == 404
        assert error_code(response) == -32601


class TestSessionsAreGone:
    """The regression that would silently reintroduce a removed protocol feature."""

    async def test_session_header_is_ignored_and_not_echoed(self, http):
        response = await http.post(
            PATH,
            json=body("tools/list"),
            headers=headers("tools/list", mcp_session_id="abc123"),
        )
        assert response.status_code == 200
        assert "mcp-session-id" not in response.headers

    async def test_a_header_less_request_mints_no_session(self, http):
        """The test that actually carries the claim (ADR-006).

        SDK era-routing is by header alone: a POST with no `MCP-Protocol-Version`
        falls through to the legacy stateful transport, which mints and echoes a
        session ID. Probing only the modern path passes on a server that mints
        sessions on every legacy call, which is why this one is header-less.
        """
        response = await http.post(
            PATH,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-probe", "version": "0"},
                },
            },
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "host": f"{HOST}:{PORT}",
            },
        )
        assert "mcp-session-id" not in response.headers


class TestOriginValidation:
    """DNS-rebinding defence. The allowlist starts empty — stricter than the SDK."""

    async def test_absent_origin_passes(self, http):
        """Claude Code and curl send no Origin; they must not need one."""
        response = await http.post(PATH, json=body("tools/list"), headers=headers("tools/list"))
        assert response.status_code == 200

    async def test_foreign_origin_is_refused(self, http):
        response = await http.post(
            PATH,
            json=body("tools/list"),
            headers=headers("tools/list", origin="http://evil.example"),
        )
        assert response.status_code == 403

    async def test_localhost_origin_is_refused_by_default(self, http):
        """The SDK's own default allows `http://localhost:*`; ours does not.

        On this box that would include the LangFuse UI on :3000, so any page
        served there could drive the server from a browser.
        """
        response = await http.post(
            PATH,
            json=body("tools/list"),
            headers=headers("tools/list", origin="http://localhost:3000"),
        )
        assert response.status_code == 403

    async def test_allow_origin_widens_deliberately(self, server):
        app = http_app(path=PATH, host=HOST, port=PORT, allow_origins=["http://localhost:3000"])
        async with asgi_client(app) as client:
            permitted = await client.post(
                PATH,
                json=body("tools/list"),
                headers=headers("tools/list", origin="http://localhost:3000"),
            )
            other = await client.post(
                PATH,
                json=body("tools/list"),
                headers=headers("tools/list", origin="http://evil.example"),
            )
        assert permitted.status_code == 200
        assert other.status_code == 403


class TestAuthIsOptional:
    async def test_no_auth_server_serves_without_a_token(self, http):
        response = await http.post(PATH, json=body("tools/list"), headers=headers("tools/list"))
        assert response.status_code == 200

    async def test_no_auth_server_publishes_no_resource_metadata(self, http):
        assert (await http.get(PRM_URL)).status_code == 404


class TestProtectedResourceMetadata:
    """RFC 9728. The document a 401 points a client at."""

    async def test_document_is_served_and_well_formed(self, secured):
        response = await secured.get(PRM_URL)
        assert response.status_code == 200
        doc = json.loads(response.text)
        assert doc["resource"] == RESOURCE
        assert doc["scopes_supported"] == [auth_mod.DEFAULT_SCOPE]
        assert len(doc["authorization_servers"]) == 1

    async def test_the_published_issuer_is_one_this_server_accepts(self, secured):
        """The one-character bug (ADR-007).

        The document renders the issuer through pydantic's `AnyHttpUrl`, which
        appends a trailing slash to a path-less authority, while RFC 8414 §2
        makes issuer comparison exact. A client that trusted this document and
        presented a token with that exact `iss` was rejected by the server that
        published it.
        """
        published = json.loads((await secured.get(PRM_URL)).text)["authorization_servers"][0]
        response = await secured.post(
            PATH, json=body("tools/list"), headers=bearer(token(issuer=published))
        )
        assert response.status_code == 200

    async def test_a_foreign_issuer_is_still_refused(self, secured):
        response = await secured.post(
            PATH, json=body("tools/list"), headers=bearer(token(issuer="http://evil.example"))
        )
        assert response.status_code == 401


class TestBearerAuth:
    async def test_valid_token_reaches_the_tools(self, secured):
        response = await secured.post(PATH, json=body("tools/list"), headers=bearer(token()))
        assert response.status_code == 200
        names = [t["name"] for t in json.loads(response.text)["result"]["tools"]]
        assert sorted(names) == sorted(EXPECTED_TOOLS)

    async def test_missing_token_challenges_with_resource_metadata(self, secured):
        response = await secured.post(PATH, json=body("tools/list"), headers=headers("tools/list"))
        assert response.status_code == 401
        parsed = challenge(response)
        assert parsed["error"] == "invalid_token"
        assert parsed["resource_metadata"].endswith(PRM_URL)
        # RFC 6750 §3.1 — the SDK omits this; ScopeChallengeMiddleware adds it.
        assert parsed["scope"] == auth_mod.DEFAULT_SCOPE

    async def test_token_for_another_resource_is_refused(self, secured):
        """The confused-deputy case: the single most important test in this file.

        The token is correctly signed by the right issuer and carries the right
        scope. Only its audience is wrong. A server that accepts it lets a token
        the user consented to give one service act as a credential for another.
        """
        wrong = token(audience="http://127.0.0.1:8000/some-other-service")
        response = await secured.post(PATH, json=body("tools/list"), headers=bearer(wrong))
        assert response.status_code == 401
        assert challenge(response)["error"] == "invalid_token"

    async def test_expired_token_is_refused(self, secured):
        response = await secured.post(
            PATH, json=body("tools/list"), headers=bearer(token(ttl_seconds=-60))
        )
        assert response.status_code == 401

    async def test_token_signed_by_someone_else_is_refused(self, secured):
        forged = token(secret="an-attackers-signing-secret-40-bytes-xxxx")
        response = await secured.post(PATH, json=body("tools/list"), headers=bearer(forged))
        assert response.status_code == 401

    async def test_under_scoped_token_is_403_not_401(self, secured):
        """Authenticated but not authorized — a different answer, and a different fix."""
        response = await secured.post(
            PATH, json=body("tools/list"), headers=bearer(token(scopes=["openid"]))
        )
        assert response.status_code == 403
        parsed = challenge(response)
        assert parsed["error"] == "insufficient_scope"
        assert parsed["scope"] == auth_mod.DEFAULT_SCOPE

    async def test_a_non_bearer_authorization_header_is_refused(self, secured):
        response = await secured.post(
            PATH,
            json=body("tools/list"),
            headers=headers("tools/list", authorization="Basic dXNlcjpwdw=="),
        )
        assert response.status_code == 401


class TestVerifierConfiguration:
    async def test_short_secret_is_refused_at_construction(self):
        """RFC 7518 §3.2 wants >= 32 bytes for HS256; PyJWT only warns."""
        with pytest.raises(ValueError, match="RFC 7518"):
            auth_mod.JWTVerifier(
                secret="too-short", issuer=auth_mod.DEFAULT_ISSUER, audience=RESOURCE
            )

    async def test_the_factory_does_not_leak_auth_between_apps(self, server):
        """`mcp` is a module-level singleton; an authed app must not arm the next one."""
        probe = dict(json=body("tools/list"), headers=headers("tools/list"))

        secured_app = http_app(path=PATH, host=HOST, port=PORT, auth_secret=SECRET)
        async with asgi_client(secured_app) as client:
            assert (await client.post(PATH, **probe)).status_code == 401

        open_app = http_app(path=PATH, host=HOST, port=PORT)
        async with asgi_client(open_app) as client:
            assert (await client.post(PATH, **probe)).status_code == 200


class TestTransportParity:
    """The Day 2 claim is "the same server runs both transports". Assert it."""

    async def test_stdio_and_http_return_identical_tool_listings(self, http, index_path):
        over_http = json.loads(
            (await http.post(PATH, json=body("tools/list"), headers=headers("tools/list"))).text
        )["result"]["tools"]

        request = json.dumps(body("tools/list")) + "\n"
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-m", "regdocs_mcp"],
            input=request,
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": "/usr/bin:/bin", "REGDOCS_INDEX": str(index_path)},
        )
        assert proc.returncode == 0 or proc.stdout, proc.stderr[-2000:]
        over_stdio = json.loads(proc.stdout.splitlines()[0])["result"]["tools"]

        # Guard against the comparison passing on two empty listings.
        assert sorted(t["name"] for t in over_stdio) == sorted(EXPECTED_TOOLS)
        assert json.dumps(over_stdio, sort_keys=True) == json.dumps(over_http, sort_keys=True)
