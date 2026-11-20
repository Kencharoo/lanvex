"""Signing in against a real OpenLDAP (slapd), started by scripts/ldap-test-server.sh with the directory in
tests/ldap/school.ldif: binds, searches and group checks as slapd answers them, and STARTTLS with its
certificate checked.

Run with LANVEX_TEST_LDAP_URL=ldap://localhost and LANVEX_TEST_LDAP_CA=<the script's ca.crt>.
"""

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from lanvex.auth import directory
from lanvex.auth.models import AuthSource, Role
from lanvex.core.config import get_settings

pytestmark = [
    pytest.mark.ldap,
    pytest.mark.skipif(not os.environ.get("LANVEX_TEST_LDAP_URL"), reason="set LANVEX_TEST_LDAP_URL"),
]
BASE = "dc=school,dc=example"


@pytest.fixture
def settings():  # type: ignore[no-untyped-def]
    return get_settings().model_copy(
        update={
            "ldap_url": [os.environ.get("LANVEX_TEST_LDAP_URL", "")],
            "ldap_type": "ldap",
            "ldap_starttls": True,
            "ldap_ca_file": os.environ.get("LANVEX_TEST_LDAP_CA"),
            "ldap_bind_dn": f"cn=lanvex,ou=services,{BASE}",
            "ldap_bind_password": SecretStr("svc-password"),
            "ldap_user_base": f"ou=people,{BASE}",
            "ldap_admin_group": f"cn=net-admins,ou=groups,{BASE}",
            "ldap_readonly_group": f"cn=net-readers,ou=groups,{BASE}",
        }
    )


async def test_sign_in_against_openldap(settings) -> None:  # type: ignore[no-untyped-def]
    anna = await directory.sign_in(settings, "anna", "anna-password")
    assert anna is not None and (anna.source, anna.username, anna.display_name, anna.role) == (
        AuthSource.DIRECTORY,
        "anna",
        "Anna Admin",
        Role.ADMIN,
    )
    assert anna.external_id == f"uid=anna,ou=people,{BASE}"
    bert = await directory.sign_in(settings, "bert", "bert-password")  # posixGroup memberUid
    assert bert is not None and bert.role == Role.READONLY and bert.display_name == "Bert Reader"
    assert await directory.sign_in(settings, "anna", "wrong-password") is None
    assert await directory.sign_in(settings, "anna", "") is None  # no anonymous bind
    assert await directory.sign_in(settings, "*", "anna-password") is None
    with pytest.raises(directory.NotAllowed):
        await directory.sign_in(settings, "carl", "carl-password")
    assert "can read the user base" in await directory.test_connection(settings)


async def test_the_certificate_is_checked(settings, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    import subprocess

    other = tmp_path / "other.crt"
    await asyncio.to_thread(
        subprocess.run,
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost"]
        + ["-keyout", str(tmp_path / "k"), "-out", str(other)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(directory.DirectoryUnavailable):
        await directory.sign_in(
            settings.model_copy(update={"ldap_ca_file": str(other)}), "anna", "anna-password"
        )
    wrong = settings.model_copy(update={"ldap_bind_password": SecretStr("not-it")})
    with pytest.raises(directory.DirectoryUnavailable, match="refused Lanvex's account"):
        await directory.sign_in(wrong, "anna", "anna-password")
