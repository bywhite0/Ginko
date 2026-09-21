"""Inspect local release artifacts and exercise the installed wheel, without publishing."""

import argparse
import ast
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PERSONA_FILES = {"manifest.toml", "identity.md", "style.md", "world.md"}
LICENSE_EXPRESSION = "AGPL-3.0-only AND LicenseRef-Ginko-Persona"
LICENSE_FILES = {"LICENSE", "NOTICE", "LICENSES/LicenseRef-Ginko-Persona.txt"}
SDIST_FILES = {
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "docs/architecture.md",
    "docs/roadmap.md",
    "docs/persona/README.md",
    "docs/licensing.md",
    ".python-version",
    "scripts/verify_release.py",
} | LICENSE_FILES
DIST_INFO_FILES = {"METADATA", "WHEEL", "entry_points.txt", "RECORD"}


class VerificationError(ValueError):
    """A release artifact or installed command did not meet the offline contract."""


@dataclass(frozen=True)
class Source:
    version: str
    persona_version: str
    persona_name: str
    schema_version: int
    files: dict[str, bytes]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def constant(data: bytes, name: str):
    for node in ast.parse(data).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise VerificationError(f"missing source constant: {name}")


def read_source(root: Path) -> Source:
    paths = SDIST_FILES | {
        path.relative_to(root).as_posix()
        for folder in (root / "src/ginko", root / "tests")
        for path in folder.rglob("*.py")
    }
    paths |= {f"src/ginko/personas/ginko/{name}" for name in PERSONA_FILES}
    files = {name: (root / name).read_bytes() for name in paths}
    if (root / ".gitignore").exists():
        files[".gitignore"] = (root / ".gitignore").read_bytes()
    project = tomllib.loads(files["pyproject.toml"].decode())["project"]
    locked = [
        package
        for package in tomllib.loads(files["uv.lock"].decode())["package"]
        if package["name"] == "ginko"
    ]
    if project["name"] != "ginko" or len(locked) != 1:
        raise VerificationError("expected exactly one ginko project in pyproject.toml and uv.lock")
    version = project["version"]
    if constant(files["src/ginko/__init__.py"], "__version__") != version:
        raise VerificationError("source package version differs from pyproject.toml")
    if locked[0]["version"] != version:
        raise VerificationError("uv.lock version differs from pyproject.toml")
    persona = tomllib.loads(files["src/ginko/personas/ginko/manifest.toml"].decode())
    return Source(
        version,
        persona["version"],
        persona["display_name"],
        constant(files["src/ginko/storage/database.py"], "SCHEMA_VERSION"),
        files,
    )


