#!/usr/bin/env python3
"""
deb_mirror.py – Debian/Ubuntu repository mirroring tool (debmirror-based).

Config driven via YAML. Supports a global GPG key store so keys are
downloaded/imported once and referenced by name from any mirror entry.
"""

import argparse
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
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.rule import Rule
from rich.table import Table

console = Console()

# ---------------------------------------------------------------------------
# Helpers
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


def download_file(url: str, dest: Path) -> None:
    console.print(f"  [cyan]Downloading[/cyan] {url} → {dest}")
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as e:
        raise RuntimeError(f"Failed to download {url}: {e}") from e


def import_gpg_key(key_path: Path, keyring: Path) -> None:
    console.print(f"  [cyan]Importing GPG key[/cyan] {key_path.name} → {keyring}")
    run(["gpg", "--no-default-keyring", "--keyring", str(keyring.resolve()),
         "--import", str(key_path)])


# ---------------------------------------------------------------------------
# Global GPG key store
#
# YAML structure (top-level):
#
#   gpg-keys:
#     store-path: /etc/mirror/gpg-keys     # zentrales Verzeichnis
#     keys:
#       debian-archive-12:
#         url: https://ftp-master.debian.org/keys/archive-key-12.asc
#       mein-interner-key:
#         (kein url-Feld → Datei muss schon als <store-path>/<name>.gpg liegen)
#
# deb-mirrors referenzieren per Name:  gpg-key: debian-archive-12
# ---------------------------------------------------------------------------

class GpgKeyStore:
    def __init__(self, cfg: dict) -> None:
        raw_path = cfg.get("store-path", "/etc/mirror/gpg-keys")
        self.store_path: Path = ensure_dir(raw_path)
        self.keys: dict[str, dict] = cfg.get("keys") or {}

    def resolve(self, key_name: str) -> Path:
        if key_name not in self.keys:
            raise KeyError(f"GPG key '{key_name}' not defined in gpg-keys.keys")

        key_cfg  = self.keys[key_name]
        url      = key_cfg.get("url")
        gpg_path = self.store_path / f"{key_name}.gpg"

        if not url:
            if not gpg_path.exists():
                raise FileNotFoundError(
                    f"No url for key '{key_name}' and {gpg_path} does not exist"
                )
            return gpg_path

        asc_path = self.store_path / f"{key_name}.asc"
        download_file(url, asc_path)
        import_gpg_key(asc_path, gpg_path)
        return gpg_path

    def is_empty(self) -> bool:
        return not self.keys


# ---------------------------------------------------------------------------
# Deb mirrors  (debmirror)
# ---------------------------------------------------------------------------

DEBMIRROR_DEFAULTS = {
    "method":  "http",   # debmirror --method: http | ftp | rsync | ssh
    "arch":    "amd64",
    "dist":    "stable",
    "section": "main",
    "i10n":    True,
    "sources": False,
}


