"""Configure a newly generated repository, then remove setup scaffolding."""

import argparse
import json
import keyword
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{label}{suffix}: ").strip() or default


def read_choices() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="Repository name under nordicintel")
    parser.add_argument("--module", help="Python import name")
    parser.add_argument("--description", help="One-line project description")
    publishing = parser.add_mutually_exclusive_group()
    publishing.add_argument("--publish", action="store_true", dest="publish")
    publishing.add_argument("--no-publish", action="store_false", dest="publish")
    parser.set_defaults(publish=None)
    parser.add_argument("--license", choices=["none", "mit"])
    choices = parser.parse_args()

    choices.repo = choices.repo or prompt("Repository name", ROOT.name)
    if not re.fullmatch(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*", choices.repo):
        parser.error(
            "Repository name must contain letters/numbers separated by . _ or -"
        )
    default_module = re.sub(r"[.-]", "_", choices.repo).lower()
    choices.module = choices.module or prompt("Python import name", default_module)
    if (
        not re.fullmatch(r"[a-z_][a-z0-9_]*", choices.module)
        or keyword.iskeyword(choices.module)
        or choices.module in sys.stdlib_module_names
    ):
        parser.error(
            "Import name must be a lowercase Python identifier, not a stdlib name"
        )
    choices.description = (
        choices.description
        if choices.description is not None
        else prompt("Description")
    )
    if not choices.description.strip() or any(
        ord(character) < 32 for character in choices.description
    ):
        parser.error(
            "Provide a nonempty, one-line description without control characters"
        )
    if choices.publish is None:
        answer = prompt("Enable PyPI publishing? (yes/no)", "no").lower()
        if answer not in {"y", "yes", "n", "no"}:
            parser.error("Publishing choice must be yes or no")
        choices.publish = answer in {"y", "yes"}
    choices.license = choices.license or prompt("License (none/mit)", "none").lower()
    if choices.license not in {"none", "mit"}:
        parser.error("Choose none or mit; other licenses can be added after setup")
    return choices


def configure(choices: argparse.Namespace) -> None:
    scaffold = ROOT / ".template"
    original_package = ROOT / "src/template_package"
    package = ROOT / "src" / choices.module
    if package != original_package and package.exists():
        raise SystemExit(f"Refusing to overwrite existing package: {package}")
    for relative in ("LICENSE", ".github/workflows/publish.yml"):
        if (ROOT / relative).exists():
            raise SystemExit(
                f"Refusing to overwrite {relative}; use a fresh template copy"
            )

    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    metadata = metadata.replace(
        'name = "nordicintel-python-template"', f"name = {json.dumps(choices.repo)}"
    ).replace(
        'description = "A shared Python foundation for NordicIntel projects."',
        f"description = {json.dumps(choices.description, ensure_ascii=False)}",
    )
    metadata = metadata.replace("src/template_package", f"src/{choices.module}")
    metadata = metadata.replace(
        "https://github.com/nordicintel/python-template",
        f"https://github.com/nordicintel/{choices.repo}",
    )
    if choices.license == "mit":
        metadata = metadata.replace(
            'readme = "README.md"',
            'readme = "README.md"\nlicense = "MIT"\nlicense-files = ["LICENSE"]',
        )

    releases = (
        "Set `[project].name` in `pyproject.toml` to the intended PyPI distribution name "
        "before the first release. Run `uv lock` after changing package metadata.\n\n"
        "1. Update `[project].version` in `pyproject.toml`, run `uv lock`, and commit.\n"
        "2. Run the checks above and `uv build`.\n"
        "3. Publish a GitHub Release from that commit with a matching tag, "
        "such as `v0.1.0`.\n\n"
        "The release workflow checks the tag against the version, runs checks, builds, "
        "and publishes to PyPI.\n\n"
        "Before publishing, add a repository Actions secret named `PYPI_TOKEN` under "
        "Settings → Secrets and variables → Actions. The token must permit uploads to "
        "the intended PyPI project."
        if choices.publish
        else "PyPI publishing is not configured. To build local distributions, run "
        "`uv build`."
    )
    values = {
        "REPO": choices.repo,
        "MODULE": choices.module,
        "DESCRIPTION": choices.description,
        "RELEASES": releases,
        "LICENSE": "MIT; see [LICENSE](LICENSE)."
        if choices.license == "mit"
        else "No license has been selected. Add one when appropriate.",
    }
    readme = re.sub(
        r"\{\{([A-Z]+)\}\}",
        lambda match: values[match[1]],
        (scaffold / "README.md").read_text(encoding="utf-8"),
    )
    updates = {"pyproject.toml": metadata, "README.md": readme}
    for relative in ("tests/test_import.py",):
        updates[relative] = (
            (ROOT / relative)
            .read_text(encoding="utf-8")
            .replace("template_package", choices.module)
        )
    if choices.publish:
        updates[".github/workflows/publish.yml"] = (scaffold / "publish.yml").read_text(
            encoding="utf-8"
        )
    if choices.license == "mit":
        updates["LICENSE"] = (
            (scaffold / "MIT.txt")
            .read_text(encoding="utf-8")
            .replace("{{YEAR}}", str(date.today().year))
        )

    for relative, content in updates.items():
        (ROOT / relative).write_text(content, encoding="utf-8", newline="\n")
    if package != original_package:
        original_package.rename(package)
    for name in ("README.md", "publish.yml", "MIT.txt"):
        (scaffold / name).unlink()
    scaffold.rmdir()
    Path(__file__).unlink()


def main() -> None:
    if not (ROOT / ".template").is_dir():
        raise SystemExit("This repository has already been configured")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("Install uv before configuring this repository")
    choices = read_choices()
    configure(choices)
    try:
        subprocess.run([uv, "sync"], cwd=ROOT, check=True)
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            "Repository configured, but environment setup failed. "
            "Resolve the uv error and run `uv sync` again before committing."
        ) from error
    print(f"Configured nordicintel/{choices.repo}; import name: {choices.module}.")
    print("Run the README checks, then review and commit the generated files.")
    if choices.publish:
        print("Reminder: add a repository Actions secret named PYPI_TOKEN.")


if __name__ == "__main__":
    main()
