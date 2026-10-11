"""First-N% commit-history sampling engine for subset.

Cutoff maths: keep the first ceil(total * pct / 100) commits of the default branch, drop
every other ref and tag, then gc. Stdlib-only so subset ships standalone.

Every repo that is *not* sampled gets a line in samples/sampling_log.jsonl and a
WARNING in the run log carrying the reason.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

SAMPLE_ENV = "SAMPLE"
MIN_COMMITS_ENV = "SAMPLE_MIN_COMMITS"
REPOS_ENV = "SAMPLE_REPOS"
DEFAULT_MIN_COMMITS = 20
GIT_TIMEOUT_SECONDS = 900
SAMPLES_DIRNAME = "samples"
LOG_NAME = "sampling_log.jsonl"
REPORT_NAME = "sampling_report.json"

GIT_HARDENING_ARGS = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor="]
FALLBACK_BRANCHES = ("main", "master", "trunk")
# Config an upstream clone leaves behind that must not ship (URLs can carry credentials).
STRIPPED_CONFIG_PREFIXES = ("remote.", "branch.", "submodule.", "credential.", "url.", "http.")
STRIPPED_CONFIG_KEYS = ("core.fsmonitor", "core.hookspath", "core.sshcommand", "core.gitproxy")

_log_lock = threading.Lock()


class SampleConfigError(ValueError):
    """SAMPLE (or SAMPLE_MIN_COMMITS) holds a value we refuse to guess about."""


class SampleSkipped(RuntimeError):
    """Repo cannot be sampled; str(exc) is the reason that gets logged."""


def parse_sample_pct(raw: str | None) -> float:
    """0 means off. Anything else must be a number in 1..100."""
    text = (raw or "").strip()
    if not text:
        return 0.0
    try:
        value = float(text)
    except ValueError:
        raise SampleConfigError(
            f"{SAMPLE_ENV}={text!r} is not a number; use 0 (off) or 1-100"
        ) from None
    if value == 0:
        return 0.0
    if not (1 <= value <= 100) or math.isnan(value):
        raise SampleConfigError(
            f"{SAMPLE_ENV}={text!r} is out of range; use 0 (off) or 1-100"
        )
    return value


def sample_pct_from_env() -> float:
    return parse_sample_pct(os.environ.get(SAMPLE_ENV))


def min_commits_from_env() -> int:
    text = os.environ.get(MIN_COMMITS_ENV, "").strip()
    if not text:
        return DEFAULT_MIN_COMMITS
    try:
        value = int(text)
    except ValueError:
        raise SampleConfigError(f"{MIN_COMMITS_ENV}={text!r} is not an integer") from None
    if value < 1:
        raise SampleConfigError(f"{MIN_COMMITS_ENV} must be >= 1")
    return value


def parse_repo_filter(raw: str | None) -> frozenset[str]:
    """Comma/newline separated repo selectors, lower-cased. Empty = sample all."""
    return frozenset(
        part.strip().strip("/").lower()
        for part in re.split(r"[,\n]", raw or "")
        if part.strip()
    )


def repo_filter_from_env() -> frozenset[str]:
    return parse_repo_filter(os.environ.get(REPOS_ENV))


def selector_matches(selector: str, full_name: str) -> bool:
    """A selector is a full `org/repo` name or a bare repo name."""
    name = full_name.strip("/").lower()
    return selector == name or selector == name.rsplit("/", 1)[-1]


def is_selected(repo_filter: frozenset[str], full_name: str) -> bool:
    return not repo_filter or any(selector_matches(sel, full_name) for sel in repo_filter)


def unmatched_selectors(repo_filter: frozenset[str], full_names: list[str]) -> list[str]:
    return sorted(
        sel for sel in repo_filter
        if not any(selector_matches(sel, name) for name in full_names)
    )


def load_dotenv_file(path: Path) -> None:
    """Minimal .env loader. Values already in the environment win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _git(args: list[str], cwd: Path, check: bool = True,
         timeout: int = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    # The repo under sampling is untrusted: its config may point core.fsmonitor or
    # core.hooksPath at an executable, which git would otherwise run on our behalf.
    env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_CONFIG_NOSYSTEM": "1"}
    proc = subprocess.run(
        ["git", *GIT_HARDENING_ARGS, *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout, env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    return proc


def _is_promisor_clone(repo: Path) -> bool:
    cfg = repo / ".git" / "config"
    if not cfg.is_file():
        return False
    return "promisor = true" in cfg.read_text(encoding="utf-8", errors="ignore")


def _ref_exists(repo: Path, ref: str) -> bool:
    return _git(["show-ref", "--verify", "--quiet", ref], repo, check=False).returncode == 0


def _default_branch(repo: Path) -> tuple[str, str, str]:
    """(short name, full ref, how it was chosen).

    A local repo often has a feature branch checked out, so HEAD is the last resort:
    origin/HEAD, then main/master/trunk, then whatever HEAD is.
    """
    origin_head = _git(
        ["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], repo, check=False
    ).stdout.strip()
    if origin_head.startswith("refs/remotes/origin/") and _ref_exists(repo, origin_head):
        name = origin_head[len("refs/remotes/origin/"):]
        local = f"refs/heads/{name}"
        return name, local if _ref_exists(repo, local) else origin_head, "origin/HEAD"
    for name in FALLBACK_BRANCHES:
        local = f"refs/heads/{name}"
        if _ref_exists(repo, local):
            return name, local, f"conventional name {name!r}"
    head = _git(["symbolic-ref", "-q", "HEAD"], repo, check=False).stdout.strip()
    if head.startswith("refs/heads/") and _ref_exists(repo, head):
        return head[len("refs/heads/"):], head, "checked-out HEAD"
    raise SampleSkipped("no branch to sample (detached HEAD, no origin/HEAD, no main/master/trunk)")


def _cutoff(repo: Path, ref: str, pct: float, min_commits: int) -> tuple[str, int, int]:
    total = int(_git(["rev-list", "--count", ref, "--"], repo).stdout.strip())
    if total < min_commits:
        raise SampleSkipped(
            f"only {total} commits on {ref!r}, below the minimum of {min_commits}"
        )
    keep = min(total, max(1, math.ceil(total * pct / 100)))
    # Topological, not date order: commit dates can be skewed or merged out of order,
    # and only a topo prefix is closed under "parent of", so the ancestors of
    # shas[keep-1] can never exceed `keep`.
    shas = _git(["rev-list", "--topo-order", "--reverse", ref, "--"], repo).stdout.splitlines()
    return shas[keep - 1], total, keep


def _commit_objects_on_disk(repo: Path) -> int:
    out = _git(["cat-file", "--batch-all-objects", "--batch-check=%(objecttype)"], repo).stdout
    return sum(1 for line in out.splitlines() if line == "commit")


def _strip_config(work: Path) -> None:
    names = _git(["config", "--local", "--list", "--name-only"], work, check=False).stdout.splitlines()
    for name in dict.fromkeys(names):
        low = name.lower()
        if low.startswith(STRIPPED_CONFIG_PREFIXES) or low in STRIPPED_CONFIG_KEYS:
            _git(["config", "--local", "--unset-all", name], work, check=False)


def _truncate(work: Path, cutoff_sha: str, sample_branch: str, keep: int) -> int:
    """Cut history at cutoff_sha, leaving nothing past it reachable or on disk.

    Returns the commit count in the sample.
    """
    git_dir = work / ".git"
    _git(["reset", "--hard", "--quiet"], work)
    _git(["clean", "-fdx", "--quiet"], work)
    _git(["checkout", "--quiet", "--force", cutoff_sha], work)
    _git(["checkout", "--quiet", "-B", sample_branch], work)

    # refs/replace could graft history back in; the rest (stash, notes, pull/*,
    # original/*, remotes, tags) can each point past the cutoff.
    keep_ref = f"refs/heads/{sample_branch}"
    for ref in _git(["for-each-ref", "--format=%(refname)"], work).stdout.split():
        if ref != keep_ref:
            _git(["update-ref", "--no-deref", "-d", ref], work, check=False)

    # Alternates would leave the sample dependent on (and borrowing from) the source.
    if (git_dir / "objects" / "info" / "alternates").exists():
        _git(["repack", "-a", "-d", "-q"], work)
        (git_dir / "objects" / "info" / "alternates").unlink()

    _strip_config(work)
    for stale in ("FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "shallow"):
        (git_dir / stale).unlink(missing_ok=True)
    for stale_dir in ("modules", "lfs", "worktrees", "logs", "hooks"):
        shutil.rmtree(git_dir / stale_dir, ignore_errors=True)
    (git_dir / "info" / "grafts").unlink(missing_ok=True)

    _git(["reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all"], work)
    _git(["gc", "--prune=now", "--quiet"], work)

    reachable = int(_git(["rev-list", "--count", "--all"], work).stdout.strip())
    head_only = int(_git(["rev-list", "--count", "HEAD"], work).stdout.strip())
    on_disk = _commit_objects_on_disk(work)
    if reachable != head_only or on_disk != reachable or reachable > keep:
        raise RuntimeError(
            f"truncation leaked history: {reachable} reachable (HEAD {head_only}), "
            f"{on_disk} commit objects on disk, limit {keep}"
        )
    return reachable


def append_log(run_dir: Path, entry: dict) -> None:
    samples_dir = run_dir / SAMPLES_DIRNAME
    samples_dir.mkdir(parents=True, exist_ok=True)
    entry = {"time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    with _log_lock, (samples_dir / LOG_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def take_sample(
    *,
    repo_name: str,
    slug: str,
    source: Path,
    disposable: bool,
    run_dir: Path,
    pct: float,
    min_commits: int,
    log: logging.Logger,
) -> bool:
    """Write run_dir/samples/<slug>/ holding the first pct% of history.

    `disposable` means source is a throwaway clone: it is moved into place
    rather than copied, which avoids doubling disk use on large repos. A local
    (user-owned) repo is always copied and never modified.

    Returns True when a sample was produced. Every other outcome is logged with
    its reason and returns False; this function never raises.
    """
    dest = run_dir / SAMPLES_DIRNAME / slug
    staging = run_dir / SAMPLES_DIRNAME / f".staging-{slug}"
    try:
        if not (source / ".git").exists():
            raise SampleSkipped(f"not a non-bare git working tree: {source.name}")
        if _is_promisor_clone(source):
            raise SampleSkipped(
                "source is a partial (blobless) clone; historical blobs are missing "
                "so the sample would be incomplete"
            )
        if _git(["rev-parse", "--is-shallow-repository"], source).stdout.strip() == "true":
            raise SampleSkipped(
                "source is a shallow clone; its commit count and cutoff would be wrong "
                "(run `git fetch --unshallow` first)"
            )
        branch, branch_ref, branch_how = _default_branch(source)
        log.info("SAMPLE %s: using branch %s (%s)", repo_name, branch, branch_how)
        cutoff_sha, total, keep = _cutoff(source, branch_ref, pct, min_commits)

        for stale in (dest, staging):
            if stale.exists():
                shutil.rmtree(stale)
        staging.parent.mkdir(parents=True, exist_ok=True)
        if disposable:
            shutil.move(str(source), str(staging))
        else:
            shutil.copytree(source, staging, symlinks=True)

        sample_branch = f"sample{pct:g}pct".replace(".", "_")
        reachable = _truncate(staging, cutoff_sha, sample_branch, keep)
        fsck = _git(["fsck", "--full"], staging, check=False)
        fsck_clean = fsck.returncode == 0 and not fsck.stdout.strip()
        if not fsck_clean:
            raise SampleSkipped(f"sample failed git fsck: {fsck.stdout.strip() or fsck.stderr.strip()[:300]}")

        manifest = {
            "repo": repo_name,
            "branch_sampled": branch,
            "branch_chosen_by": branch_how,
            "sample_branch_name": sample_branch,
            "requested_pct": pct,
            "total_commits": total,
            "cutoff_commit_index": keep,
            "cutoff_commit_sha": cutoff_sha,
            "reachable_commits_in_sample": reachable,
            "fsck_clean": fsck_clean,
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (staging / "SAMPLE_MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        staging.rename(dest)

        log.info("SAMPLED %s: %s of %s commits on %s", repo_name, reachable, total, branch)
        append_log(run_dir, {
            "repo": repo_name, "slug": slug, "status": "sampled",
            "total_commits": total, "sampled_commits": reachable,
            "fsck_clean": fsck_clean,
        })
        return True
    except SampleSkipped as exc:
        reason = str(exc)
    except subprocess.TimeoutExpired as exc:
        reason = f"git timed out: {exc.cmd[:3]}"
    except (RuntimeError, OSError) as exc:
        reason = f"sampling error: {exc}"

    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(dest, ignore_errors=True)
    log.warning("SAMPLE SKIPPED %s: %s", repo_name, reason)
    append_log(run_dir, {"repo": repo_name, "slug": slug, "status": "skipped", "reason": reason})
    return False


def log_skip(run_dir: Path, repo_name: str, slug: str, reason: str, log: logging.Logger) -> None:
    """Record a repo that never reached the sampler (e.g. clone/metrics failed)."""
    log.warning("SAMPLE SKIPPED %s: %s", repo_name, reason)
    append_log(run_dir, {"repo": repo_name, "slug": slug, "status": "skipped", "reason": reason})


def logged_slugs(run_dir: Path) -> set[str]:
    path = run_dir / SAMPLES_DIRNAME / LOG_NAME
    if not path.is_file():
        return set()
    return {
        json.loads(line)["slug"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def write_report(run_dir: Path, pct: float, min_commits: int,
                 repo_filter: frozenset[str] = frozenset(),
                 unmatched: list[str] | None = None) -> dict:
    """Collapse sampling_log.jsonl (latest entry per repo wins) into a report."""
    samples_dir = run_dir / SAMPLES_DIRNAME
    latest: dict[str, dict] = {}
    log_path = samples_dir / LOG_NAME
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                latest[entry["slug"]] = entry
    sampled = [e for e in latest.values() if e["status"] == "sampled"]
    skipped = [e for e in latest.values() if e["status"] == "skipped"]
    report = {
        "sample_pct": pct,
        "min_commits": min_commits,
        "selected_repos": sorted(repo_filter) or "all",
        "selectors_matching_no_repo": unmatched or [],
        "sampled_count": len(sampled),
        "skipped_count": len(skipped),
        "sampled": sorted(sampled, key=lambda e: e["repo"]),
        "skipped": sorted(skipped, key=lambda e: e["repo"]),
    }
    samples_dir.mkdir(parents=True, exist_ok=True)
    (samples_dir / REPORT_NAME).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
