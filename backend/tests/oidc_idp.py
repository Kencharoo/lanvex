"""TEST DOUBLE: an OpenID Connect provider for the browser tests (frontend/e2e/sso.spec.ts).

Signs everyone in as one person without asking: /authorize sends the browser straight back with a code.
It answers discovery, keys and token as the specifications describe, signs ID tokens with an RSA key
made at start, and checks the client's secret and PKCE. Run on localhost while Lanvex runs on
127.0.0.1, so the browser treats them as two sites, as with a real provider.

    uv run uvicorn tests.oidc_idp:app --port 18003
"""

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from joserfc import jwt
from joserfc.jwk import RSAKey

ISSUER = "http://localhost:18003"
CLIENT_ID, CLIENT_SECRET = "lanvex-e2e", "e2e-client-secret"
PERSON = {
    "sub": "e2e-sso-1",
    "preferred_username": "sso-erik",
    "name": "Erik Example",
    "groups": ["net-admins"],
}

app = FastAPI()
key = RSAKey.generate_key(2048, parameters={"kid": "e2e", "use": "sig", "alg": "RS256"})
pending: dict[str, dict[str, str]] = {}


@app.get("/.well-known/openid-configuration")
def discovery() -> dict[str, object]:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }


@app.get("/jwks")
def jwks() -> dict[str, object]:
    return {"keys": [key.as_dict(private=False)]}


@app.get("/authorize")
def authorize(request: Request) -> RedirectResponse:
    q = dict(request.query_params)
    if q.get("client_id") != CLIENT_ID or q.get("code_challenge_method") != "S256":
        raise HTTPException(400, "bad request")
    code = secrets.token_urlsafe(16)
    pending[code] = q
    return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}", 302)


@app.post("/token")
async def token(request: Request, authorization: str = Header("")) -> JSONResponse:
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    code, code_verifier, redirect_uri = (form.get(k, "") for k in ("code", "code_verifier", "redirect_uri"))
    q = pending.pop(code, None)
    expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    )
    if (
        q is None
        or authorization != expected
        or redirect_uri != q["redirect_uri"]
        or challenge != q["code_challenge"]
    ):
        return JSONResponse({"error": "invalid_grant"}, 400)
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, "nonce": q["nonce"], **PERSON}
    return JSONResponse(
        {"id_token": jwt.encode({"alg": "RS256", "kid": "e2e"}, claims, key), "token_type": "Bearer"}
    )
