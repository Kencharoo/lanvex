import pytest

from lanvex.core.config import Settings
from lanvex.core.errors import NotConfigured
from lanvex.core.secrets import Vault, VaultError, generate_secret_key

A = "a" * 40
B = "b" * 40


def test_round_trip_and_ciphertext_is_opaque() -> None:
    v = Vault(A)
    token = v.encrypt({"password": "hunter2-correct"})
    assert "hunter2" not in token
    assert v.decrypt(token) == {"password": "hunter2-correct"}


def test_wrong_key_fails_with_guidance() -> None:
    token = Vault(A).encrypt({"x": 1})
    with pytest.raises(VaultError) as e:
        Vault(B).decrypt(token)
    assert "LANVEX_SECRET_KEY_FALLBACKS" in e.value.message


def test_rotation_with_fallback() -> None:
    old_token = Vault(A).encrypt({"x": 1})
    rotated = Vault(B, [A])
    assert rotated.decrypt(old_token) == {"x": 1}
    new_token = rotated.rotate(old_token)
    assert Vault(B).decrypt(new_token) == {"x": 1}


def test_key_requirements() -> None:
    with pytest.raises(NotConfigured):
        Vault("short")
    with pytest.raises(NotConfigured) as e:
        Vault.from_settings(Settings(secret_key=None))
    assert "generate-secret-key" in e.value.message
    assert len(generate_secret_key()) >= 32
