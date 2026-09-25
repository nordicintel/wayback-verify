from importlib import import_module


def test_package_imports():
    assert import_module("wayback_verify") is not None
