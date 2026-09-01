"""Bearer-token authorization: the resource-server half, and only that half.

Spec 2026-07-28 makes authorization optional overall, but a server that supports
it MUST behave as an OAuth 2.1 *protected resource*: publish RFC 9728 metadata,
challenge with `401` + `WWW-Authenticate`, validate the token's **audience**, and
answer an under-scoped token with `403 insufficient_scope`.

What is deliberately NOT here (ADR-007): no authorization server. No `/authorize`,
no `/token`, no dynamic client registration, no PKCE, no refresh. Those belong to
an AS (Keycloak, Auth0, Entra), and standing one up would demonstrate nothing this
server is responsible for. What a real AS changes is small and named in the ADR:
`RS256` over a JWKS fetched from the issuer's metadata, in place of the shared
secret below.

**Audience validation is the part that matters, and it is ours to do.** The SDK
verifies the bearer scheme, calls this verifier, and enforces scopes; it never
checks `aud`. A resource server that accepts any signed token its issuer minted —
including one minted for a *different* resource — is the confused-deputy hole
RFC 8707 exists to close: a token the user consented to give service A becomes a
credential for service B. `jwt.decode(audience=...)` below is that check.
"""

from __future__ import annotations

import os
import time

import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_SCOPE = "regdocs:read"
# RFC 2606 reserves `.invalid`, so this can never resolve. That is the point: there
# is no authorization server here, and an operator who enables --auth without setting
# $REGDOCS_AUTH_ISSUER should get an unambiguous DNS failure rather than a confusing
# answer from whatever happens to be listening on a local port. A real client follows
# the 401 challenge to this URL, so pointing it at a loopback port is worse than
# useless — on this box :9000 is ClickHouse, and it answered a client's Dynamic Client
# Registration attempt with "Port 9000 is for clickhouse-client program".
DEFAULT_ISSUER = "https://auth.invalid"
ALGORITHM = "HS256"

SECRET_ENV = "REGDOCS_JWT_SECRET"
ISSUER_ENV = "REGDOCS_AUTH_ISSUER"

# RFC 7518 §3.2: an HMAC key MUST be at least as long as the hash output — 32
# bytes for SHA-256. PyJWT only warns; a warning on stderr is not a control, so
# this is refused at construction instead.
MIN_SECRET_BYTES = 32


def _check_secret(secret: str) -> str:
    if len(secret.encode()) < MIN_SECRET_BYTES:
        raise ValueError(
            f"signing secret is {len(secret.encode())} bytes; RFC 7518 §3.2 requires at "
            f"least {MIN_SECRET_BYTES} for HS256. Generate one with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return secret


def resource_uri(host: str, port: int, path: str) -> str:
    """This server's canonical resource identifier (RFC 8707).

    It is both the `aud` a token must carry and the `resource` in the published
    metadata, so a token minted for another service cannot be replayed here.
    """
    return f"http://{host}:{port}{path}"


class JWTVerifier:
    """Verifies a signed JWT: signature, expiry, issuer, and audience.

    Structurally satisfies the SDK's `TokenVerifier` protocol (one async method).
    Returning `None` is the only failure channel the protocol has — the SDK turns
    it into `401 invalid_token` — so every rejection below is deliberately
    indistinguishable to the caller. That is the right posture for a token
    endpoint: a verifier that explained *why* a token failed would help an
    attacker enumerate.
    """

    def __init__(
        self,
        *,
        secret: str,
        issuer: str,
        audience: str,
        required_scopes: list[str] | None = None,
    ):
        self.secret = _check_secret(secret)
        self.issuer = issuer
        self.audience = audience
        self.required_scopes = required_scopes or [DEFAULT_SCOPE]
        # RFC 8414 §2 makes issuer comparison exact string comparison, and this
        # keeps it exact — against **both** spellings that this deployment
        # publishes. The RFC 9728 document renders the issuer through pydantic's
        # AnyHttpUrl, which appends a trailing slash to a path-less authority; a
        # client that reads `authorization_servers` from that document and
        # presents a token whose `iss` matches it verbatim would otherwise be
        # rejected by the very server that published it. The relaxation is
        # exactly one character wide and covers only the SDK's own
        # normalisation — no prefix matching, no case folding.
        self.accepted_issuers = frozenset({issuer, str(AnyHttpUrl(issuer))})

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                self.secret,
                # Never taken from the token's own header: that is alg confusion.
                algorithms=[ALGORITHM],
                audience=self.audience,  # RFC 8707 — the confused-deputy check
                # `iss` is required but compared below, against the set above.
                options={"require": ["exp", "aud", "iss"], "verify_iss": False},
            )
        except jwt.InvalidTokenError:
            return None

        if claims.get("iss") not in self.accepted_issuers:
            return None

        # RFC 8693 `scope` is space-delimited; `scp` as a list is the Entra/Okta shape.
        raw = claims.get("scope") or claims.get("scp") or ""
        scopes = raw.split() if isinstance(raw, str) else list(raw)

        return AccessToken(
            token=token,
            client_id=str(
                claims.get("client_id") or claims.get("azp") or claims.get("sub") or "unknown"
            ),
            scopes=scopes,
            expires_at=claims.get("exp"),
            resource=self.audience,
            subject=claims.get("sub"),
            claims=claims,
        )


