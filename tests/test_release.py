import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from contextlib import nullcontext
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_release.py"
spec = importlib.util.spec_from_file_location("verify_release", SCRIPT)
release = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = release
spec.loader.exec_module(release)


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "checkout"
    files = dict.fromkeys(release.SDIST_FILES, "test fixture\n")
    files.update(
        {
            "pyproject.toml": '[project]\nname = "ginko"\nversion = "0.1.0"\n',
            "uv.lock": '[[package]]\nname = "ginko"\nversion = "0.1.0"\n',
            "src/ginko/__init__.py": '__version__ = "0.1.0"\n',
            "src/ginko/storage/database.py": "SCHEMA_VERSION = 1\n",
            "src/ginko/personas/ginko/manifest.toml": (
                'version = "1.0.0"\ndisplay_name = "百生吟子"\n'
            ),
            "src/ginko/personas/ginko/identity.md": "identity\n",
            "src/ginko/personas/ginko/style.md": "style\n",
            "src/ginko/personas/ginko/world.md": "world\n",
            "tests/test_example.py": "def test_example(): pass\n",
            ".gitignore": ".env\n",
        }
    )
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return root


def artifact_files(source, kind):
    metadata = (
        b"Metadata-Version: 2.4\nName: ginko\nVersion: 0.1.0\n"
        b"License-Expression: AGPL-3.0-only AND LicenseRef-Ginko-Persona\n"
        b"License-File: LICENSE\nLicense-File: NOTICE\n"
        b"License-File: LICENSES/LicenseRef-Ginko-Persona.txt\n"
    )
    if kind == "wheel":
        files = {
            name.removeprefix("src/"): data
            for name, data in source.files.items()
            if name.startswith("src/ginko/")
        }
        files.update({f"ginko-0.1.0.dist-info/{name}": b"" for name in release.DIST_INFO_FILES})
        files.update(
            {
                f"ginko-0.1.0.dist-info/licenses/{name}": source.files[name]
                for name in release.LICENSE_FILES
            }
        )
        files["ginko-0.1.0.dist-info/METADATA"] = metadata
        return files
    files = {f"ginko-0.1.0/{name}": data for name, data in source.files.items()}
    files["ginko-0.1.0/PKG-INFO"] = metadata
    return files


def write_artifact(directory, kind, files):
    directory.mkdir(exist_ok=True)
    filename = "ginko-0.1.0-py3-none-any.whl" if kind == "wheel" else "ginko-0.1.0.tar.gz"
    path = directory / filename
    if kind == "wheel":
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
    else:
        with tarfile.open(path, "w:gz") as archive:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return path


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_artifact_manifest_has_source_resources_and_hashes(source_tree, tmp_path, kind):
    source = release.read_source(source_tree)
    files = artifact_files(source, kind)
    path = write_artifact(tmp_path, kind, files)
    result = release.inspect_artifact(path, source)
    assert {item["path"] for item in result["files"]} == files.keys()
    assert result["sha256"] == release.sha256(path.read_bytes())
    assert all(len(item["sha256"]) == 64 for item in result["files"])


@pytest.mark.parametrize(
    ("kind", "unwanted"),
    [
        ("sdist", "ginko-0.1.0/docs/persona/private-source.md"),
        ("sdist", "ginko-0.1.0/docs/acceptance/0.2.0.md"),
        ("sdist", "ginko-0.1.0/docs/releases/0.1.0.md"),
        ("sdist", "ginko-0.1.0/docs/release-plan.md"),
        ("sdist", "ginko-0.1.0/.env"),
        ("sdist", "ginko-0.1.0/config.local.toml"),
        ("sdist", "ginko-0.1.0/data/private.sqlite3"),
        ("sdist", "ginko-0.1.0/logs/messages.log"),
        ("wheel", "ginko/__pycache__/cli.pyc"),
        ("wheel", "ginko/extra.py"),
        ("wheel", "ginko-0.1.0.dist-info/secrets.txt"),
    ],
)
def test_private_or_unexpected_archive_files_are_rejected(source_tree, tmp_path, kind, unwanted):
    source = release.read_source(source_tree)
    files = artifact_files(source, kind)
    files[unwanted] = b"must not ship"
    path = write_artifact(tmp_path, kind, files)
    with pytest.raises(release.VerificationError, match="archive"):
        release.inspect_artifact(path, source)


@pytest.mark.parametrize("missing", ["ginko/personas/ginko/world.md", "ginko/storage/database.py"])
def test_incomplete_wheel_is_rejected(source_tree, tmp_path, missing):
    source = release.read_source(source_tree)
    files = artifact_files(source, "wheel")
    del files[missing]
    path = write_artifact(tmp_path, "wheel", files)
    with pytest.raises(release.VerificationError, match="missing archive files"):
        release.inspect_artifact(path, source)


