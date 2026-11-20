"""Single sign-on with OpenID Connect: the authorization code flow with PKCE, state and nonce.

/auth/sso/start sends the browser to the identity provider with a fresh state, nonce and PKCE
verifier, kept encrypted in a short-lived cookie of Lanvex's own. /auth/sso/callback checks the state,
exchanges the code (with the verifier and the client secret) for tokens, and verifies the ID token:
its signature against the provider's published keys, issuer, audience, expiry and nonce. The groups
claim (from the ID token, or else from the userinfo endpoint) decides the role, as the directory's
groups do; an account in none of the configured groups may not sign in.
"""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from lanvex.auth.directory import Identity
from lanvex.auth.models import AuthSource, Role
from lanvex.core.config import Settings
from lanvex.core.secrets import Vault, VaultError

COOKIE = "lanvex_sso"
COOKIE_PATH = "/api/v1/auth/sso"
MAX_AGE = 600  # seconds from start to callback
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
DISCOVERY_TTL = 3600


class SsoError(Exception):
    """A sign-in that did not work; `code` is what the sign-in page says about it."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


@dataclass
class Provider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None
    basic_auth: bool  # client_secret_basic rather than client_secret_post


_providers: dict[str, tuple[float, Provider]] = {}
_keys: dict[str, KeySet] = {}


def callback_url(settings: Settings) -> str:
    return f"{settings.public_url}/api/v1/auth/sso/callback"


async def provider(settings: Settings, http: httpx.AsyncClient) -> Provider:
    issuer = settings.oidc_issuer or ""
    cached = _providers.get(issuer)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        r = await http.get(f"{issuer}/.well-known/openid-configuration")
        r.raise_for_status()
        doc = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise SsoError("unavailable", f"The identity provider's configuration could not be read: {e}.") from e
    if doc.get("issuer", "").rstrip("/") != issuer:
        raise SsoError("failed", f"The identity provider calls itself {doc.get('issuer')!r}, not {issuer!r}.")
    methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    p = Provider(
        issuer=doc["issuer"],
        authorization_endpoint=doc["authorization_endpoint"],
        token_endpoint=doc["token_endpoint"],
        jwks_uri=doc["jwks_uri"],
        userinfo_endpoint=doc.get("userinfo_endpoint"),
        basic_auth="client_secret_basic" in methods,
    )
    _providers[issuer] = (time.monotonic() + DISCOVERY_TTL, p)
    return p


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def safe_next(path: str | None) -> str:
    """Where to go after signing in: a path in Lanvex, never another site."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path or "\n" in path:
        return "/"
    return path[:512]


async def start(settings: Settings, http: httpx.AsyncClient, next_path: str | None) -> tuple[str, str]:
    """The provider's URL to send the browser to, and the cookie that remembers this attempt."""
    p = await provider(settings, http)
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": callback_url(settings),
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    cookie = Vault.from_settings(settings).encrypt(
        {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "next": safe_next(next_path),
            "at": time.time(),
        }
    )
    sep = "&" if "?" in p.authorization_endpoint else "?"
    return f"{p.authorization_endpoint}{sep}{urlencode(params)}", cookie


async def _key_set(http: httpx.AsyncClient, p: Provider, refresh: bool = False) -> KeySet:
    if refresh or p.jwks_uri not in _keys:
        try:
            r = await http.get(p.jwks_uri)
            r.raise_for_status()
            _keys[p.jwks_uri] = KeySet.import_key_set(r.json())
        except (httpx.HTTPError, ValueError, JoseError) as e:
            raise SsoError("unavailable", f"The identity provider's keys could not be read: {e}.") from e
    return _keys[p.jwks_uri]