def auth_settings(
    *, issuer: str, resource: str, required_scopes: list[str] | None = None
) -> AuthSettings:
    """Resource-server-only settings.

    `resource_server_url` is what makes the SDK publish RFC 9728 metadata at
    `/.well-known/oauth-protected-resource{path}` and name it in the challenge.
    No `auth_server_provider` is passed anywhere, so no AS endpoint is mounted.
    """
    return AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(resource),
        required_scopes=required_scopes or [DEFAULT_SCOPE],
    )


class ScopeChallengeMiddleware:
    """Adds the RFC 6750 `scope` attribute to `WWW-Authenticate`.

    The SDK's challenge carries `error`, `error_description` and
    `resource_metadata` but not `scope`. RFC 6750 §3.1 says a `403
    insufficient_scope` response SHOULD name the scope required, and a client
    that has to parse `error_description` prose to learn it is a client that will
    get it wrong. Measured, then fixed — this is the only middleware of our own
    in the stack, and it exists because a probe showed the attribute absent.
    """

    def __init__(self, app: ASGIApp, scopes: list[str]):
        self.app = app
        self.scope_value = " ".join(scopes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] in (401, 403):
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"www-authenticate" and b"scope=" not in value:
                        value = value + f', scope="{self.scope_value}"'.encode()
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def mint_token(
    *,
    secret: str,
    issuer: str,
    audience: str,
    scopes: list[str] | None = None,
    subject: str = "dev",
    client_id: str = "regdocs-dev-client",
    ttl_seconds: int = 3600,
) -> str:
    """Mint a token for local demos and tests.

    **This is not an authorization server.** It stands in for one so the README's
    curl example is runnable and the tests can construct the wrong-audience and
    expired cases they need to assert. A real deployment gets tokens from an AS
    and this function is never called.
    """
    _check_secret(secret)
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "client_id": client_id,
            "scope": " ".join(scopes if scopes is not None else [DEFAULT_SCOPE]),
            "iat": now,
            "exp": now + ttl_seconds,
        },
        secret,
        algorithm=ALGORITHM,
    )


def main() -> None:
    """`python -m regdocs_mcp.auth` — print a dev token for the README's curl example."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m regdocs_mcp.auth",
        description=(
            "Mint a development bearer token. Stands in for an authorization "
            "server; never use in production."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/mcp")
    parser.add_argument("--scope", action="append", default=None, metavar="SCOPE")
    parser.add_argument(
        "--ttl", type=int, default=3600, help="seconds until expiry (default: %(default)s)"
    )
    parser.add_argument(
        "--audience", help="override the audience (use to demo an audience rejection)"
    )
    args = parser.parse_args()

    secret = os.environ.get(SECRET_ENV)
    if not secret:
        raise SystemExit(f"set {SECRET_ENV} to the same value the server was started with")
    print(
        mint_token(
            secret=secret,
            issuer=os.environ.get(ISSUER_ENV, DEFAULT_ISSUER),
            audience=args.audience or resource_uri(args.host, args.port, args.path),
            scopes=args.scope,
            ttl_seconds=args.ttl,
        )
    )


if __name__ == "__main__":
    main()