def test_old_artifact_with_same_version_is_rejected(source_tree, tmp_path):
    source = release.read_source(source_tree)
    files = artifact_files(source, "wheel")
    files["ginko/personas/ginko/identity.md"] = b"outdated persona"
    path = write_artifact(tmp_path, "wheel", files)
    with pytest.raises(release.VerificationError, match="differs from current source"):
        release.inspect_artifact(path, source)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_archive_metadata_version_must_match_source(source_tree, tmp_path, kind):
    source = release.read_source(source_tree)
    files = artifact_files(source, kind)
    metadata_path = "ginko-0.1.0.dist-info/METADATA" if kind == "wheel" else "ginko-0.1.0/PKG-INFO"
    files[metadata_path] = files[metadata_path].replace(b"0.1.0", b"0.2.0")
    path = write_artifact(tmp_path, kind, files)
    with pytest.raises(release.VerificationError, match="metadata name or version"):
        release.inspect_artifact(path, source)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("name", ["LICENSE", "NOTICE", "LICENSES/LicenseRef-Ginko-Persona.txt"])
def test_license_files_must_exist_and_match_source(source_tree, tmp_path, kind, name):
    source = release.read_source(source_tree)
    files = artifact_files(source, kind)
    prefix = "ginko-0.1.0.dist-info/licenses/" if kind == "wheel" else "ginko-0.1.0/"
    del files[prefix + name]
    path = write_artifact(tmp_path, kind, files)
    with pytest.raises(release.VerificationError, match="missing archive files"):
        release.inspect_artifact(path, source)
    files[prefix + name] = b"wrong license scope"
    path = write_artifact(tmp_path, kind, files)
    with pytest.raises(release.VerificationError, match="differs from current source"):
        release.inspect_artifact(path, source)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (b"License-File: NOTICE\n", b""),
        (b"License-File: NOTICE\n", b"License-File: OTHER\n"),
        (b"License-File: NOTICE\n", b"License-File: NOTICE\nLicense-File: NOTICE\n"),
        (b"AGPL-3.0-only AND LicenseRef-Ginko-Persona", b"MIT"),
        (b"AGPL-3.0-only AND LicenseRef-Ginko-Persona", b"AGPL-3.0-only"),
        (b"AGPL-3.0-only", b"AGPL-3.0-or-later"),
        (b"License-File: NOTICE\n", b"License-File: NOTICE\nLicense: MIT\n"),
    ],
)
def test_metadata_preserves_license_scope_and_notices(source_tree, tmp_path, original, replacement):
    source = release.read_source(source_tree)
    files = artifact_files(source, "wheel")
    metadata_path = "ginko-0.1.0.dist-info/METADATA"
    files[metadata_path] = files[metadata_path].replace(original, replacement)
    path = write_artifact(tmp_path, "wheel", files)
    with pytest.raises(release.VerificationError, match="license"):
        release.inspect_artifact(path, source)


@pytest.mark.parametrize("name", ["uv.lock", "src/ginko/__init__.py"])
def test_source_versions_must_agree(source_tree, name):
    source = release.read_source(source_tree)
    (source_tree / name).write_bytes(source.files[name].replace(b"0.1.0", b"0.2.0"))
    with pytest.raises(release.VerificationError, match="version differs"):
        release.read_source(source_tree)


def test_traversal_and_links_are_rejected_before_install(tmp_path):
    path = write_artifact(tmp_path, "wheel", {"../outside.py": b""})
    with pytest.raises(release.VerificationError, match="unsafe archive path"):
        release.read_archive(path)
    path = tmp_path / "ginko-0.1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("ginko-0.1.0/src/ginko/linked.py")
        member.type = tarfile.SYMTYPE
        member.linkname = "/private"
        archive.addfile(member)
    with pytest.raises(release.VerificationError, match="nonregular"):
        release.read_archive(path)


@pytest.fixture
def runtime(source_tree, tmp_path):
    source = release.read_source(source_tree)
    venv = tmp_path / "isolated/venv"
    site_packages = venv / "lib/site-packages"
    wheel = source_tree / "dist/ginko-0.1.0-py3-none-any.whl"
    return {
        "doctor": {
            "version": "0.1.0",
            "stage": "foundation",
            "persona": "百生吟子",
            "persona_version": "1.0.0",
            "live_gateway": False,
            "live_model": False,
        },
        "smoke": {
            "mode": "offline",
            "version": "0.1.0",
            "persona": "百生吟子",
            "deduplicated": True,
            "inbox": "done",
            "outbox": "sent",
            "paid_calls": 0,
            "platform_messages": 0,
        },
        "imported": {
            "version": "0.1.0",
            "distribution_version": "0.1.0",
            "persona_version": "1.0.0",
            "schema_version": 1,
            "python": release.platform.python_version(),
            "prefix": str(venv),
            "package_file": str(site_packages / "ginko/__init__.py"),
            "site_packages": str(site_packages),
            "distribution_root": str(site_packages),
            "direct_url": {"url": wheel.as_uri(), "archive_info": {}},
        },
        "persona": "identity\n\nstyle\n\n",
        "source": source,
        "venv": venv,
        "wheel": wheel,
    }


