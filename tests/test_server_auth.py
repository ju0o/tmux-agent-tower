from tmux_agent_tower.server.auth import PairingSession, TokenStore


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
