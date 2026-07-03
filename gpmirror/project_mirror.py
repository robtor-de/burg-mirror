#!/usr/bin/env python3
"""
project_mirror.py – Project-oriented offline mirroring tool.

Mirrors everything a self-hosted application needs in one shot:
  - Docker images (via skopeo)
  - Source / doc git repositories (bare mirrors)
  - Arbitrary files / binary releases (GitHub Releases API or generic
    URL templates), with automatic "latest" resolution and
    skip-if-exists / --force semantics.

Example use case: mirroring everything needed to deploy Immich offline
(postgres, redis, the immich server image, the Android APK, ML models, …)
in a single project definition.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.rule import Rule
from rich.table import Table

console = Console()

GITHUB_API = "https://api.github.com"

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    console.print(f"  [dim]$ {' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd, text=True, capture_output=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")
    return result


def ensure_dir(path: str | Path) -> Path:
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "project_mirror.py"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_with_progress(url: str, dest: Path, progress: Progress) -> None:
    """Stream a download into dest, showing a rich progress bar."""
    req = urllib.request.Request(url, headers={"User-Agent": "project_mirror.py"})
    tmp = dest.with_suffix(dest.suffix + ".part")

    with urllib.request.urlopen(req) as resp:
        total = int(resp.headers.get("Content-Length", 0)) or None
        task = progress.add_task(f"  {dest.name}", total=total)
        with tmp.open("wb") as out:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                progress.advance(task, len(chunk))
        progress.remove_task(task)

    tmp.rename(dest)


# ---------------------------------------------------------------------------
# Docker images  (skopeo) – same approach as the general mirror tool
# ---------------------------------------------------------------------------

def mirror_image(image_cfg: dict, target_dir: Path) -> None:
    image = image_cfg["image"]          # (*) full reference, e.g. docker.io/library/postgres
    tag   = image_cfg.get("tag", "latest")

    if ":" in image.split("/")[-1]:
        src_ref    = f"docker://{image}"
        image_slug = image.replace("/", "_").replace(":", "_")
    else:
        src_ref    = f"docker://{image}:{tag}"
        image_slug = f"{image.replace('/', '_')}_{tag}"

    dest_dir = target_dir / "docker" / image_slug
    dest_ref = f"dir:{dest_dir}"

    if dest_dir.exists() and not image_cfg.get("force"):
        console.print(f"  [dim]skip (exists)[/dim] {image_slug}")
        return

    console.print(f"  [cyan]Copying[/cyan] {src_ref} → {dest_ref}")
    run(["skopeo", "copy", "--all", src_ref, dest_ref])
    console.print(f"  [green]✓[/green] {image_slug}")


# ---------------------------------------------------------------------------
# Git repos (source / doc) – bare mirrors
# ---------------------------------------------------------------------------

def mirror_repo(repo_cfg: dict, target_dir: Path) -> None:
    url  = repo_cfg["url"]   # (*)
    name = repo_cfg.get("name") or url.rstrip("/").split("/")[-1].removesuffix(".git")

    bare_path = target_dir / "git" / f"{name}.git"
    ensure_dir(bare_path.parent)

    if (bare_path / "HEAD").exists():
        console.print(f"  [cyan]Updating[/cyan] {name}.git")
        run(["git", "--git-dir", str(bare_path), "remote", "update", "--prune"])
    else:
        console.print(f"  [cyan]Cloning (bare)[/cyan] {url} → {bare_path}")
        run(["git", "clone", "--mirror", url, str(bare_path)])

    console.print(f"  [green]✓[/green] {name}.git")


# ---------------------------------------------------------------------------
# File / binary release downloads
#
# Two source modes:
#
#   1. GitHub Releases API:
#        source: github
#        repo: owner/name
#        version: "v1.2.3" | "latest"
#        asset-pattern: "regex matched against release asset filenames"
#
#   2. Generic URL template:
#        source: url
#        url-template: "https://example.com/dl/{version}/app-{version}.apk"
#        version: "1.2.3" | requires version-check-url for "latest"
#        version-check-url: optional URL returning the latest version string
#        version-check-jsonpath: dotted path into a JSON response, e.g. "tag_name"
# ---------------------------------------------------------------------------

def _resolve_github_release(repo: str, version: str) -> dict:
    if version == "latest":
        url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    else:
        url = f"{GITHUB_API}/repos/{repo}/releases/tags/{version}"
    return http_get_json(url)


def _pick_asset(release: dict, pattern: Optional[str]) -> dict:
    assets = release.get("assets", [])
    if not assets:
        raise RuntimeError(f"Release {release.get('tag_name')} has no assets")

    if not pattern:
        if len(assets) > 1:
            names = ", ".join(a["name"] for a in assets)
            raise RuntimeError(
                f"Multiple assets found ({names}) – set 'asset-pattern' to disambiguate"
            )
        return assets[0]

    rx = re.compile(pattern)
    matches = [a for a in assets if rx.search(a["name"])]
    if not matches:
        names = ", ".join(a["name"] for a in assets)
        raise RuntimeError(f"No asset matched pattern '{pattern}'. Available: {names}")
    if len(matches) > 1:
        names = ", ".join(a["name"] for a in matches)
        raise RuntimeError(f"Pattern '{pattern}' matched multiple assets: {names}")
    return matches[0]


def _resolve_generic_version(file_cfg: dict) -> str:
    version = file_cfg.get("version", "latest")
    if version != "latest":
        return version

    check_url = file_cfg.get("version-check-url")
    if not check_url:
        raise RuntimeError(
            "version: latest requires 'version-check-url' for source: url"
        )

    data = http_get_json(check_url)
    jsonpath = file_cfg.get("version-check-jsonpath", "tag_name")
    value = data
    for part in jsonpath.split("."):
        value = value[part]
    return str(value).lstrip("v")


def mirror_file(name: str, file_cfg: dict, target_dir: Path, progress: Progress,
                 force: bool = False) -> None:
    source = file_cfg.get("source", "url")
    dest_dir = ensure_dir(target_dir / "files")

    if source == "github":
        repo    = file_cfg["repo"]            # (*) owner/name
        version = file_cfg.get("version", "latest")
        pattern = file_cfg.get("asset-pattern")

        release   = _resolve_github_release(repo, version)
        resolved_v = release.get("tag_name", version)
        asset      = _pick_asset(release, pattern)
        download_url = asset["browser_download_url"]
        filename     = file_cfg.get("filename", asset["name"])

    elif source == "url":
        template = file_cfg["url-template"]   # (*)
        version  = _resolve_generic_version(file_cfg)
        resolved_v = version
        download_url = template.format(version=version)
        filename = file_cfg.get(
            "filename", download_url.rstrip("/").split("/")[-1]
        )

    else:
        raise ValueError(f"Unknown source type '{source}' for file '{name}'")

    dest_path = dest_dir / filename

    if dest_path.exists() and not force and not file_cfg.get("force"):
        console.print(f"  [dim]skip (exists)[/dim] {filename}  (version {resolved_v})")
        return

    console.print(f"  [cyan]Downloading[/cyan] {filename}  (version {resolved_v})")
    download_with_progress(download_url, dest_path, progress)

    expected_sha = file_cfg.get("sha256")
    if expected_sha:
        actual = sha256_of(dest_path)
        if actual.lower() != expected_sha.lower():
            dest_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"SHA256 mismatch for {filename}: expected {expected_sha}, got {actual}"
            )
        console.print("  [dim]sha256 OK[/dim]")

    console.print(f"  [green]✓[/green] {filename}")


# ---------------------------------------------------------------------------
# doc-repo: append a manifest entry to a shared documentation repo
# ---------------------------------------------------------------------------

def update_doc_repo(doc_repo_url: str, project_name: str, target_dir: Path) -> None:
    gitmirror_path = target_dir.parent / "_doc-mirror"

    if (gitmirror_path / ".git").exists():
        console.print("  [dim]Updating doc-repo[/dim]")
        run(["git", "-C", str(gitmirror_path), "pull", "--ff-only"], check=False)
    else:
        console.print(f"  [dim]Cloning doc-repo[/dim] {doc_repo_url}")
        run(["git", "clone", doc_repo_url, str(gitmirror_path)], check=False)

    manifest = gitmirror_path / "projects.txt"
    existing = manifest.read_text() if manifest.exists() else ""
    if project_name not in existing:
        with manifest.open("a") as f:
            f.write(f"{project_name}\n")
        run(["git", "-C", str(gitmirror_path), "add", "projects.txt"], check=False)
        run(["git", "-C", str(gitmirror_path), "commit", "-m",
             f"mirror: add project {project_name}"], check=False)
        run(["git", "-C", str(gitmirror_path), "push"], check=False)


# ---------------------------------------------------------------------------
# Project runner
# ---------------------------------------------------------------------------

def mirror_project(name: str, cfg: dict, base_dir: Path, force: bool) -> list[str]:
    console.print(Rule(f"[bold blue]project: {name}"))

    target_dir = ensure_dir(base_dir / name)
    errors: list[str] = []

    # --- Docker images ----------------------------------------------------
    images = cfg.get("docker-images") or []
    for img_cfg in images:
        try:
            if force:
                img_cfg = {**img_cfg, "force": True}
            mirror_image(img_cfg, target_dir)
        except Exception as exc:
            errors.append(f"{name}/docker/{img_cfg.get('image')}: {exc}")
            console.print(f"  [red]ERROR[/red] {exc}")

    # --- Source / doc git repos --------------------------------------------
    for repo_cfg in cfg.get("repos") or []:
        try:
            mirror_repo(repo_cfg, target_dir)
        except Exception as exc:
            errors.append(f"{name}/repo/{repo_cfg.get('url')}: {exc}")
            console.print(f"  [red]ERROR[/red] {exc}")

    # --- Files / binary releases --------------------------------------------
    files = cfg.get("files") or {}
    if files:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as file_progress:
            for fname, file_cfg in files.items():
                try:
                    mirror_file(fname, file_cfg, target_dir, file_progress, force=force)
                except Exception as exc:
                    errors.append(f"{name}/file/{fname}: {exc}")
                    console.print(f"  [red]ERROR[/red] {fname}: {exc}")

    # --- Doc repo -----------------------------------------------------------
    doc_repo = cfg.get("doc-repo")
    if doc_repo:
        try:
            update_doc_repo(doc_repo, name, target_dir)
        except Exception as exc:
            errors.append(f"{name}/doc-repo: {exc}")

    if not errors:
        console.print(f"  [green]✓ project '{name}' complete[/green]")
    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def print_summary(config: dict) -> None:
    projects = config.get("projects") or {}
    base_dir = config.get("base-dir", "/srv/mirrors/projects")

    table = Table(title=f"Configured projects (base-dir: {base_dir})",
                  show_header=True, header_style="bold")
    table.add_column("Project")
    table.add_column("Docker images")
    table.add_column("Repos")
    table.add_column("Files")
    table.add_column("doc-repo", style="dim")

    for name, cfg in projects.items():
        n_images = len(cfg.get("docker-images") or [])
        n_repos  = len(cfg.get("repos") or [])
        n_files  = len(cfg.get("files") or {})
        table.add_row(
            name, str(n_images), str(n_repos), str(n_files),
            "yes" if cfg.get("doc-repo") else "-",
        )

    console.print(table)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Project-oriented offline mirror tool (Docker + Git + file/release downloads)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  project_mirror.py config.yaml
  project_mirror.py config.yaml --only immich
  project_mirror.py config.yaml --only immich paperless --force
  project_mirror.py config.yaml --list
""",
    )
    parser.add_argument("config", help="Path to YAML config file")
    parser.add_argument("--only", "-o", nargs="+", metavar="PROJECT",
                        help="Run only these project(s) by name")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List all configured projects and exit")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Re-download/re-copy even if target already exists")

    args   = parser.parse_args()
    config = load_config(args.config)

    if args.list:
        print_summary(config)
        return 0

    base_dir = ensure_dir(config.get("base-dir", "/srv/mirrors/projects"))
    projects = config.get("projects") or {}

    if args.only:
        unknown = set(args.only) - set(projects)
        if unknown:
            console.print(f"[red]Unknown project name(s): {', '.join(unknown)}[/red]")
            console.print(f"Available: {', '.join(projects)}")
            return 1
        projects = {k: v for k, v in projects.items() if k in args.only}

    if not projects:
        console.print("[yellow]No projects configured (or filter matched nothing).[/yellow]")
        return 0

    console.print(Panel(
        f"[bold]project_mirror.py[/bold]  –  {len(projects)} project(s) to run",
        subtitle=f"config: {args.config}  |  base-dir: {base_dir}",
    ))

    all_errors: list[str] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        overall = progress.add_task("[bold]Overall progress", total=len(projects))

        for name, cfg in projects.items():
            progress.update(overall, description=f"[bold]{name}[/bold]")
            errors = mirror_project(name, cfg, base_dir, args.force)
            all_errors.extend(errors)
            progress.advance(overall)

    console.print()
    if all_errors:
        console.print(Panel(
            "\n".join(f"• {e}" for e in all_errors),
            title="[red]Errors[/red]",
            border_style="red",
        ))
        return 1

    console.print(Panel("[green]All projects mirrored successfully.[/green]"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
