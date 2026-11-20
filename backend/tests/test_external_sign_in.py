"""Signing in with a directory (LDAP, Active Directory) and with single sign-on (OpenID Connect).

The directory is ldap3's in-memory server (MOCK_SYNC) holding a small school directory; the identity
provider is a stand-in that answers discovery, keys, token and userinfo as the OpenID Connect
specifications describe, signs its ID tokens with an RSA key of its own, and checks PKCE.
"""

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import ldap3
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import SecretStr

from lanvex.auth import directory, sso
from lanvex.core.config import get_settings

from .conftest import login

BASE = "dc=school,dc=example"
PEOPLE = f"ou=people,{BASE}"
ADMINS, READERS = f"cn=net-admins,ou=groups,{BASE}", f"cn=net-readers,ou=groups,{BASE}"


@pytest.fixture
def ldap(monkeypatch: pytest.MonkeyPatch) -> Iterator[ldap3.Server]:
    server = ldap3.Server("dir.school.example")
    c = ldap3.Connection(server, client_strategy=ldap3.MOCK_SYNC)
    add = c.strategy.add_entry
    for ou in ("people", "groups", "services"):
        add(f"ou={ou},{BASE}", {"objectClass": ["organizationalUnit"], "ou": ou})
    add(
        f"cn=lanvex,ou=services,{BASE}",
        {"objectClass": ["person"], "cn": "lanvex", "userPassword": "svc-password"},
    )
    for uid, name, extra in (
        ("anna", "Anna Admin", {}),
        ("bert", "Bert Reader", {}),
        ("carl", "Carl Student", {}),
        ("dora", "Dora Teacher", {"memberOf": [READERS.upper()]}),  # memberOf overlay, written in capitals
    ):
        add(
            f"uid={uid},{PEOPLE}",
            {"objectClass": ["person", "inetOrgPerson"], "uid": uid, "cn": name, "displayName": name}
            | {"userPassword": f"{uid}-password"}
            | extra,
        )
    add(ADMINS, {"objectClass": ["groupOfNames"], "cn": "net-admins", "member": [f"uid=anna,{PEOPLE}"]})
    add(READERS, {"objectClass": ["posixGroup"], "cn": "net-readers", "memberUid": ["bert"]})
    s = get_settings()
    for k, v in {
        "ldap_url": ["ldap://dir.school.example"],
        "ldap_bind_dn": f"cn=lanvex,ou=services,{BASE}",
        "ldap_bind_password": SecretStr("svc-password"),
        "ldap_user_base": PEOPLE,
        "ldap_admin_group": ADMINS,
        "ldap_readonly_group": READERS,
    }.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(directory, "STRATEGY", ldap3.MOCK_SYNC)
    monkeypatch.setattr(directory, "SERVER", server)
    yield server


async def sign_in(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"username": username, "password": password})


async def test_directory_accounts_sign_in_with_their_groups_role(
    client: httpx.AsyncClient, ldap: Any
) -> None:
    assert (await client.get("/api/v1/auth/methods")).json() == {"directory": True, "sso": None}
    r = await sign_in(client, "anna", "anna-password")
    assert r.status_code == 200, r.text
    me = r.json()["user"]
    assert (me["username"], me["role"], me["display_name"], me["auth_source"]) == (
        "anna",
        "admin",
        "Anna Admin",
        "directory",
    )
    for uid, role in (("bert", "readonly"), ("dora", "readonly")):  # memberUid; memberOf
        r = await sign_in(client, uid, f"{uid}-password")
        assert r.status_code == 200 and r.json()["user"]["role"] == role, r.text

    assert (await sign_in(client, "anna", "wrong")).json()["error"]["code"] == "invalid_credentials"
    assert (await sign_in(client, "nobody", "x-password")).json()["error"]["code"] == "invalid_credentials"
    r = await sign_in(client, "carl", "carl-password")  # right password, in no group
    assert r.status_code == 401 and r.json()["error"]["code"] == "not_allowed"
    assert (await sign_in(client, "anna)(uid=*", "anna-password")).status_code == 401  # filter escaped