def mirror_deb(name: str, cfg: dict, gpg_store: GpgKeyStore) -> None:
    console.print(Rule(f"[bold blue]deb mirror: {name}"))

    target = ensure_dir(cfg["target-path"])

    # --- GPG key resolution ---------------------------------------------
    # Priority:
    #   1. gpg-key: <name>      → look up in global store
    #   2. gpg-key-url: <url>   → ad-hoc download into target-path
    #   3. existing trustedkeys.gpg in target-path
    keyring: Optional[Path] = None

    gpg_key_name = cfg.get("gpg-key")
    gpg_key_url  = cfg.get("gpg-key-url")

    if gpg_key_name:
        keyring = gpg_store.resolve(gpg_key_name)
    elif gpg_key_url:
        asc_path = target / f"{name}.asc"
        download_file(gpg_key_url, asc_path)
        keyring = target / "trustedkeys.gpg"
        import_gpg_key(asc_path, keyring)
    else:
        fallback = target / "trustedkeys.gpg"
        if fallback.exists():
            console.print("  [yellow]No gpg-key configured, using existing trustedkeys.gpg[/yellow]")
            keyring = fallback
        else:
            console.print("  [yellow]Warning: no GPG key configured and no existing keyring found[/yellow]")

    # --- Build debmirror command -----------------------------------------
    host    = cfg["path"]
    method  = cfg.get("method",  DEBMIRROR_DEFAULTS["method"])
    arch    = cfg.get("arch",    DEBMIRROR_DEFAULTS["arch"])
    dist    = cfg.get("dist",    DEBMIRROR_DEFAULTS["dist"])
    section = cfg.get("section", DEBMIRROR_DEFAULTS["section"])
    i10n    = cfg.get("i10n",    DEBMIRROR_DEFAULTS["i10n"])
    sources = cfg.get("sources", DEBMIRROR_DEFAULTS["sources"])

    cmd = [
        "debmirror",
        f"--host={host}",
        f"--method={method}",
        "--root=/",
        f"--dist={dist}",
        f"--section={section}",
        f"--arch={arch}",
    ]

    if keyring and keyring.exists():
        cmd.append(f"--keyring={keyring.resolve()}")

    if i10n:
        cmd.append("--i18n")

    if not sources:
        cmd.append("--nosource")

    extra = cfg.get("debmirror-options", "")
    if isinstance(extra, str) and extra.strip():
        cmd.extend(extra.split())
    elif isinstance(extra, list):
        cmd.extend(extra)

    cmd.append(str(target))
    run(cmd)
    console.print(f"  [green]✓ deb mirror '{name}' done[/green]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def print_summary(config: dict, gpg_store: GpgKeyStore) -> None:
    if not gpg_store.is_empty():
        kt = Table(title="GPG key store", show_header=True, header_style="bold")
        kt.add_column("Name")
        kt.add_column("URL / source")
        for kname, kcfg in gpg_store.keys.items():
            kt.add_row(kname, kcfg.get("url", "(local)"))
        console.print(kt)
        console.print(f"  store-path: [dim]{gpg_store.store_path}[/dim]\n")

    mt = Table(title="Configured deb mirrors", show_header=True, header_style="bold")
    mt.add_column("Name")
    mt.add_column("Host/Path")
    mt.add_column("Dist")
    mt.add_column("GPG key", style="dim")

    for name, cfg in (config.get("deb-mirrors") or {}).items():
        mt.add_row(
            name,
            cfg.get("path", ""),
            cfg.get("dist", DEBMIRROR_DEFAULTS["dist"]),
            cfg.get("gpg-key", "-"),
        )

    console.print(mt)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Debian/Ubuntu repository mirror tool (debmirror)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  deb_mirror.py config.yaml
  deb_mirror.py config.yaml --only debian-stable
  deb_mirror.py config.yaml --dry-run
  deb_mirror.py config.yaml --list
""",
    )
    parser.add_argument("config", help="Path to YAML config file")
    parser.add_argument("--only", "-o", nargs="+", metavar="NAME",
                        help="Run only these mirror entries (by name)")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List all configured mirrors and exit")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Print what would be done without executing")

    args   = parser.parse_args()
    config = load_config(args.config)
    gpg_store = GpgKeyStore(config.get("gpg-keys") or {})

    if args.list:
        print_summary(config, gpg_store)
        return 0

    entries = config.get("deb-mirrors") or {}

    if args.only:
        unknown = set(args.only) - set(entries)
        if unknown:
            console.print(f"[red]Unknown mirror name(s): {', '.join(unknown)}[/red]")
            console.print(f"Available: {', '.join(entries)}")
            return 1
        entries = {k: v for k, v in entries.items() if k in args.only}

    if not entries:
        console.print("[yellow]No deb-mirrors configured (or filter matched nothing).[/yellow]")
        return 0

    console.print(Panel(
        f"[bold]deb_mirror.py[/bold]  –  {len(entries)} mirror(s) to run",
        subtitle=f"config: {args.config}",
    ))

    errors: list[str] = []

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
        overall = progress.add_task("[bold]Overall progress", total=len(entries))

        for name, cfg in entries.items():
            progress.update(overall, description=f"[bold]{name}[/bold]")

            if args.dry_run:
                console.print(f"  [dim]DRY-RUN[/dim] would run deb-mirrors/{name}")
            else:
                try:
                    mirror_deb(name, cfg, gpg_store)
                except Exception as exc:
                    msg = f"{name}: {exc}"
                    console.print(f"  [red]ERROR[/red] {msg}")
                    errors.append(msg)

            progress.advance(overall)

    console.print()
    if errors:
        console.print(Panel(
            "\n".join(f"• {e}" for e in errors),
            title="[red]Errors[/red]",
            border_style="red",
        ))
        return 1

    console.print(Panel("[green]All deb mirrors completed successfully.[/green]"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