def check_path(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or "\\" in name
        or ":" in name
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise VerificationError(f"unsafe archive path: {name}")
    if any(
        part in {"__pycache__", ".cache", ".venv", "data", "logs", "config", "configs"}
        or part.startswith(".env")
        for part in path.parts
    ):
        raise VerificationError(f"private or runtime file in archive: {name}")


def read_archive(path: Path) -> dict[str, bytes]:
    files = {}
    seen = set()
    directories = set()
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                name = member.filename.rstrip("/")
                check_path(name)
                if name in seen or stat.S_ISLNK(member.external_attr >> 16):
                    raise VerificationError(f"duplicate or linked archive member: {name}")
                seen.add(name)
                if member.is_dir():
                    directories.add(name)
                else:
                    files[name] = archive.read(member)
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                check_path(name)
                if name in seen or not (member.isfile() or member.isdir()):
                    raise VerificationError(f"duplicate or nonregular archive member: {name}")
                seen.add(name)
                if member.isdir():
                    directories.add(name)
                else:
                    with archive.extractfile(member) as stream:
                        files[name] = stream.read()
    parents = {str(parent) for name in files for parent in PurePosixPath(name).parents}
    if unexpected := directories - parents:
        raise VerificationError(f"unexpected archive directories: {', '.join(sorted(unexpected))}")
    return files


def check_metadata(data: bytes, version: str) -> None:
    metadata = BytesParser().parsebytes(data)
    if metadata.get_all("Name") != ["ginko"] or metadata.get_all("Version") != [version]:
        raise VerificationError("archive metadata name or version differs from the project")
    if metadata.get_all("License-Expression") != [LICENSE_EXPRESSION]:
        raise VerificationError(
            "archive license expression does not preserve code and persona scope"
        )
    if sorted(metadata.get_all("License-File", [])) != sorted(LICENSE_FILES):
        raise VerificationError("archive license files differ from the required license notices")
    if "License" in metadata:
        raise VerificationError("archive contains a legacy license field alongside its expression")


def inspect_artifact(path: Path, source: Source) -> dict:
    files = read_archive(path)
    if path.suffix == ".whl":
        if path.name != f"ginko-{source.version}-py3-none-any.whl":
            raise VerificationError(f"unexpected wheel filename or version: {path.name}")
        expected = {
            name.removeprefix("src/"): data
            for name, data in source.files.items()
            if name.startswith("src/ginko/")
        }
        info = f"ginko-{source.version}.dist-info/"
        expected.update({info + "licenses/" + name: source.files[name] for name in LICENSE_FILES})
        metadata_name = info + "METADATA"
        allowed = expected.keys() | {info + name for name in DIST_INFO_FILES}
        required = allowed
    else:
        if path.name != f"ginko-{source.version}.tar.gz":
            raise VerificationError(f"unexpected sdist filename or version: {path.name}")
        prefix = f"ginko-{source.version}/"
        expected = {prefix + name: data for name, data in source.files.items()}
        metadata_name = prefix + "PKG-INFO"
        allowed = expected.keys() | {metadata_name}
        required = allowed - {prefix + ".gitignore"}
    if unexpected := files.keys() - allowed:
        raise VerificationError(f"unexpected archive files: {', '.join(sorted(unexpected))}")
    if missing := required - files.keys():
        raise VerificationError(f"missing archive files: {', '.join(sorted(missing))}")
    check_metadata(files[metadata_name], source.version)
    for name, data in expected.items():
        if name in files and files[name] != data:
            raise VerificationError(f"archive content differs from current source: {name}")
    return {
        "filename": path.name,
        "sha256": sha256(path.read_bytes()),
        "size": path.stat().st_size,
        "files": [
            {"path": name, "size": len(data), "sha256": sha256(data)}
            for name, data in sorted(files.items())
        ],
    }


def run(command: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300
    )
    if result.returncode:
        raise VerificationError(f"command failed: {command[0:3]}\n{result.stderr or result.stdout}")
    return result.stdout


def check_fields(actual: dict, expected: dict, label: str) -> None:
    if not isinstance(actual, dict):
        raise VerificationError(f"{label}: expected a JSON object")
    for name, value in expected.items():
        if type(actual.get(name)) is not type(value) or actual[name] != value:
            raise VerificationError(f"{label}: invalid {name}: {actual.get(name)!r}")


def validate_runtime(
    doctor: dict, smoke: dict, imported: dict, persona: str, source: Source, venv: Path, wheel: Path
) -> dict:
    check_fields(
        doctor,
        {
            "version": source.version,
            "stage": "foundation",
            "persona": source.persona_name,
            "persona_version": source.persona_version,
            "live_gateway": False,
            "live_model": False,
        },
        "doctor",
    )
    check_fields(
        smoke,
        {
            "mode": "offline",
            "version": source.version,
            "persona": source.persona_name,
            "deduplicated": True,
            "inbox": "done",
            "outbox": "sent",
            "paid_calls": 0,
            "platform_messages": 0,
        },
        "smoke",
    )
    check_fields(
        imported,
        {
            "version": source.version,
            "distribution_version": source.version,
            "schema_version": source.schema_version,
            "persona_version": source.persona_version,
            "python": platform.python_version(),
        },
        "isolated import",
    )
    prefix = Path(imported["prefix"]).resolve()
    package_file = Path(imported["package_file"]).resolve()
    site_packages = Path(imported["site_packages"]).resolve()
    if (
        prefix != venv.resolve()
        or not site_packages.is_relative_to(prefix)
        or not package_file.is_relative_to(site_packages)
        or Path(imported["distribution_root"]).resolve() != site_packages
        or imported["direct_url"].get("dir_info", {}).get("editable", False)
    ):
        raise VerificationError("installed package is not isolated in the clean venv site-packages")
    if imported["direct_url"].get("url") != wheel.resolve().as_uri():
        raise VerificationError("installed distribution source differs from the verified wheel")
    expected_persona = "\n".join(
        source.files[f"src/ginko/personas/ginko/{name}.md"].decode().replace("\r\n", "\n")
        for name in ("identity", "style")
    )
    if persona.strip() != expected_persona.strip():
        raise VerificationError("persona command differs from bundled identity and style")
    return {
        key: imported[key]
        for key in (
            "version",
            "distribution_version",
            "persona_version",
            "schema_version",
            "python",
        )
    } | {
        "package_file": package_file.relative_to(prefix).as_posix(),
        "source_wheel": wheel.name,
        "isolated": True,
        "editable": False,
    }


IMPORT_PROBE = """
import importlib.metadata, json, pathlib, platform, sys, sysconfig
import ginko
from ginko.persona import load_ginko
from ginko.storage.database import SCHEMA_VERSION
distribution = importlib.metadata.distribution("ginko")
print(json.dumps({
    "version": ginko.__version__,
    "distribution_version": distribution.version,
    "persona_version": load_ginko().version,
    "schema_version": SCHEMA_VERSION,
    "python": platform.python_version(),
    "prefix": sys.prefix,
    "package_file": str(pathlib.Path(ginko.__file__).resolve()),
    "site_packages": sysconfig.get_path("purelib"),
    "distribution_root": str(distribution.locate_file("")),
    "direct_url": json.loads(distribution.read_text("direct_url.json")),
}))
"""


def verify_install(root: Path, wheel: Path, source: Source) -> dict:
    uv = shutil.which("uv")
    if uv is None:
        raise VerificationError("uv is required for clean installation verification")
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(name, None)
    env.update(PYTHONUTF8="1", PYTHONNOUSERSITE="1", UV_NO_PROGRESS="1")
    with TemporaryDirectory(prefix="ginko-release-") as directory:
        work = Path(directory).resolve()
        if work.is_relative_to(root.resolve()):
            raise VerificationError("temporary directory must be outside the source checkout")
        venv = work / "venv"
        requirements = work / "requirements.txt"
        run(
            [
                uv,
                "export",
                "--project",
                str(root),
                "--locked",
                "--no-dev",
                "--no-default-groups",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            work,
            env,
        )
        run([uv, "venv", "--python", sys.executable, str(venv)], work, env)
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        executable = scripts / ("ginko.exe" if os.name == "nt" else "ginko")
        install = [uv, "pip", "install", "--python", str(python), "--no-deps"]
        run(install + ["--require-hashes", "-r", str(requirements)], work, env)
        run(install + [str(wheel)], work, env)
        run([uv, "pip", "check", "--python", str(python)], work, env)
        doctor = json.loads(run([str(executable), "doctor"], work, env))
        smoke = json.loads(run([str(executable), "smoke"], work, env))
        persona = run([str(executable), "persona"], work, env)
        imported = json.loads(run([str(python), "-I", "-c", IMPORT_PROBE], work, env))
        evidence = validate_runtime(doctor, smoke, imported, persona, source, venv, wheel)
        return {
            "doctor": doctor,
            "smoke": smoke,
            "persona": {"matches_bundled_resources": True, "sha256": sha256(persona.encode())},
            "isolated_import": evidence,
            "requirements_sha256": sha256(requirements.read_bytes()),
            "working_directory_outside_source": True,
            "environment_removed_after_check": True,
        }


def git_state(root: Path) -> dict:
    if shutil.which("git") is None or not (root / ".git").exists():
        return {"head": None, "dirty": None}
    return {
        "head": run(["git", "rev-parse", "HEAD"], root, dict(os.environ)).strip(),
        "dirty": bool(run(["git", "status", "--porcelain"], root, dict(os.environ)).strip()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    dist = args.dist_dir.resolve()
    dist.mkdir(parents=True, exist_ok=True)
    report_path = dist / "verification.json"
    checksums_path = dist / "SHA256SUMS"
    report = {
        "status": "running",
        "purpose": "local artifact verification; not a formal release",
        "checked_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    checksums_path.unlink(missing_ok=True)
    try:
        source = read_source(ROOT)
        wheels = sorted(dist.glob("*.whl"))
        sdists = sorted(dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise VerificationError("dist must contain exactly one ginko wheel and one ginko sdist")
        report.update(
            version=source.version,
            persona_version=source.persona_version,
            schema_version=source.schema_version,
            lock_sha256=sha256(source.files["uv.lock"]),
            git=git_state(ROOT),
        )
        report["artifacts"] = [inspect_artifact(path, source) for path in (wheels[0], sdists[0])]
        report["installed"] = verify_install(ROOT, wheels[0], source)
        checksums_path.write_text(
            "".join(f"{item['sha256']}  {item['filename']}\n" for item in report["artifacts"]),
            encoding="utf-8",
        )
        report["status"] = "passed"
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        tarfile.TarError,
        zipfile.BadZipFile,
        subprocess.TimeoutExpired,
    ) as error:
        report.update(
            status="failed",
            error="Verification failed; see stderr for details.",
            error_type=type(error).__name__,
        )
        print(f"Release verification failed: {error}", file=sys.stderr)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if report["status"] != "passed":
        return 1
    print(f"Local artifact verification passed: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
