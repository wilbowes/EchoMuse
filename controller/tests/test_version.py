import version


def test_env_var_wins(monkeypatch):
    monkeypatch.setenv("EM_CONTROLLER_VERSION", "v9.9.9")
    assert version._resolve() == "v9.9.9"


def test_git_describe_strips_controller_prefix(monkeypatch):
    monkeypatch.delenv("EM_CONTROLLER_VERSION", raising=False)
    v = version._resolve()
    # In a checkout this is the described tag; in a bare copy it's "dev".
    # Either way the controller- prefix must never leak into the display.
    assert not v.startswith("controller-")
    assert v  # never empty


def test_no_env_no_git_is_dev(monkeypatch):
    monkeypatch.delenv("EM_CONTROLLER_VERSION", raising=False)
    monkeypatch.setattr(version.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert version._resolve() == "dev"
