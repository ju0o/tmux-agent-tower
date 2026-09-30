from tmux_agent_tower import main


def test_serve_subcommand_routes_with_defaults(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_serve", lambda lan, port: calls.append((lan, port)))
    main.cli(["serve"])
    assert calls == [(False, main.DEFAULT_SERVE_PORT)]


def test_serve_subcommand_with_lan_and_port(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_serve", lambda lan, port: calls.append((lan, port)))
    main.cli(["serve", "--lan", "--port", "9999"])
    assert calls == [(True, 9999)]


def test_no_subcommand_still_runs_normal_ui(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_ui", lambda: calls.append("ui"))
    main.cli([])
    assert calls == ["ui"]


def test_version_flag_still_works(monkeypatch, capsys):
    main.cli(["--version"])
    out = capsys.readouterr().out
    assert "tower" in out
