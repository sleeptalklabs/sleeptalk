from sleeptalk.webcalls import resolve_web_call, web_call_registered


def test_resolve_web_call_uses_package_host_without_app(monkeypatch):
    import sleeptalk.webcalls as webcalls

    webcalls._runner = None
    runner = resolve_web_call()
    assert callable(runner)
    assert web_call_registered()
