from importlib import import_module


def test_package_imports():
    assert import_module("template_package") is not None
