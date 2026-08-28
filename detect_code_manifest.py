#!/usr/bin/env python3
"""
Walk a git repository N levels deep and report, for every directory
encountered, what kind of code lives there (by file extension) and whether a
dependency manifest / lockfile pair was found.

Ecosystem detection mirrors how Semgrep's supply chain scanner identifies
"subprojects": it does not do a generic directory inspection, it looks for a
fixed list of known manifest/lockfile filenames per ecosystem. Unlike the
scanner, this tool reports on every directory it visits -- including ones
with no manifest at all, or a manifest but no lockfile -- and flags whether
that directory would currently produce a scannable subproject:

  * lockfile present (with or without a manifest)      -> scannable
  * manifest-only, and the ecosystem allows manifest-only
    subprojects (NuGet, Maven, Python/setup.py, Gradle, SBT) -> scannable
  * manifest-only, everything else (npm/yarn/pnpm/bun, RubyGems,
    Go, Cargo, Composer, Pub, SwiftPM, Hex, Pipenv/Poetry/uv)  -> NOT scannable
  * nothing recognized                                   -> NOT scannable

Once a directory has a full manifest+lockfile pair for some ecosystem, its
subdirectories are not visited -- this matches the ask to skip descending
into an already-resolved dependency module.

File enumeration goes through `git ls-files` (tracked, plus untracked-but-
not-ignored) rather than a raw filesystem walk, so .gitignore'd directories
like node_modules/ or vendor/ are skipped the way they would be in a real
repo scan.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------------------
# Ecosystem definitions: manifest/lockfile filenames Semgrep's supply chain
# scanner recognizes, one entry per ecosystem/package-manager pairing.
# "manifest_only_ok" mirrors which ecosystems Semgrep will still create a
# subproject for when only the manifest (no lockfile) is present.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ecosystem:
    name: str
    language: str
    manifests: frozenset  # filenames, may include glob patterns like "*.csproj"
    lockfiles: frozenset
    manifest_only_ok: bool


ECOSYSTEMS = [
    Ecosystem("npm/yarn/pnpm/bun", "JavaScript/TypeScript",
              frozenset({"package.json"}),
              frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb"}),
              manifest_only_ok=False),
    Ecosystem("RubyGems", "Ruby",
              frozenset({"Gemfile"}),
              frozenset({"Gemfile.lock"}),
              manifest_only_ok=False),
    Ecosystem("Go modules", "Go",
              frozenset({"go.mod"}),
              frozenset({"go.sum"}),
              manifest_only_ok=False),
    Ecosystem("Cargo", "Rust",
              frozenset({"Cargo.toml"}),
              frozenset({"Cargo.lock"}),
              manifest_only_ok=False),
    Ecosystem("Composer", "PHP",
              frozenset({"composer.json"}),
              frozenset({"composer.lock"}),
              manifest_only_ok=False),
    Ecosystem("Pub", "Dart",
              frozenset({"pubspec.yaml"}),
              frozenset({"pubspec.lock"}),
              manifest_only_ok=False),
    Ecosystem("SwiftPM", "Swift",
              frozenset({"Package.swift"}),
              frozenset({"Package.resolved"}),
              manifest_only_ok=False),
    Ecosystem("Hex", "Elixir",
              frozenset({"mix.exs"}),
              frozenset({"mix.lock"}),
              manifest_only_ok=False),
    Ecosystem("Pipenv", "Python",
              frozenset({"Pipfile"}),
              frozenset({"Pipfile.lock"}),
              manifest_only_ok=False),
    Ecosystem("Poetry/uv", "Python",
              frozenset({"pyproject.toml"}),
              frozenset({"poetry.lock", "uv.lock"}),
              manifest_only_ok=False),
    Ecosystem("setuptools", "Python",
              frozenset({"setup.py"}),
              frozenset(),
              manifest_only_ok=True),
    Ecosystem("NuGet", "C#/.NET",
              frozenset({"*.csproj"}),
              frozenset({"packages.lock.json"}),
              manifest_only_ok=True),
    Ecosystem("Maven", "Java",
              frozenset({"pom.xml"}),
              frozenset(),
              manifest_only_ok=True),
    Ecosystem("Gradle", "Java/Kotlin",
              frozenset({"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}),
              frozenset({"gradle.lockfile"}),
              manifest_only_ok=True),
    Ecosystem("SBT", "Scala",
              frozenset({"build.sbt"}),
              frozenset(),
              manifest_only_ok=True),
]

# Extension -> language label, used purely for the "code type" column.
EXTENSION_LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rb": "Ruby",
    ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".php": "PHP", ".dart": "Dart", ".swift": "Swift", ".ex": "Elixir",
    ".exs": "Elixir", ".cs": "C#", ".scala": "Scala", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".m": "Objective-C",
    ".sh": "Shell", ".pl": "Perl", ".ml": "OCaml", ".hs": "Haskell",
    ".lua": "Lua", ".r": "R", ".jl": "Julia", ".clj": "Clojure",
}

DEFAULT_EXCLUDE_DIRS = {".git"}


@dataclass
class DirReport:
    rel_path: str
    depth: int
    languages: Counter
    manifest_paths: list  # repo-relative paths, e.g. "frontend/package.json"
    lockfile_paths: list
    ecosystem_matches: list  # ecosystem names matched in this directory
    scannable: bool
    reason: str
    pruned_children: bool


def list_repo_files(repo: Path, include_untracked: bool) -> list:
    """Return repo-relative file paths via git, respecting .gitignore.

    Uses -z (NUL-terminated, unquoted) output so filenames with non-ASCII or
    special characters aren't C-style quoted by git (e.g. `"path/with/\\unicode\\"`),
    which would otherwise show up as a bogus extra directory entry.
    """
    cmd = ["git", "-C", str(repo), "ls-files", "-z"]
    if include_untracked:
        cmd += ["--others", "--exclude-standard"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        sys.exit("git executable not found on PATH.")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"git ls-files failed: {exc.stderr.strip()}")
    return [line for line in out.stdout.split("\0") if line]


def build_dir_tree(files: list) -> dict:
    """Map directory (as PurePosixPath, '' = repo root) -> list of filenames directly in it."""
    tree: dict = {}
    for f in files:
        p = PurePosixPath(f)
        parent = p.parent
        parent_str = "" if str(parent) == "." else str(parent)
        tree.setdefault(parent_str, []).append(p.name)
    return tree


def _full_path(dir_path: str, filename: str) -> str:
    return filename if dir_path in ("", ".") else f"{dir_path}/{filename}"


def match_ecosystems(dir_path: str, filenames: list) -> list:
    """For each ecosystem, find the actual manifest/lockfile file paths present.

    Returns a list of (Ecosystem, manifest_paths, lockfile_paths), where the
    path lists are full repo-relative paths (e.g. "frontend/package.json").
    """
    matches = []
    fname_set = set(filenames)
    for eco in ECOSYSTEMS:
        manifest_files = []
        for m in eco.manifests:
            if "*" in m:
                manifest_files += [f for f in filenames if fnmatch.fnmatch(f, m)]
            elif m in fname_set:
                manifest_files.append(m)
        lockfile_files = [l for l in eco.lockfiles if l in fname_set]
        if manifest_files or lockfile_files:
            manifest_paths = sorted(_full_path(dir_path, f) for f in set(manifest_files))
            lockfile_paths = sorted(_full_path(dir_path, f) for f in set(lockfile_files))
            matches.append((eco, manifest_paths, lockfile_paths))
    return matches


def detect_languages(filenames: list) -> Counter:
    counts: Counter = Counter()
    for f in filenames:
        ext = PurePosixPath(f).suffix.lower()
        lang = EXTENSION_LANGUAGES.get(ext)
        if lang:
            counts[lang] += 1
    return counts


def evaluate_scannability(matches: list) -> tuple:
    """Return (scannable: bool, reason: str) for a directory given its ecosystem matches."""
    if not matches:
        return False, "no recognized manifest or lockfile found"

    scannable_ecos = []
    unscannable_ecos = []
    for eco, manifest_paths, lockfile_paths in matches:
        if lockfile_paths:
            scannable_ecos.append(eco.name)
        elif manifest_paths and eco.manifest_only_ok:
            scannable_ecos.append(f"{eco.name} (manifest-only)")
        elif manifest_paths:
            unscannable_ecos.append(eco.name)

    if scannable_ecos:
        return True, "scannable via: " + ", ".join(scannable_ecos)
    return False, (
        "manifest found for " + ", ".join(unscannable_ecos)
        + " but no lockfile, and this ecosystem requires one for a scannable subproject"
    )


def has_full_pair(matches: list) -> bool:
    return any(manifest_paths and lockfile_paths for _, manifest_paths, lockfile_paths in matches)


def scan(repo: Path, max_depth: int, include_untracked: bool) -> list:
    files = list_repo_files(repo, include_untracked)
    tree = build_dir_tree(files)

    # Every directory that contains files, plus intermediate ancestor directories
    # (so an empty-of-files intermediate dir on the path to a deeper match still
    # gets visited/reported), up to max_depth.
    all_dirs = set(tree.keys())
    for d in list(all_dirs):
        parts = PurePosixPath(d).parts
        for i in range(len(parts)):
            all_dirs.add(str(PurePosixPath(*parts[: i + 1])))
    all_dirs.add("")  # repo root

    def depth_of(d: str) -> int:
        return 0 if d == "" else len(PurePosixPath(d).parts)

    pruned_prefixes = []  # directories whose subtree we stop reporting on

    def is_pruned(d: str) -> bool:
        for prefix in pruned_prefixes:
            if d == prefix:
                continue
            if prefix == "" or d.startswith(prefix + "/"):
                return True
        return False

    reports = []
    for d in sorted(all_dirs, key=depth_of):
        depth = depth_of(d)
        if depth > max_depth:
            continue
        if any(part in DEFAULT_EXCLUDE_DIRS for part in PurePosixPath(d).parts):
            continue
        if is_pruned(d):
            continue

        filenames = tree.get(d, [])
        matches = match_ecosystems(d, filenames)
        languages = detect_languages(filenames)
        scannable, reason = evaluate_scannability(matches)
        pruned_children = has_full_pair(matches)
        if pruned_children:
            pruned_prefixes.append(d)

        manifest_paths = sorted({p for _, mp, _ in matches for p in mp})
        lockfile_paths = sorted({p for _, _, lp in matches for p in lp})

        reports.append(DirReport(
            rel_path=d if d else ".",
            depth=depth,
            languages=languages,
            manifest_paths=manifest_paths,
            lockfile_paths=lockfile_paths,
            ecosystem_matches=[eco.name for eco, _, _ in matches],
            scannable=scannable,
            reason=reason,
            pruned_children=pruned_children,
        ))

    return reports


def format_table(reports: list) -> str:
    lines = []
    header = (
        f"{'Directory':<40} {'Depth':<6} {'Code type':<24} "
        f"{'Manifest path':<35} {'Lockfile path':<35} Supported by Semgrep?"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for r in reports:
        langs = ", ".join(f"{lang}({n})" for lang, n in r.languages.most_common()) or "-"
        manifest = ", ".join(r.manifest_paths) or "-"
        lockfile = ", ".join(r.lockfile_paths) or "-"
        supported = "Yes" if r.scannable else "No"
        pruned_note = "  [subtree pruned: full pair found]" if r.pruned_children else ""
        lines.append(
            f"{r.rel_path:<40} {r.depth:<6} {langs:<24} "
            f"{manifest:<35} {lockfile:<35} {supported}{pruned_note}"
        )
    return "\n".join(lines)


def to_json(reports: list) -> str:
    payload = [
        {
            "directory": r.rel_path,
            "depth": r.depth,
            "code_type_by_extension": dict(r.languages),
            "manifest_paths": r.manifest_paths,
            "lockfile_paths": r.lockfile_paths,
            "ecosystems_matched": r.ecosystem_matches,
            "supported_by_semgrep": r.scannable,
            "reason": r.reason,
            "subtree_pruned": r.pruned_children,
        }
        for r in reports
    ]
    return json.dumps(payload, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", nargs="?", default=".", help="Path to the git repository (default: current directory)")
    parser.add_argument("-n", "--depth", type=int, default=3, help="How many directory levels deep to scan (default: 3)")
    parser.add_argument("--include-untracked", action="store_true",
                         help="Also consider untracked files that aren't gitignored (default: tracked files only)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of a table")
    parser.add_argument("-o", "--output", help="Write the report to this file instead of stdout")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        sys.exit(f"{repo} does not look like a git repository (no .git directory).")

    reports = scan(repo, args.depth, args.include_untracked)

    if args.json:
        output = to_json(reports)
    else:
        total = len(reports)
        scannable = sum(1 for r in reports if r.scannable)
        output = (
            format_table(reports)
            + f"\n\n{total} directories analyzed, {scannable} currently supported by Semgrep supply chain."
        )

    if args.output:
        Path(args.output).write_text(output + "\n")
        print(f"Report written to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