async def finish(
    settings: Settings, http: httpx.AsyncClient, code: str, state: str, cookie: str | None
) -> tuple[Identity, str]:
    """The signed-in identity and where to go next."""
    if not cookie:
        raise SsoError("expired", "The sign-in started too long ago, or in another browser.")
    try:
        saved = Vault.from_settings(settings).decrypt(cookie)
    except VaultError as e:
        raise SsoError("expired", "The sign-in could not be matched to this browser.") from e
    if time.time() - float(saved.get("at", 0)) > MAX_AGE:
        raise SsoError("expired", "The sign-in took longer than ten minutes.")
    if not secrets.compare_digest(str(saved.get("state", "")), state):
        raise SsoError("failed", "The answer does not belong to this sign-in (state).")
    p = await provider(settings, http)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_url(settings),
        "code_verifier": saved["verifier"],
        "client_id": settings.oidc_client_id or "",
    }
    secret = settings.oidc_client_secret.get_secret_value() if settings.oidc_client_secret else None
    auth = None
    if secret and p.basic_auth:
        auth = httpx.BasicAuth(settings.oidc_client_id or "", secret)
    elif secret:
        form["client_secret"] = secret
    try:
        r = await http.post(p.token_endpoint, data=form, auth=auth, headers={"Accept": "application/json"})
        tokens = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise SsoError("unavailable", f"The identity provider did not answer: {e}.") from e
    if r.status_code != 200 or "id_token" not in tokens:
        raise SsoError(
            "failed", f"The identity provider refused the code: {tokens.get('error', r.status_code)}."
        )
    claims = await _verify(settings, http, p, tokens["id_token"], saved["nonce"])
    groups_claim = settings.oidc_groups_claim
    if groups_claim not in claims and p.userinfo_endpoint and tokens.get("access_token"):
        claims = {**await _userinfo(http, p, tokens["access_token"], claims["sub"]), **claims}
    return identity(settings, claims), saved["next"]


async def _verify(
    settings: Settings, http: httpx.AsyncClient, p: Provider, token: str, nonce: str
) -> dict[str, Any]:
    keys = await _key_set(http, p)
    try:
        try:
            decoded = jwt.decode(token, keys, algorithms=ALGORITHMS)
        except JoseError:
            decoded = jwt.decode(
                token, await _key_set(http, p, refresh=True), algorithms=ALGORITHMS
            )  # rotated
        claims = dict(decoded.claims)
        jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": p.issuer},
            aud={"essential": True, "value": settings.oidc_client_id},
            exp={"essential": True},
            sub={"essential": True},
            nonce={"essential": True, "value": nonce},
        ).validate(claims)
    except JoseError as e:
        raise SsoError("failed", f"The identity token is not valid: {e}.") from e
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != settings.oidc_client_id:
        raise SsoError("failed", "The identity token was issued to another application (azp).")
    return claims


async def _userinfo(http: httpx.AsyncClient, p: Provider, access_token: str, sub: str) -> dict[str, Any]:
    try:
        r = await http.get(p.userinfo_endpoint or "", headers={"Authorization": f"Bearer {access_token}"})
        info = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        info = {}
    return info if info.get("sub") == sub else {}  # someone else's answer counts for nothing


def _groups(value: Any) -> set[str]:
    items = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    # Keycloak sends groups as paths ("/lanvex-admins"); either form matches.
    return {str(g) for g in items} | {str(g).lstrip("/") for g in items}


def identity(settings: Settings, claims: dict[str, Any]) -> Identity:
    groups = _groups(claims.get(settings.oidc_groups_claim))
    admin = {g.lstrip("/") for g in settings.oidc_admin_groups}
    readonly = {g.lstrip("/") for g in settings.oidc_readonly_groups}
    if groups & admin:
        role = Role.ADMIN
    elif groups & readonly:
        role = Role.READONLY
    else:
        raise SsoError("denied", "This account is in none of the groups allowed to use Lanvex.")
    username = str(claims.get(settings.oidc_username_claim) or claims.get("email") or claims["sub"]).strip()
    return Identity(
        source=AuthSource.SSO,
        external_id=f"{claims['iss']}|{claims['sub']}"[:512],
        username=username[:64],
        display_name=(str(claims["name"])[:128] if claims.get("name") else None),
        role=role,
    )
