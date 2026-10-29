"""Build an allowlisted AstrBot plugin ZIP, never the local runtime directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

import yaml

ROOT_FILES = {
    "__init__.py",
    "main.py",
    "completion_monitor.py",
    "desktop_client.py",
    "remote_control.py",
    "_conf_schema.json",
    "metadata.yaml",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "SECURITY.md",
    "MARKET_REVIEW.md",
    "pyproject.toml",
    "requirements-dev.txt",
    ".gitignore",
    ".gitattributes",
}


def build(root: Path, output: Path):
    metadata = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
    for key in ("name", "author", "version", "repo", "desc"):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError(f"Missing metadata: {key}")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", metadata["repo"]):
        raise ValueError("Expected a GitHub repository URL")
    if not re.fullmatch(r"\d+\.\d+\.\d+", metadata["version"]):
        raise ValueError("Expected a semantic release version")
    config = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    for key in (
        "enable_codex_reply",
        "enable_codex_create",
        "allow_codex_running_reply",
        "codex_notifications_in_context",
    ):
        if config[key]["default"] is not False:
            raise ValueError(f"Expected explicit opt-in: {key}")
    selected = [root / name for name in ROOT_FILES if (root / name).is_file()]
    selected += sorted((root / "tools").glob("*.py"))
    selected += sorted((root / "tests").glob("*.py"))
    selected += sorted((root / ".github/workflows").glob("*.yml"))
    for path in selected:
        if path.is_symlink():
            raise ValueError("Do not package symlinks")
        text = path.read_text(encoding="utf-8")
        if re.search(r"(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})", text):
            raise ValueError("Potential credential in publication source")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{metadata['name']}-{metadata['version']}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(selected):
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            handle.writestr(info, path.read_text(encoding="utf-8").encode("utf-8"))
    if archive.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("AstrBot marketplace ZIP size limit exceeded")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive.name + ".sha256")).write_text(
        digest + "  " + archive.name + "\n", encoding="utf-8"
    )
    manifest = {
        "archive": archive.name,
        "sha256": digest,
        "bytes": archive.stat().st_size,
        "files": [p.relative_to(root).as_posix() for p in sorted(selected)],
    }
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    result = build(Path(__file__).resolve().parents[1], args.output.resolve())
    print(json.dumps({k: v for k, v in result.items() if k != "files"}, ensure_ascii=False))
