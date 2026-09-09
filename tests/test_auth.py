from repowatch.auth import hash_password, verify_password


def test_verify_password_accepts_correct_password():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored) is True


def test_verify_password_rejects_wrong_password():
    stored = hash_password("correct horse battery staple")
    assert verify_password("wrong password", stored) is False


def test_hash_password_never_stores_plaintext():
    stored = hash_password("my-secret-password")
    assert "my-secret-password" not in stored


def test_hash_password_uses_random_salt_each_time():
    """The same password string must produce DIFFERENT hashes (random salt) —
    otherwise hashes could be compared directly (a rainbow-table risk)."""
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second
    # but both still verify successfully
    assert verify_password("same-password", first) is True
    assert verify_password("same-password", second) is True


def test_verify_password_rejects_malformed_stored_hash():
    assert verify_password("anything", "not-a-real-hash-format") is False
    assert verify_password("anything", "") is False
    assert verify_password("anything", "pbkdf2_sha256$not-a-number$aa$bb") is False


def test_verify_password_rejects_unknown_algorithm_prefix():
    fake = hash_password("password").replace("pbkdf2_sha256", "md5")
    assert verify_password("password", fake) is False