async def test_a_directory_role_follows_the_groups(
    client: httpx.AsyncClient, ldap: ldap3.Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    await login(client, "anna", "anna-password")
    me = (await client.get("/api/v1/auth/session")).json()["user"]
    assert me["role"] == "admin"
    # Moved from the administrators to the readers in the directory: read-only on the next sign-in.
    monkeypatch.setattr(get_settings(), "ldap_admin_group", f"cn=other,ou=groups,{BASE}")
    monkeypatch.setattr(get_settings(), "ldap_readonly_group", ADMINS)
    r = await sign_in(client, "anna", "anna-password")
    assert r.json()["user"]["role"] == "readonly" and r.json()["user"]["id"] == me["id"]  # the same account


async def test_directory_accounts_in_lanvex(
    client: httpx.AsyncClient, ldap: Any, users: dict[str, str]
) -> None:
    # A local user with the directory account's name signs in locally; the directory is not asked.
    await login(client, "admin", users["admin"])
    r = await client.post(
        "/api/v1/users", json={"username": "bert", "password": "a-local-password-1", "role": "admin"}
    )
    assert r.status_code == 201
    assert (await sign_in(client, "bert", "bert-password")).status_code == 401
    assert (await sign_in(client, "bert", "a-local-password-1")).json()["user"]["auth_source"] == "local"

    await login(client, "anna", "anna-password")
    anna = (await client.get("/api/v1/auth/session")).json()["user"]
    r = await client.post(
        "/api/v1/auth/password", json={"current_password": "anna-password", "new_password": "x" * 16}
    )
    assert r.status_code == 409  # the directory keeps the password
    r = await client.patch(f"/api/v1/users/{anna['id']}", json={"role": "readonly"})
    assert r.status_code == 409  # the groups decide the role

    await login(client, "admin", users["admin"])
    assert (await client.patch(f"/api/v1/users/{anna['id']}", json={"is_active": False})).status_code == 200
    r = await sign_in(client, "anna", "anna-password")
    assert (
        r.status_code == 401 and r.json()["error"]["code"] == "disabled"
    )  # Lanvex can still keep someone out


async def test_when_the_directory_does_not_answer(
    client: httpx.AsyncClient, ldap: Any, users: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "ldap_bind_password", SecretStr("not-it"))
    r = await sign_in(client, "anna", "anna-password")
    assert r.status_code == 502 and r.json()["error"]["code"] == "directory_unavailable"
    await login(client, "admin", users["admin"])  # local accounts still sign in
    r = await client.post("/api/v1/auth/directory/test")
    assert r.json()["ok"] is False and "refused Lanvex's account" in r.json()["message"]


def test_active_directory_checks_nested_groups() -> None:
    """Active Directory answers membership through nested groups with LDAP_MATCHING_RULE_IN_CHAIN; ldap3's
    in-memory server cannot, so this checks the question Lanvex asks."""

    class Conn:
        def __init__(self) -> None:
            self.searches: list[tuple[str, str]] = []
            self.response: list[dict[str, Any]] = []

        def search(self, base: str, query: str, *a: Any, **k: Any) -> None:
            self.searches.append((base, query))
            self.response = [{"type": "searchResEntry", "dn": base}]

    s = get_settings().model_copy(update={"ldap_type": "ad", "ldap_url": ["ldaps://dc.example"]})
    d = directory.Directory.__new__(directory.Directory)
    d.s = s
    conn = Conn()
    assert d._member(conn, "CN=Anna,OU=Staff,DC=corp", "anna", [], "CN=Net Admins (IT),OU=Groups,DC=corp")
    assert conn.searches == [
        (
            "CN=Anna,OU=Staff,DC=corp",
            "(memberOf:1.2.840.113556.1.4.1941:=CN=Net Admins \\28IT\\29,OU=Groups,DC=corp)",
        )
    ]
    assert (
        "sAMAccountName={username}" in directory.FILTERS["ad"]
        and "userPrincipalName" in directory.FILTERS["ad"]
    )


# ---- single sign-on ---------------------------------------------------------------------------------------


class Idp:
    """TEST DOUBLE for an OpenID Connect provider."""

    issuer = "https://idp.example/realms/school"

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"})
        self.claims: dict[str, Any] = {
            "sub": "a1b2",
            "preferred_username": "erik",
            "name": "Erik Example",
            "groups": ["/net-admins", "/staff"],
        }
        self.userinfo: dict[str, Any] | None = None
        self.pending: dict[str, dict[str, str]] = {}
        self.sign_with = self.key
        self.audience = "lanvex"

    def authorize(self, url: str) -> dict[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert url.startswith(f"{self.issuer}/protocol/openid-connect/auth?")
        assert (q["response_type"], q["client_id"], q["code_challenge_method"]) == ("code", "lanvex", "S256")
        assert q["redirect_uri"] == "http://lanvex.test/api/v1/auth/sso/callback"
        code = secrets.token_urlsafe(8)
        self.pending[code] = q
        return {"code": code, "state": q["state"]}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/realms/school")
        if path == "/.well-known/openid-configuration":
            base = f"{self.issuer}/protocol/openid-connect"
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{base}/auth",
                    "token_endpoint": f"{base}/token",
                    "jwks_uri": f"{base}/certs",
                    "userinfo_endpoint": f"{base}/userinfo",
                    "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                },
            )
        if path == "/protocol/openid-connect/certs":
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if path == "/protocol/openid-connect/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            auth = base64.b64decode(request.headers["authorization"].split()[1]).decode()
            q = self.pending.pop(form.get("code", ""), None)
            if auth != "lanvex:s3cret" or q is None or form["redirect_uri"] != q["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(form["code_verifier"].encode()).digest()
            ).rstrip(b"=")
            if challenge.decode() != q["code_challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
            now = int(time.time())
            claims = {
                "iss": self.issuer,
                "aud": self.audience,
                "iat": now,
                "exp": now + 300,
                "nonce": q["nonce"],
            }
            token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims | self.claims, self.sign_with)
            return httpx.Response(
                200, json={"id_token": token, "access_token": "at-1", "token_type": "Bearer"}
            )
        if path == "/protocol/openid-connect/userinfo":
            assert request.headers["authorization"] == "Bearer at-1"
            return httpx.Response(200, json=self.userinfo or {})
        return httpx.Response(404)


@pytest.fixture
def idp(app: Any, monkeypatch: pytest.MonkeyPatch) -> Idp:
    provider = Idp()
    app.state.sso_transport = httpx.MockTransport(provider.handle)
    s = get_settings()
    for k, v in {
        "public_url": "http://lanvex.test",
        "oidc_issuer": provider.issuer,
        "oidc_client_id": "lanvex",
        "oidc_client_secret": SecretStr("s3cret"),
        "oidc_admin_groups": ["/net-admins"],
        "oidc_readonly_groups": ["staff"],
        "oidc_name": "School account",
    }.items():
        monkeypatch.setattr(s, k, v)
    sso._providers.clear()
    sso._keys.clear()
    return provider


async def sso_sign_in(client: httpx.AsyncClient, idp: Idp, next_path: str = "/servers") -> httpx.Response:
    r = await client.get("/api/v1/auth/sso/start", params={"next": next_path})
    assert r.status_code == 303, r.text
    answer = idp.authorize(r.headers["location"])
    return await client.get("/api/v1/auth/sso/callback", params=answer)


async def test_single_sign_on(client: httpx.AsyncClient, idp: Idp) -> None:
    assert (await client.get("/api/v1/auth/methods")).json() == {"directory": False, "sso": "School account"}
    r = await sso_sign_in(client, idp)
    assert r.status_code == 200 and 'content="0;url=/servers"' in r.text
    me = (await client.get("/api/v1/auth/session")).json()["user"]
    assert (me["username"], me["display_name"], me["role"], me["auth_source"]) == (
        "erik",
        "Erik Example",
        "admin",
        "sso",
    )
    events = (await client.get("/api/v1/audit-events", params={"action": "auth.login"})).json()["results"]
    assert events[0]["detail"] == {"method": "sso"}

    # The same person again, now only staff: the same account, read-only.
    idp.claims["groups"] = ["/staff"]
    assert (await sso_sign_in(client, idp, "//evil.example")).text.count('url=/"') == 1  # never another site
    me2 = (await client.get("/api/v1/auth/session")).json()["user"]
    assert me2["id"] == me["id"] and me2["role"] == "readonly"


async def test_groups_from_userinfo(client: httpx.AsyncClient, idp: Idp) -> None:
    del idp.claims["groups"]
    idp.userinfo = {"sub": "a1b2", "groups": ["staff"]}
    assert (await sso_sign_in(client, idp)).status_code == 200
    assert (await client.get("/api/v1/auth/session")).json()["user"]["role"] == "readonly"


@pytest.mark.parametrize(
    ("spoil", "reason"),
    [
        (lambda idp: idp.claims.update(groups=["/students"]), "denied"),
        (
            lambda idp: setattr(idp, "sign_with", RSAKey.generate_key(2048, parameters={"kid": "k1"})),
            "failed",
        ),
        (lambda idp: setattr(idp, "audience", "another-app"), "failed"),
        (lambda idp: idp.claims.update(preferred_username="admin"), "taken"),  # a local user's name
    ],
)
async def test_single_sign_on_refused(
    client: httpx.AsyncClient, idp: Idp, users: dict[str, str], spoil: Any, reason: str
) -> None:
    spoil(idp)
    r = await sso_sign_in(client, idp)
    assert r.status_code == 303 and r.headers["location"] == f"/login?sso_error={reason}"
    assert (await client.get("/api/v1/auth/session")).status_code == 401


async def test_an_answer_for_another_sign_in(client: httpx.AsyncClient, idp: Idp) -> None:
    r = await client.get("/api/v1/auth/sso/start")
    answer = idp.authorize(r.headers["location"])
    r = await client.get("/api/v1/auth/sso/callback", params=answer | {"state": "someone-elses"})
    assert r.headers["location"] == "/login?sso_error=failed"
    client.cookies.clear()  # another browser
    r = await client.get("/api/v1/auth/sso/callback", params=answer)
    assert r.headers["location"] == "/login?sso_error=expired"
    r = await client.get("/api/v1/auth/sso/callback", params={"error": "access_denied", "state": "x"})
    assert r.headers["location"] == "/login?sso_error=cancelled"
    assert json.loads((await client.get("/api/v1/auth/session")).text)["error"]["code"] == "not_authenticated"


async def test_testing_the_directory(client: httpx.AsyncClient, ldap: Any, users: dict[str, str]) -> None:
    await login(client, "admin", users["admin"])
    r = await client.post("/api/v1/auth/directory/test")
    assert r.json() == {"ok": True, "message": "Connected; Lanvex's account can read the user base."}
    conf = (await client.get("/api/v1/auth/configuration")).json()
    assert conf["sso"] is None and conf["directory"]["encrypted"] is False  # ldap:// without STARTTLS
    assert "bind_password" not in json.dumps(conf) and "svc-password" not in json.dumps(conf)
