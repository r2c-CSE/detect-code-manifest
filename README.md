# detect-code-manifest

A Python script that walks a git repository N levels deep and reports, for
every directory it visits, what kind of code lives there and whether a
dependency manifest / lockfile pair was found — and whether that directory
would currently be scannable by Semgrep's supply chain (SCA) scanning.

## Why

Semgrep's supply chain scanner doesn't do a generic "what's in this
directory" inspection. It asks git for files matching a fixed list of known
manifest/lockfile filenames — one set per ecosystem it has a matcher for —
and only those matches ever become candidates. A directory with, say, a
`Cargo.toml` but no `Cargo.lock` produces no visible signal at all: no row,
no entry, nothing to distinguish it from an empty directory.

This tool fills that gap for exploration purposes: it reports on **every**
directory up to a given depth, whether or not anything was found there, so
you can see the difference between "genuinely nothing here," "an ecosystem
Semgrep doesn't know about," and "Semgrep knows this ecosystem but the
lockfile is missing so nothing gets scanned."

## Usage

```
python3 detect_code_manifest.py [repo] [-n DEPTH] [--include-untracked] [--json] [-o OUTPUT]
```

| Argument | Default | Description |
|---|---|---|
| `repo` | `.` | Path to the git repository to scan |
| `-n`, `--depth` | `3` | How many directory levels deep to scan, counting the repo root as depth 0 |
| `--include-untracked` | off | Also consider untracked files that aren't gitignored (default is tracked files only) |
| `--json` | off | Output JSON instead of a table |
| `-o`, `--output` | stdout | Write the report to this file instead of printing it |

### Example

```
$ python3 detect_code_manifest.py ~/code/some-repo -n 3

Directory      Depth  Code type   Manifest path             Lockfile path                Supported by Semgrep?
--------------------------------------------------------------------------------------------------------------
.               0      -           -                         -                            No
backend         1      -           backend/go.mod            -                            No
frontend        1      -           frontend/package.json     frontend/package-lock.json    Yes  [subtree pruned: full pair found]
services/java   2      -           services/java/pom.xml     -                            Yes

4 directories analyzed, 2 currently supported by Semgrep supply chain.
```

## How it works

1. **File discovery** — runs `git ls-files` (tracked files; add
   `--include-untracked` to also include untracked-but-not-gitignored files)
   instead of walking the raw filesystem. This means `.gitignore`d
   directories like `node_modules/`, `vendor/`, or `dist/` are skipped
   automatically, the same way they'd be invisible to a real repo scan.
2. **Directory tree** — files are grouped by their containing directory, and
   every ancestor directory up to `--depth` is included in the report even
   if it has no files of its own directly in it, so the tree stays
   contiguous.
3. **Language detection** — for each directory, filenames are matched
   against a table of file extensions (`.py`, `.go`, `.rs`, `.java`, etc.) to
   give a rough "code type" signal, independent of manifest detection.
4. **Ecosystem / manifest detection** — for each directory, filenames are
   checked against a fixed table of manifest and lockfile names per
   ecosystem (see below); matches are reported as repo-relative file paths
   (e.g. `frontend/package.json`), not just the ecosystem name.
5. **Scannability** — a directory is reported as currently supported
   ("Supported by Semgrep?" = Yes/No) if:
   - a lockfile is present for any matched ecosystem, or
   - only a manifest is present, but that ecosystem allows manifest-only
     subprojects.

   Everything else (manifest present but no lockfile, for an ecosystem that
   requires one; or nothing recognized at all) is reported as not scannable,
   with a reason.
6. **Pruning** — once a directory has a full manifest+lockfile pair for some
   ecosystem, its subdirectories are not visited or reported. A manifest-only
   match (even a scannable one, like a bare `pom.xml`) does *not* prune,
   since nested modules may still exist underneath.

## Ecosystems recognized

| Ecosystem | Language | Manifest | Lockfile | Manifest-only scannable? |
|---|---|---|---|---|
| npm/yarn/pnpm/bun | JavaScript/TypeScript | `package.json` | `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `bun.lock(b)` | No |
| RubyGems | Ruby | `Gemfile` | `Gemfile.lock` | No |
| Go modules | Go | `go.mod` | `go.sum` | No |
| Cargo | Rust | `Cargo.toml` | `Cargo.lock` | No |
| Composer | PHP | `composer.json` | `composer.lock` | No |
| Pub | Dart | `pubspec.yaml` | `pubspec.lock` | No |
| SwiftPM | Swift | `Package.swift` | `Package.resolved` | No |
| Hex | Elixir | `mix.exs` | `mix.lock` | No |
| Pipenv | Python | `Pipfile` | `Pipfile.lock` | No |
| Poetry/uv | Python | `pyproject.toml` | `poetry.lock`, `uv.lock` | No |
| setuptools | Python | `setup.py` | — | Yes |
| NuGet | C#/.NET | `*.csproj` | `packages.lock.json` | Yes |
| Maven | Java | `pom.xml` | — | Yes |
| Gradle | Java/Kotlin | `build.gradle(.kts)`, `settings.gradle(.kts)` | `gradle.lockfile` | Yes |
| SBT | Scala | `build.sbt` | — | Yes |

## Output

With `--json`, each directory is reported as an object:

```json
{
  "directory": "frontend",
  "depth": 1,
  "code_type_by_extension": {"JavaScript": 4},
  "manifest_paths": ["frontend/package.json"],
  "lockfile_paths": ["frontend/package-lock.json"],
  "ecosystems_matched": ["npm/yarn/pnpm/bun"],
  "supported_by_semgrep": true,
  "reason": "scannable via: npm/yarn/pnpm/bun",
  "subtree_pruned": true
}
```

## Limitations

- This is a heuristic exploration tool, not a copy of Semgrep's internal
  matcher logic. It doesn't model every nuance (e.g. which specific lockfile
  *kinds* have a parser wired up once a subproject is created) — it only
  distinguishes "would produce a scannable subproject at all" from "would
  not," based on the ecosystems table above.
- `pyproject.toml` is treated as one generic Python manifest paired with
  either `poetry.lock` or `uv.lock`; it doesn't distinguish Poetry- from
  uv-managed projects.
