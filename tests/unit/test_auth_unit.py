"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


import pytest


from app.core.security import hash_password, verify_password


pytestmark = pytest.mark.unit


def test_password_storage_uses_distinct_hashes():
    password = "Test-Passw0rd!"
    first, second = hash_password(password), hash_password(password)
    assert first != password and second != password and first != second
    assert verify_password(password, first) and verify_password(password, second)


@pytest.mark.parametrize(
    "password,broken,expected",
    [
        ("Test-Passw0rd!", False, True),
        ("wrong-password", False, False),
        ("Test-Passw0rd!", True, False),
    ],
    ids=["correct", "wrong", "corrupt-hash"],
)
def test_password_verification(password, broken, expected):
    stored = "not-a-bcrypt-hash" if broken else hash_password("Test-Passw0rd!")
    assert verify_password(password, stored) is expected
