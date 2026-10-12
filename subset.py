#!/usr/bin/env python3
"""subset: sample the first N% of commit history of many repos. Sampling only, no metrics.

Sources:
  --local-repos-dir DIR   every git repo found under DIR (copied, never modified)
  --remote SPEC           owner/repo, or a github/gitlab/bitbucket URL (cloned in full, then sliced)

Usage:
  python subset.py --local-repos-dir ./repos
  python subset.py --local-repos-dir ./repos --repo alpha --repo org/beta
  python subset.py --remote acme/widgets --remote https://gitlab.com/grp/proj
  python subset.py --discover bitbucket-dc --host https://bitbucket.example.com --list-only
  python subset.py --discover github --repo alpha   # token from env/tokens file
  python subset.py --ui                       # browser UI on http://127.0.0.1:8768

Output: outputs/subset-<stamp>/ with samples/<org__repo>/, samples/sampling_log.jsonl,
samples/sampling_report.json, logs/subset.log, plus outputs/subset-<stamp>.zip.
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import repo_discovery
import sampler
from repo_discovery import Target

HERE = Path(__file__).resolve().parent
sampler.load_dotenv_file(HERE / ".env")

# Fixed on purpose: every sample keeps the first 5% of commits, and repos with fewer
# than 20 commits are skipped (logged). Not configurable from .env, the CLI or the UI.
SAMPLE_PCT = 5.0
MIN_COMMITS = 20
DEFAULT_WORKERS = 4

DEFAULT_OUTPUT = Path(os.environ.get("SUBSET_OUTPUT_DIR", "").strip() or HERE / "outputs")
CLONE_TIMEOUT_SECONDS = 1800
MAX_DISCOVERY_DEPTH = 3
CRED_RE = re.compile(r"(https?://)[^/@\s]+:[^/@\s]+@", re.I)
HOSTS = {"github": "github.com", "gitlab": "gitlab.com", "bitbucket": "bitbucket.org"}
TOKEN_USER = {"github": "x-access-token", "gitlab": "oauth2", "bitbucket": "x-token-auth"}
# Bitbucket Data Center takes a Bearer header (HTTP access token), not a URL login.
DC_KEY_RE = re.compile(r"data_?center|(^|[_-])(dc|server)([_-]|$)")


# ── discovery / parsing ─────────────────────────────────────────────────────

def discover_local_repos(root: Path) -> list[Target]:
    """Git working trees under root (depth <= 3). Does not descend into a repo."""
    root = root.resolve()
    found: list[Target] = []

    def walk(directory: Path, depth: int) -> None:
        if (directory / ".git").exists():
            rel = directory.relative_to(root).as_posix()
            found.append(Target(full_name=rel if rel != "." else directory.name, local_path=directory))
            return
        if depth >= MAX_DISCOVERY_DEPTH:
            return
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return
        for child in children:
            walk(child, depth + 1)

    walk(root, 0)
    return found


def resolve_local_selectors(root: Path, selectors: list[str]) -> list[Target]:
    """Exact local repos for --repo selectors.

    A selector naming a direct child of `root` that is a git tree wins outright, so
    it works even when `root` is itself a repo (discovery would stop there and
    return only root). Otherwise it must match exactly one discovered repo;
    a bare name matching several is an error, never a silent pick.
    """
    root = root.resolve()
    found: dict[str, Target] = {}
    discovered: list[Target] | None = None
    for raw in selectors:
        sel = raw.strip().strip("/")
        if not sel:
            continue
        direct = root / sel
        if (direct / ".git").exists() and direct.resolve().parent == root.joinpath(*Path(sel).parts[:-1]).resolve():
            found.setdefault(sel, Target(full_name=sel, local_path=direct))
            continue
        if discovered is None:
            discovered = discover_local_repos(root)
        hits = [t for t in discovered if sampler.selector_matches(sel.lower(), t.full_name)]
        if len(hits) > 1:
            raise ValueError(
                f"--repo {raw!r} is ambiguous under {root}: "
                + ", ".join(t.full_name for t in hits) + "; use org/repo or --repo-path"
            )
        if hits:
            found.setdefault(hits[0].full_name, hits[0])
    return list(found.values())


def parse_remote(spec: str, default_platform: str = "github", gitlab_host: str = "gitlab.com") -> Target:
    text = re.sub(r"\s*\[[^\]]*\]\s*$", "", spec.strip())
    if not text:
        raise ValueError("empty repo entry")
    scp = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$", text)
    if scp:
        host, path = scp.group(1), scp.group(2)
    elif re.match(r"^https?://", text):
        parts = urlsplit(text)
        host, path = parts.hostname or "", parts.path.strip("/").removesuffix(".git")
    elif re.match(r"^[\w.-]+/[\w./-]+$", text):
        host = gitlab_host if default_platform == "gitlab" else HOSTS[default_platform]
        path = text.removesuffix(".git")
    else:
        raise ValueError(f"cannot parse repo entry: {spec!r}")
    if not path or "/" not in path:
        raise ValueError(f"repo entry needs owner/repo: {spec!r}")
    platform = next((k for k, v in HOSTS.items() if v == host), None)
    if platform is None:
        platform = "gitlab"  # self-hosted: only GitLab hosts are configurable in the UI
    return Target(full_name=path, clone_url=f"https://{host}/{path}.git", platform=platform)


def parse_tokens_file(path: Path | None) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for env_key, slot in (("GH_TOKEN", "github"), ("GITHUB_TOKEN", "github"),
                          ("GITLAB_TOKEN", "gitlab"), ("BITBUCKET_TOKEN", "bitbucket"),
                          ("BITBUCKET_DC_TOKEN", "bitbucket_dc")):
        if os.environ.get(env_key) and slot not in tokens:
            tokens[slot] = os.environ[env_key].strip()
    if path and path.is_file():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip().lower(), value.strip().strip("\"'")
            if not value:
                continue
            if "bitbucket" in key and DC_KEY_RE.search(key):
                tokens.setdefault("bitbucket_dc", value)
                continue
            for slot in HOSTS:
                if slot in key:
                    tokens.setdefault(slot, value)
    return tokens


def redact(text: str, tokens: dict[str, str]) -> str:
    for token in tokens.values():
        if token:
            text = text.replace(token, "***").replace(quote(token, safe=""), "***")
    return CRED_RE.sub(r"\1***:***@", text)


def dc_url_from_env() -> str:
    return os.environ.get("BITBUCKET_DC_URL", "").strip()


def authed_url(target: Target, tokens: dict[str, str]) -> str:
    token = tokens.get(target.platform)
    url = target.clone_url or ""
    if target.platform == "bitbucket_dc" or not token or not url.startswith("https://"):
        return url
    return f"https://{TOKEN_USER[target.platform]}:{quote(token, safe='')}@{url[len('https://'):]}"


def git_config_env(env: dict[str, str], pairs: list[tuple[str, str]]) -> None:
    """Add `git -c` settings via GIT_CONFIG_*, so tokens stay out of argv."""
    count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    for key, value in pairs:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)


def clone_full(target: Target, dest: Path, tokens: dict[str, str], timeout: int,
               insecure_tls: bool = False) -> None:
    env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"}
    extra: list[tuple[str, str]] = []
    if target.platform == "bitbucket_dc" and tokens.get("bitbucket_dc"):
        extra.append(("http.extraHeader", f"Authorization: Bearer {tokens['bitbucket_dc']}"))
    if insecure_tls:
        extra.append(("http.sslVerify", "false"))
    git_config_env(env, extra)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["git", "clone", "--quiet", "--recurse-submodules=0", authed_url(target, tokens), str(dest)],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"clone timed out after {timeout}s") from None
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        detail = redact((proc.stderr or proc.stdout or "clone failed").strip(), tokens)
        raise RuntimeError(f"clone failed: {detail[-300:]}")


# ── job ─────────────────────────────────────────────────────────────────────

class RecentLog(logging.Handler):
    def __init__(self, keep: int = 200):
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=keep)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.levelname} {record.getMessage()}")


def make_logger(run_dir: Path, name: str) -> tuple[logging.Logger, RecentLog]:
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    for old in list(log.handlers):
        old.close()
        log.removeHandler(old)
    log.propagate = False
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(run_dir / "logs" / "subset.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    recent = RecentLog()
    for handler in (file_handler, stream, recent):
        log.addHandler(handler)
    return log, recent


def zip_run_dir(run_dir: Path) -> Path:
    zip_path = run_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    base = run_dir.parent
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(run_dir, followlinks=False):
            rel_root = Path(root).relative_to(run_dir)
            if rel_root.parts[:1] == ("clones",):
                dirs[:] = []
                continue
            if rel_root == Path("."):
                dirs[:] = [d for d in dirs if d != "clones"]
            # Empty dirs matter: git refuses a repo without .git/refs/{heads,tags}.
            if not dirs and not files:
                zf.write(root, Path(root).relative_to(base).as_posix())
            for name in files + [d for d in dirs if (Path(root) / d).is_symlink()]:
                path = Path(root) / name
                arc = path.relative_to(base).as_posix()
                if path.is_symlink():
                    # Store the link itself; ZipFile.write would embed the target's
                    # contents, which for a tracked `link -> /etc/passwd` is a host file.
                    info = zipfile.ZipInfo(arc)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    zf.writestr(info, os.readlink(path))
                else:
                    zf.write(path, arc)
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
    return zip_path


def run_job(
    targets: list[Target],
    *,
    pct: float = SAMPLE_PCT,
    min_commits: int = MIN_COMMITS,
    workers: int = DEFAULT_WORKERS,
    output_dir: Path,
    tokens: dict[str, str] | None = None,
    repo_filter: frozenset[str] = frozenset(),
    clone_timeout: int = CLONE_TIMEOUT_SECONDS,
    insecure_tls: bool = False,
    progress: dict | None = None,
    logger_name: str = "subset",
) -> dict:
    """Sample every target; returns {run_dir, zip, report}. Never raises per-repo."""
    tokens = tokens or {}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_dir / f"subset-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log, recent = make_logger(run_dir, logger_name)
    progress = progress if progress is not None else {}
    lock = threading.Lock()
    progress.update(run_dir=str(run_dir), total=len(targets), done=0, sampled=0, skipped=0,
                    current=[], recent=recent.lines)
    log.info("subset: %s repo(s), keep first %g%% of commits, min %s commits, %s worker(s)",
             len(targets), pct, min_commits, workers)

    def work(target: Target) -> None:
        with lock:
            progress["current"].append(target.full_name)
        ok = False
        try:
            if not sampler.is_selected(repo_filter, target.full_name):
                sampler.log_skip(run_dir, target.full_name, target.slug,
                                 f"not in the selected sample list ({sampler.REPOS_ENV})", log)
            elif target.local_path is not None:
                ok = sampler.take_sample(
                    repo_name=target.full_name, slug=target.slug, source=target.local_path,
                    disposable=False, run_dir=run_dir, pct=pct, min_commits=min_commits, log=log)
            else:
                clone = run_dir / "clones" / target.slug
                try:
                    clone_full(target, clone, tokens, clone_timeout, insecure_tls)
                except RuntimeError as exc:
                    sampler.log_skip(run_dir, target.full_name, target.slug, str(exc), log)
                else:
                    ok = sampler.take_sample(
                        repo_name=target.full_name, slug=target.slug, source=clone,
                        disposable=True, run_dir=run_dir, pct=pct, min_commits=min_commits, log=log)
                    shutil.rmtree(clone, ignore_errors=True)
        except Exception as exc:  # one bad repo must not stop the batch
            sampler.log_skip(run_dir, target.full_name, target.slug,
                             redact(f"unexpected error: {exc}", tokens), log)
        finally:
            with lock:
                progress["current"].remove(target.full_name)
                progress["done"] += 1
                progress["sampled" if ok else "skipped"] += 1

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(work, targets))

    unmatched = sampler.unmatched_selectors(repo_filter, [t.full_name for t in targets])
    if unmatched:
        log.warning("selected names matching no repo: %s", ", ".join(unmatched))
    report = sampler.write_report(run_dir, pct, min_commits, repo_filter, unmatched)
    shutil.rmtree(run_dir / "clones", ignore_errors=True)
    log.info("done: %s sampled, %s skipped", report["sampled_count"], report["skipped_count"])
    zip_path = zip_run_dir(run_dir)
    log.info("zip: %s", zip_path)
    for handler in list(log.handlers):
        handler.close()
        log.removeHandler(handler)
    progress.update(finished=True, zip=str(zip_path), report=report)
    return {"run_dir": run_dir, "zip": zip_path, "report": report}


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ui", action="store_true", help="start the browser UI")
    ap.add_argument("--ui-host", default="127.0.0.1")
    ap.add_argument("--ui-port", type=int, default=8768)
    ap.add_argument("--local-repos-dir", type=Path, help="folder holding git clones (searched 3 levels deep)")
    ap.add_argument("--repo-path", action="append", type=Path, default=[],
                    help="exact path of one git working tree to sample (repeatable); "
                         "unambiguous alternative to --local-repos-dir + --repo")
    ap.add_argument("--remote", action="append", default=[], help="owner/repo or repo URL (repeatable)")
    ap.add_argument("--remote-file", type=Path, help="text file with one --remote entry per line")
    ap.add_argument("--default-platform", choices=["github", "gitlab", "bitbucket"], default="github")
    ap.add_argument("--gitlab-host", default="gitlab.com")
    ap.add_argument("--discover", choices=[p.replace("_", "-") for p in repo_discovery.PLATFORMS],
                    help="find every repo the token can see on this platform and sample them "
                         "(use --repo to narrow)")
    ap.add_argument("--host", default="",
                    help="--discover: Bitbucket Data Center base URL (or BITBUCKET_DC_URL) / GitLab host")
    ap.add_argument("--workspace", default="", help="--discover bitbucket: workspace (default: repos you are a member of)")
    ap.add_argument("--insecure-tls", action="store_true",
                    help="skip TLS certificate checks for discovery and clones (self-signed servers only)")
    ap.add_argument("--list-only", action="store_true", help="--discover: print the repos found and exit")
    ap.add_argument("--tokens-file", type=Path, default=HERE / "tokens")
    ap.add_argument("--repo", action="append", default=[],
                    help="only sample this repo (org/repo or bare name; repeatable). Default: all found")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="repos processed in parallel")
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args(argv)

    if args.ui:
        import subset_ui
        subset_ui.serve(args.ui_host, args.ui_port)
        return 0

    targets: list[Target] = []
    tokens = parse_tokens_file(args.tokens_file)
    if args.discover:
        platform = args.discover.replace("-", "_")
        host = args.host or (dc_url_from_env() if platform == "bitbucket_dc" else "")
        try:
            found = repo_discovery.discover_remote(
                platform, tokens.get(platform, ""), host=host, workspace=args.workspace,
                insecure=args.insecure_tls)
        except repo_discovery.DiscoveryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"found {len(found)} repo(s) on {repo_discovery.PLATFORM_LABELS[platform]}", flush=True)
        if args.list_only:
            print("\n".join(t.full_name for t in found))
            return 0
        targets += found
    if args.local_repos_dir:
        if not args.local_repos_dir.is_dir():
            print(f"error: not a directory: {args.local_repos_dir}", file=sys.stderr)
            return 2
        if args.repo:
            try:
                local = resolve_local_selectors(args.local_repos_dir, args.repo)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        else:
            local = discover_local_repos(args.local_repos_dir)
        targets += local
    for path in args.repo_path:
        if not (path / ".git").exists():
            print(f"error: not a git working tree: {path}", file=sys.stderr)
            return 2
        targets.append(Target(full_name=path.resolve().name, local_path=path.resolve()))
    entries = list(args.remote)
    if args.remote_file:
        entries += [ln for ln in args.remote_file.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    for entry in entries:
        try:
            targets.append(parse_remote(entry, args.default_platform, args.gitlab_host))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if not targets:
        print("error: no repos found; give --local-repos-dir, --repo-path, --remote or --discover", file=sys.stderr)
        return 2

    result = run_job(
        targets, workers=args.workers, output_dir=args.output_dir,
        tokens=tokens, insecure_tls=args.insecure_tls,
        repo_filter=sampler.parse_repo_filter(",".join(args.repo)) or sampler.repo_filter_from_env(),
    )
    print(f"zip: {result['zip']}")
    return 0 if result["report"]["sampled_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
