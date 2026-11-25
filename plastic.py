"""Unity Version Control (Plastic SCM) support for subset.

Plastic history lives on a Plastic server, not in the workspace folder, so a Plastic repo is turned into a git repo first and
then sampled exactly like any other repo (first 5% of the main branch, same manifest, same leak checks):

  * a repo on a server (``name@server:port``, ``name@org@cloud``): ``cm fast-export`` writes a git fast-import stream, which
    ``git fast-import`` loads into a fresh repo. Needs the ``cm`` client, already logged in to that server.
  * a fast-export file someone made with ``cm fast-export`` (``*.fe``): imported the same way; no ``cm`` needed.
  * a Plastic workspace folder (has ``.plastic/``): its repo is read from the workspace selector and exported as above.

Plastic's ``/main`` branch arrives as git ``master`` (or ``main``), which is what the sampler picks. Stdlib only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from repo_discovery import Target

PLATFORM = "plastic"
LABEL = "Unity Version Control (Plastic SCM)"
CM_TIMEOUT_SECONDS = 3600
LIST_TIMEOUT_SECONDS = 120
EXPORT_SUFFIXES = (".fe",)
# A fast-import stream starts with one of these commands (cm writes `blob` / `reset` / `commit` first).
STREAM_STARTS = (b"blob", b"reset", b"commit", b"feature", b"progress", b"tag", b"checkpoint", b"option", b"#")
SELECTOR_FILES = ("plastic.selector", "plastic.workspace")
SELECTOR_RE = re.compile(r'\b(?:repository|rep)\s+"([^"]+)"', re.I)
# name@server:port  |  name@org@cloud  |  name@server  (optionally prefixed rep: / repo:)
REPSPEC_RE = re.compile(r"^(?:rep(?:o)?:)?([^@\s/][^@\s]*)@([^\s]+)$", re.I)


class PlasticError(RuntimeError):
    """Readable reason a Plastic repo could not be listed or exported."""


def cm_path() -> str | None:
    return shutil.which(os.environ.get("PLASTIC_CM", "").strip() or "cm")


def parse_repspec(spec: str) -> Target:
    """``name@server:port`` / ``name@org@cloud`` -> Target named ``<server>/<name>``."""
    text = spec.strip()
    m = REPSPEC_RE.match(text)
    if not m:
        raise ValueError(f"not a Plastic repo spec (want name@server:port or name@org@cloud): {spec!r}")
    name, server = m.group(1), m.group(2)
    return Target(full_name=f"{server}/{name}", clone_url=f"{name}@{server}", platform=PLATFORM)


def _cm(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    cm = cm_path()
    if not cm:
        raise PlasticError("the Plastic SCM client `cm` is not installed (use the plastic Docker image, or install "
                           "Unity Version Control and log in); a fast-export .fe file needs no cm")
    try:
        proc = subprocess.run([cm, *args], capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise PlasticError(f"cm {args[0]} timed out after {timeout}s") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise PlasticError(f"cm {args[0]} failed ({proc.returncode}): {' '.join(detail[-3:])[:300]}")
    return proc


def list_repos(server: str) -> list[Target]:
    """Every repo on a Plastic server (``host:port``, ``org@cloud``) that the logged-in cm user can see."""
    server = server.strip()
    if not server:
        raise PlasticError("Enter the Plastic server, for example myserver:8087 or myorg@cloud")
    out = _cm(["repo", "list", server, "--format={repname}"], LIST_TIMEOUT_SECONDS).stdout
    found = {}
    for line in out.splitlines():
        name = line.strip()
        if not name or name.lower().startswith(("error", "warning")):
            continue
        name = name.split("@", 1)[0]  # some cm versions print name@server
        found[name] = Target(full_name=f"{server}/{name}", clone_url=f"{name}@{server}", platform=PLATFORM)
    return sorted(found.values(), key=lambda t: t.full_name.lower())


def workspace_repspec(folder: Path) -> str | None:
    """The repo a Plastic workspace folder is bound to, read from its ``.plastic`` selector (None if unknown)."""
    meta = folder / ".plastic"
    for name in SELECTOR_FILES:
        f = meta / name
        if f.is_file():
            m = SELECTOR_RE.search(f.read_text(encoding="utf-8", errors="ignore"))
            if m and REPSPEC_RE.match(m.group(1)):
                return m.group(1)
    return None


def is_export_file(path: Path) -> bool:
    if path.suffix.lower() not in EXPORT_SUFFIXES or not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            head = fh.read(64).lstrip()
    except OSError:
        return False
    return head.startswith(STREAM_STARTS)


def export_target(name: str, path: Path) -> Target:
    return Target(full_name=name, local_path=path, platform="plastic_export")


def _git(args: list[str], cwd: Path, *, stdin=None, timeout: int = CM_TIMEOUT_SECONDS) -> None:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", *args], cwd=str(cwd),
                          stdin=stdin, capture_output=True, timeout=timeout, env=env)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise PlasticError(f"git {args[0]} failed ({proc.returncode}): {err[-300:]}")


def import_stream(stream: Path, dest: Path) -> None:
    """Load a git fast-import stream into a new non-bare repo at dest, HEAD on Plastic's main branch."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _git(["init", "--quiet"], dest)
    with stream.open("rb") as fh:
        _git(["fast-import", "--quiet"], dest, stdin=fh)
    heads = subprocess.run(["git", "for-each-ref", "--format=%(refname)", "refs/heads"], cwd=str(dest),
                           capture_output=True, text=True).stdout.split()
    if not heads:
        raise PlasticError("the fast-export stream holds no branch (empty repo?)")
    main = next((h for h in ("refs/heads/main", "refs/heads/master") if h in heads), None)
    if main is None:
        raise PlasticError("no main branch in the export (Plastic /main should arrive as master or main); "
                           f"branches: {', '.join(h[len('refs/heads/'):] for h in heads[:5])}")
    _git(["symbolic-ref", "HEAD", main], dest)


def export_repo(repspec: str, dest: Path, work: Path, timeout: int = CM_TIMEOUT_SECONDS) -> None:
    """cm fast-export the whole repo, then import it as git at dest. The export file is deleted afterwards."""
    work.mkdir(parents=True, exist_ok=True)
    stream = work / (re.sub(r"[^A-Za-z0-9._-]+", "_", repspec) + ".fe")
    stream.unlink(missing_ok=True)
    try:
        _cm(["fast-export", repspec, str(stream)], timeout)
        if not stream.is_file() or stream.stat().st_size == 0:
            raise PlasticError("cm fast-export wrote nothing (empty repo, or no read permission)")
        import_stream(stream, dest)
    finally:
        stream.unlink(missing_ok=True)
