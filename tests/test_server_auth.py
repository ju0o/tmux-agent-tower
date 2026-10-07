import stat

from tmux_agent_tower.server.auth import MAX_PAIR_ATTEMPTS, PairingSession, TokenStore


def test_new_token_store_has_no_valid_tokens(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    assert store.is_valid("anything") is False
    assert store.is_valid(None) is False
    assert store.is_valid("") is False


def test_added_token_is_valid(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.add_token("abc123")
    assert store.is_valid("abc123") is True


def test_tokens_persist_across_instances(tmp_path):
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    store.add_token("abc123")

    reopened = TokenStore(path)
    assert reopened.is_valid("abc123") is True


def test_malformed_token_file_fails_safe(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = TokenStore(path)
    assert store.is_valid("anything") is False
    store.add_token("new")
    assert store.is_valid("new") is True


def test_revoke_all_clears_every_token(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.add_token("a")
    store.add_token("b")
    store.revoke_all()
    assert store.is_valid("a") is False
    assert store.is_valid("b") is False


# -- PairingSession -----------------------------------------------------


def test_current_code_is_six_digits(tmp_path):
    session = PairingSession(TokenStore(tmp_path / "tokens.json"))
    code = session.current_code()
    assert len(code) == 6
    assert code.isdigit()


def test_current_code_is_stable_until_used_or_expired(tmp_path):
    session = PairingSession(TokenStore(tmp_path / "tokens.json"))
    first = session.current_code(now=0.0)
    second = session.current_code(now=1.0)
    assert first == second


def test_correct_code_pairs_and_issues_a_token(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    session = PairingSession(store)
    code = session.current_code(now=0.0)

    token = session.try_pair(code, now=0.0)

    assert token is not None
    assert store.is_valid(token) is True


def test_wrong_code_does_not_pair(tmp_path):
    session = PairingSession(TokenStore(tmp_path / "tokens.json"))
    session.current_code(now=0.0)
    assert session.try_pair("000000", now=0.0) is None


def test_code_is_single_use(tmp_path):
    session = PairingSession(TokenStore(tmp_path / "tokens.json"))
    code = session.current_code(now=0.0)
    assert session.try_pair(code, now=0.0) is not None
    # Replaying the same code again must fail -- it was consumed.
    assert session.try_pair(code, now=0.0) is None


def test_expired_code_does_not_pair(tmp_path):
    session = PairingSession(TokenStore(tmp_path / "tokens.json"), ttl_seconds=10.0)
    code = session.current_code(now=0.0)
    assert session.try_pair(code, now=11.0) is None


def test_new_code_generated_after_expiry(tmp_path):
    session = PairingSession(TokenStore(tmp_path / "tokens.json"), ttl_seconds=10.0)
    first = session.current_code(now=0.0)
    second = session.current_code(now=11.0)
    assert first != second


# -- pairing rate limit / lockout ----------------------------------------


def test_wrong_guesses_below_the_limit_do_not_lock_the_code(tmp_path):
    session = PairingSession(TokenStore(tmp_path / "tokens.json"))
    code = session.current_code(now=0.0)
    for _ in range(MAX_PAIR_ATTEMPTS - 1):
        assert session.try_pair("000000", now=0.0) is None
    # The real code still works -- the limit hasn't been hit yet.
    assert session.try_pair(code, now=0.0) is not None


def test_exceeding_max_attempts_locks_out_the_code_immediately(tmp_path):
    session = PairingSession(TokenStore(tmp_path / "tokens.json"))
    code = session.current_code(now=0.0)
    for _ in range(MAX_PAIR_ATTEMPTS):
        session.try_pair("000000", now=0.0)
    # Even the real code no longer works -- it was invalidated on lockout,
    # not just rate-limited for future wrong guesses.
    assert session.try_pair(code, now=0.0) is None


def test_lockout_does_not_leak_via_a_distinct_error_shape(tmp_path):
    # try_pair's return type is identical (None) whether the code was
    # wrong, expired, or locked out -- callers (httpapi.py) must not be
    # able to build a different HTTP response for "locked" vs "wrong".
    session = PairingSession(TokenStore(tmp_path / "tokens.json"))
    code = session.current_code(now=0.0)
    for _ in range(MAX_PAIR_ATTEMPTS):
        result = session.try_pair("000000", now=0.0)
        assert result is None
    locked_result = session.try_pair(code, now=0.0)
    wrong_result = session.try_pair("111111", now=0.0)
    assert locked_result is wrong_result is None


def test_regenerate_invalidates_a_still_valid_code(tmp_path):
    session = PairingSession(TokenStore(tmp_path / "tokens.json"))
    old_code = session.current_code(now=0.0)
    new_code = session.regenerate(now=0.0)
    assert new_code != old_code
    assert session.try_pair(old_code, now=0.0) is None
    assert session.try_pair(new_code, now=0.0) is not None


def test_regenerate_resets_the_attempt_counter(tmp_path):
    session = PairingSession(TokenStore(tmp_path / "tokens.json"))
    session.current_code(now=0.0)
    for _ in range(MAX_PAIR_ATTEMPTS):
        session.try_pair("000000", now=0.0)

    new_code = session.regenerate(now=0.0)
    # A fresh code after regenerate() gets a fresh attempt budget, not the
    # already-exhausted one from before.
    assert session.try_pair(new_code, now=0.0) is not None


# -- token file permissions ------------------------------------------------


def test_token_file_is_created_with_owner_only_permissions(tmp_path):
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    store.add_token("abc123")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_existing_overly_permissive_token_file_is_tightened_on_load(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)

    TokenStore(path)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_config_dir_is_owner_only(tmp_path):
    nested = tmp_path / "nested" / "tokens.json"
    TokenStore(nested)
    mode = stat.S_IMODE(nested.parent.stat().st_mode)
    assert mode == stat.S_IRWXU