def test_offline_runtime_contract_accepts_isolated_install(runtime):
    evidence = release.validate_runtime(**runtime)
    assert evidence["isolated"] is True
    assert evidence["editable"] is False
    assert evidence["source_wheel"] == runtime["wheel"].name


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("doctor", "live_gateway", True),
        ("doctor", "persona_version", "0.0.0"),
        ("smoke", "mode", "live"),
        ("smoke", "deduplicated", False),
        ("smoke", "outbox", "unknown"),
        ("smoke", "paid_calls", False),
        ("smoke", "platform_messages", 1),
        ("imported", "distribution_version", "0.2.0"),
        ("imported", "schema_version", 2),
        ("imported", "package_file", "/source/src/ginko/__init__.py"),
        ("imported", "direct_url", {"dir_info": {"editable": True}}),
        ("imported", "direct_url", {"url": "file:///other/ginko-0.1.0-py3-none-any.whl"}),
    ],
)
def test_successful_exit_cannot_hide_invalid_runtime_evidence(runtime, section, field, value):
    runtime[section][field] = value
    with pytest.raises(release.VerificationError):
        release.validate_runtime(**runtime)


def test_persona_command_must_read_bundled_identity_and_style(runtime):
    runtime["persona"] = "placeholder"
    with pytest.raises(release.VerificationError, match="persona command"):
        release.validate_runtime(**runtime)


def test_install_report_keeps_isolation_evidence_without_machine_paths(
    runtime, tmp_path, monkeypatch
):
    work = tmp_path / "private-machine-user/install-check"
    work.mkdir(parents=True)
    venv = work / "venv"
    site_packages = venv / "lib/site-packages"
    imported = runtime["imported"] | {
        "prefix": str(venv),
        "package_file": str(site_packages / "ginko/__init__.py"),
        "site_packages": str(site_packages),
        "distribution_root": str(site_packages),
        "debug_path": str(work / "private-debug.log"),
    }

    def fake_run(command, cwd, env):
        if "export" in command:
            output = Path(command[command.index("--output-file") + 1])
            output.write_text("pydantic==2.13.5\n", encoding="utf-8")
        if command[-1] in ("doctor", "smoke"):
            return json.dumps(runtime[command[-1]])
        if command[-1] == "persona":
            return runtime["persona"]
        if "-c" in command:
            return json.dumps(imported)
        return ""

    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(release.shutil, "which", lambda name: "uv")
    monkeypatch.setattr(release, "TemporaryDirectory", lambda **kwargs: nullcontext(str(work)))
    result = release.verify_install(
        runtime["wheel"].parents[1], runtime["wheel"], runtime["source"]
    )
    evidence = result["isolated_import"]
    assert evidence["package_file"] == "lib/site-packages/ginko/__init__.py"
    assert evidence["source_wheel"] == "ginko-0.1.0-py3-none-any.whl"
    assert evidence["isolated"] is True and evidence["editable"] is False
    assert result["working_directory_outside_source"] is True
    serialized = json.dumps(result)
    assert "private-machine-user" not in serialized
    assert "file:///" not in serialized
    assert str(tmp_path) not in serialized
    assert not {"prefix", "site_packages", "distribution_root", "direct_url"} & evidence.keys()
    assert "working_directory" not in result


def test_failed_recheck_replaces_previous_success(source_tree, tmp_path, monkeypatch, capsys):
    source = release.read_source(source_tree)
    dist = tmp_path / "dist"
    for kind in ("wheel", "sdist"):
        write_artifact(dist, kind, artifact_files(source, kind))
    (dist / "verification.json").write_text('{"status": "passed"}', encoding="utf-8")
    (dist / "SHA256SUMS").write_text("stale hash", encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", source_tree)

    def fail_install(*args):
        raise release.VerificationError(
            f"installed smoke failed at {tmp_path}/private-machine-user"
        )

    monkeypatch.setattr(release, "verify_install", fail_install)
    assert release.main(["--dist-dir", str(dist)]) == 1
    report = json.loads((dist / "verification.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["error_type"] == "VerificationError"
    assert "private-machine-user" not in json.dumps(report)
    assert "private-machine-user" in capsys.readouterr().err
    assert not (dist / "SHA256SUMS").exists()
    assert len(list(dist.glob("ginko-*"))) == 2
