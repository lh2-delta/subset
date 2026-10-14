#!/usr/bin/env python3
"""subset tests. Synthetic repos only; no network, no client data."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sampler
import subset
import repo_discovery
import subset_ui


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def make_repo(path: Path, commits: int) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for i in range(commits):
        (path / f"f{i}.txt").write_text(f"{i}\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"c{i}")
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repos = self.tmp / "repos"
        make_repo(self.repos / "acme" / "big", 40)
        make_repo(self.repos / "acme" / "small", 5)
        make_repo(self.repos / "solo", 30)
        (self.repos / "empty-dir").mkdir()


class DiscoveryTests(Base):
    def test_finds_nested_and_top_level_repos(self):
        names = [t.full_name for t in subset.discover_local_repos(self.repos)]
        self.assertEqual(names, ["acme/big", "acme/small", "solo"])

    def test_parse_remote_forms(self):
        t = subset.parse_remote("acme/widgets")
        self.assertEqual((t.platform, t.clone_url), ("github", "https://github.com/acme/widgets.git"))
        t = subset.parse_remote("https://gitlab.com/grp/sub/proj.git")
        self.assertEqual((t.platform, t.full_name), ("gitlab", "grp/sub/proj"))
        t = subset.parse_remote("git@bitbucket.org:ws/r.git")
        self.assertEqual((t.platform, t.full_name), ("bitbucket", "ws/r"))
        self.assertEqual(subset.parse_remote("a/b", "gitlab", "git.example.com").clone_url,
                         "https://git.example.com/a/b.git")
        for bad in ("", "justaname", "https://github.com/onlyowner"):
            with self.assertRaises(ValueError, msg=bad):
                subset.parse_remote(bad)

    def test_token_injection_and_redaction(self):
        t = subset.parse_remote("a/b")
        url = subset.authed_url(t, {"github": "ghp_secret"})
        self.assertIn("x-access-token:ghp_secret@", url)
        self.assertNotIn("ghp_secret", subset.redact(f"fatal: {url}", {"github": "ghp_secret"}))


class JobTests(Base):
    def run_job(self, targets, **kw):
        defaults = dict(pct=10, min_commits=20, workers=2, output_dir=self.tmp / "out")
        defaults.update(kw)
        return subset.run_job(targets, **defaults)

    def test_local_run_samples_logs_and_zips(self):
        result = self.run_job(subset.discover_local_repos(self.repos))
        report = result["report"]
        self.assertEqual({e["repo"] for e in report["sampled"]}, {"acme/big", "solo"})
        self.assertEqual([e["repo"] for e in report["skipped"]], ["acme/small"])
        self.assertIn("only 5 commits", report["skipped"][0]["reason"])
        out = result["run_dir"] / "samples" / "acme__big"
        self.assertEqual(git(out, "rev-list", "--count", "HEAD"), "4")
        self.assertEqual(git(self.repos / "acme" / "big", "rev-list", "--count", "HEAD"), "40")
        names = zipfile.ZipFile(result["zip"]).namelist()
        self.assertTrue(any("/samples/acme__big/.git/" in n for n in names))
        self.assertTrue(any(n.endswith("samples/sampling_report.json") for n in names))
        self.assertTrue(any(n.endswith("logs/subset.log") for n in names))
        # The zipped sample must still be a working git repo once extracted.
        extracted = self.tmp / "unzipped"
        zipfile.ZipFile(result["zip"]).extractall(extracted)
        unzipped = next(extracted.glob("subset-*/samples/acme__big"))
        self.assertEqual(git(unzipped, "rev-list", "--count", "HEAD"), "4")
        self.assertEqual(git(unzipped, "fsck", "--full"), "")

    def test_filter_logs_unselected_and_unmatched(self):
        targets = subset.discover_local_repos(self.repos)
        result = self.run_job(targets, repo_filter=frozenset({"big", "ghost"}))
        report = result["report"]
        self.assertEqual([e["repo"] for e in report["sampled"]], ["acme/big"])
        self.assertEqual(report["selectors_matching_no_repo"], ["ghost"])
        reasons = {e["repo"]: e["reason"] for e in report["skipped"]}
        self.assertIn("not in the selected sample list", reasons["solo"])

    def test_remote_clone_path_and_clone_failure(self):
        good = subset.Target(full_name="org/cloned", clone_url=(self.repos / "acme" / "big").as_uri(), platform="github")
        bad = subset.Target(full_name="org/missing", clone_url=(self.tmp / "nope").as_uri(), platform="github")
        result = self.run_job([good, bad])
        report = result["report"]
        self.assertEqual([e["repo"] for e in report["sampled"]], ["org/cloned"])
        self.assertIn("clone failed", report["skipped"][0]["reason"])
        self.assertFalse((result["run_dir"] / "clones").exists())
        self.assertFalse(any("/clones/" in n for n in zipfile.ZipFile(result["zip"]).namelist()))

    def test_defaults_are_five_percent_and_min_twenty(self):
        self.assertEqual((subset.SAMPLE_PCT, subset.MIN_COMMITS), (5.0, 20))
        result = subset.run_job(subset.discover_local_repos(self.repos), output_dir=self.tmp / "dflt")
        report = result["report"]
        self.assertEqual((report["sample_pct"], report["min_commits"]), (5.0, 20))
        self.assertEqual(git(result["run_dir"] / "samples" / "acme__big", "rev-list", "--count", "HEAD"), "2")
        self.assertIn("below the minimum of 20", report["skipped"][0]["reason"])

    def test_no_knob_for_pct_or_min_commits(self):
        for flag in ("--pct", "--min-commits"):
            with self.assertRaises(SystemExit):
                subset.main(["--local-repos-dir", str(self.repos), flag, "10"])
        with unittest.mock.patch.dict(os.environ, {"SAMPLE": "50", "SAMPLE_MIN_COMMITS": "1"}):
            result = subset.run_job(subset.discover_local_repos(self.repos), output_dir=self.tmp / "env")
        self.assertEqual(result["report"]["sample_pct"], 5.0)
        self.assertEqual(result["report"]["sampled_count"], 2)

    def test_cli_end_to_end(self):
        rc = subset.main(["--local-repos-dir", str(self.repos), "--repo", "solo",
                          "--output-dir", str(self.tmp / "cli-out")])
        self.assertEqual(rc, 0)
        run = next((self.tmp / "cli-out").glob("subset-*/samples/solo"))
        self.assertEqual(git(run, "rev-list", "--count", "HEAD"), "2")


class FakeForge(BaseHTTPRequestHandler):
    """One server pretending to be GitHub, GitLab, Bitbucket Cloud and Data Center."""
    seen_auth: list = []

    def log_message(self, *_a):
        pass

    def send(self, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        FakeForge.seen_auth.append(
            (self.path.split("?")[0], self.headers.get("Authorization") or self.headers.get("PRIVATE-TOKEN")))
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        page = int(q.get("page", ["1"])[0])
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if url.path == "/gh/user/repos":
            if "bad" in (self.headers.get("Authorization") or ""):
                self.send_response(401); self.end_headers(); return
            names = [f"acme/r{i:03d}" for i in range(150)] if page <= 2 else []
            chunk = names[(page - 1) * 100: page * 100]
            self.send([{"full_name": n, "clone_url": f"https://github.com/{n}.git"} for n in chunk])
        elif url.path == "/gl/api/v4/projects":
            self.send([{"path_with_namespace": "grp/sub/p1", "http_url_to_repo": "https://gl/grp/sub/p1.git"}] if page == 1 else [])
        elif url.path == "/bb/repositories":
            if "next" in q:
                self.send({"values": [{"full_name": "ws/two", "links": {"clone": [{"name": "https", "href": "https://u@bitbucket.org/ws/two.git"}]}}]})
            else:
                self.send({"values": [{"full_name": "ws/one", "links": {"clone": [{"name": "ssh", "href": "git@x"}, {"name": "https", "href": "https://u@bitbucket.org/ws/one.git"}]}}],
                           "next": f"{base}/bb/repositories?next=1"})
        elif url.path == "/dc/rest/api/1.0/repos":
            start = int(q.get("start", ["0"])[0])
            if start == 0:
                self.send({"values": [{"slug": "alpha", "project": {"key": "PROJ"}, "links": {"clone": [
                    {"name": "ssh", "href": "ssh://git@dc/proj/alpha.git"},
                    {"name": "http", "href": "https://bob@dc.example/scm/proj/alpha.git"}]}}],
                    "isLastPage": False, "nextPageStart": 1})
            else:
                self.send({"values": [{"slug": "beta", "project": {"key": "~USER"}, "links": {"clone": [
                    {"name": "http", "href": "https://dc.example/scm/~user/beta.git"}]}}], "isLastPage": True})
        else:
            self.send_response(404); self.end_headers()


class ForgeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        FakeForge.seen_auth = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeForge)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(setattr, repo_discovery, "GITHUB_API", repo_discovery.GITHUB_API)
        self.addCleanup(setattr, repo_discovery, "BITBUCKET_CLOUD_API", repo_discovery.BITBUCKET_CLOUD_API)
        repo_discovery.GITHUB_API = f"{self.base}/gh"
        repo_discovery.BITBUCKET_CLOUD_API = f"{self.base}/bb"

    def test_github_paginates(self):
        found = repo_discovery.discover_remote("github", "tok")
        self.assertEqual(len(found), 150)
        self.assertEqual(found[0].full_name, "acme/r000")
        self.assertEqual(found[0].platform, "github")
        self.assertIn(("/gh/user/repos", "Bearer tok"), FakeForge.seen_auth)

    def test_gitlab_uses_private_token_header(self):
        found = repo_discovery.discover_remote("gitlab", "glpat", host=self.base + "/gl")
        self.assertEqual([t.full_name for t in found], ["grp/sub/p1"])
        self.assertIn(("/gl/api/v4/projects", "glpat"), FakeForge.seen_auth)

    def test_bitbucket_cloud_follows_next_and_strips_userinfo(self):
        found = repo_discovery.discover_remote("bitbucket", "tok")
        self.assertEqual([t.full_name for t in found], ["ws/one", "ws/two"])
        self.assertEqual(found[0].clone_url, "https://bitbucket.org/ws/one.git")

    def test_bitbucket_data_center(self):
        found = repo_discovery.discover_remote("bitbucket_dc", "pat", host=self.base + "/dc/")
        self.assertEqual([t.full_name for t in found], ["PROJ/alpha", "~USER/beta"])
        self.assertEqual(found[0].clone_url, "https://dc.example/scm/proj/alpha.git")
        self.assertEqual(found[0].platform, "bitbucket_dc")
        self.assertIn(("/dc/rest/api/1.0/repos", "Bearer pat"), FakeForge.seen_auth)

    def test_errors_are_readable_and_token_free(self):
        with self.assertRaisesRegex(repo_discovery.DiscoveryError, "token was rejected"):
            repo_discovery.discover_remote("github", "bad-token")
        with self.assertRaisesRegex(repo_discovery.DiscoveryError, "No GitHub token"):
            repo_discovery.discover_remote("github", "")
        with self.assertRaisesRegex(repo_discovery.DiscoveryError, "server URL"):
            repo_discovery.discover_remote("bitbucket_dc", "pat", host="")
        with self.assertRaisesRegex(repo_discovery.DiscoveryError, "not found"):
            repo_discovery.discover_remote("bitbucket_dc", "pat", host=self.base + "/nope")

    def test_dc_clone_uses_bearer_header_not_url(self):
        t = subset.Target("PROJ/alpha", clone_url="https://dc.example/scm/proj/alpha.git", platform="bitbucket_dc")
        self.assertEqual(subset.authed_url(t, {"bitbucket_dc": "pat"}), "https://dc.example/scm/proj/alpha.git")
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
        subset.git_config_env(env, [("http.extraHeader", "Authorization: Bearer pat"), ("http.sslVerify", "false")])
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.extraHeader")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "safe.directory")

    def test_tokens_file_routes_dc_keys(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "tokens"
            f.write_text("bitbucket_token=cloud\nbitbucket_dc_token=dc1\ngithub_data=gh\nbitbucket_server=srv\n")
            saved = {k: os.environ.pop(k, None) for k in ("BITBUCKET_TOKEN", "BITBUCKET_DC_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")}
            try:
                tokens = subset.parse_tokens_file(f)
            finally:
                os.environ.update({k: v for k, v in saved.items() if v})
        self.assertEqual(tokens, {"bitbucket": "cloud", "bitbucket_dc": "dc1", "github": "gh"})

    def test_cli_discover_list_only(self):
        from io import StringIO
        from unittest import mock
        with mock.patch("sys.stdout", new=StringIO()) as out, mock.patch.dict(os.environ, {"BITBUCKET_DC_TOKEN": "pat"}):
            rc = subset.main(["--discover", "bitbucket-dc", "--host", self.base + "/dc", "--list-only",
                              "--tokens-file", "/nonexistent"])
        self.assertEqual(rc, 0)
        self.assertIn("PROJ/alpha", out.getvalue())


def commit_dated(path: Path, name: str, date: str) -> None:
    (path / name).write_text(name)
    git(path, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com",
           "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", "commit", "-q", "-m", name], cwd=path, check=True, capture_output=True, env=env)


class HardeningTests(Base):
    def sample(self, source: Path, pct: float = 10, min_commits: int = 20, disposable: bool = False):
        import logging
        run_dir = self.tmp / "run"
        ok = sampler.take_sample(repo_name=source.name, slug=source.name, source=source,
                                 disposable=disposable, run_dir=run_dir, pct=pct,
                                 min_commits=min_commits, log=logging.getLogger("t"))
        out = run_dir / "samples" / source.name
        reason = ""
        log = run_dir / "samples" / sampler.LOG_NAME
        if log.is_file():
            reason = json.loads(log.read_text().splitlines()[-1]).get("reason", "")
        return ok, out, reason

    def test_skewed_dates_do_not_leak_history(self):
        repo = self.tmp / "skew"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        for i in range(40):  # each later commit has an OLDER date
            commit_dated(repo, f"f{i}", f"{2020 - i}-01-01T00:00:00Z")
        ok, out, _ = self.sample(repo)
        self.assertTrue(ok)
        self.assertEqual(git(out, "rev-list", "--count", "--all"), "4")
        self.assertEqual(git(out, "log", "-1", "--format=%s"), "f3")

    def test_merge_history_never_exceeds_keep(self):
        repo = make_repo(self.tmp / "merge", 10)
        git(repo, "checkout", "-q", "-b", "side", "HEAD~5")
        for i in range(8):
            (repo / f"s{i}").write_text("s")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", f"s{i}")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "-m", "merge", "side")
        for i in range(25):
            (repo / f"m{i}").write_text("m")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", f"m{i}")
        total = int(git(repo, "rev-list", "--count", "main"))
        ok, out, _ = self.sample(repo, pct=30)
        self.assertTrue(ok)
        keep = -(-total * 30 // 100)
        self.assertLessEqual(int(git(out, "rev-list", "--count", "--all")), keep)

    def test_all_extra_refs_and_remotes_are_dropped(self):
        repo = make_repo(self.tmp / "refs", 40)
        late = git(repo, "rev-parse", "HEAD")
        git(repo, "tag", "-a", "v1", "-m", "x")
        (repo / "wip").write_text("w")
        git(repo, "add", "wip")
        git(repo, "stash", "-q")
        git(repo, "notes", "add", "-m", "n", "HEAD")
        git(repo, "update-ref", "refs/pull/1/head", late)
        git(repo, "update-ref", "refs/original/refs/heads/main", late)
        git(repo, "update-ref", "refs/remotes/upstream/main", late)
        git(repo, "remote", "add", "upstream", "https://user:pw@example.invalid/x.git")
        git(repo, "config", "branch.main.remote", "upstream")
        (repo / ".git" / "FETCH_HEAD").write_text(late + "\t\turl\n")
        (repo / ".git" / "ORIG_HEAD").write_text(late + "\n")
        ok, out, reason = self.sample(repo)
        self.assertTrue(ok, reason)
        refs = git(out, "for-each-ref", "--format=%(refname)").splitlines()
        self.assertEqual(refs, ["refs/heads/sample10pct"])
        self.assertEqual(git(out, "rev-list", "--count", "--all"), "4")
        cfg = (out / ".git" / "config").read_text()
        self.assertNotIn("pw@", cfg)
        self.assertNotIn("remote", cfg)
        self.assertFalse((out / ".git" / "FETCH_HEAD").exists())
        self.assertFalse((out / ".git" / "ORIG_HEAD").exists())
        # post-cutoff commit objects are physically gone, not just unreachable
        missing = subprocess.run(["git", "cat-file", "-e", late], cwd=out)
        self.assertNotEqual(missing.returncode, 0)

    def test_lfs_objects_removed(self):
        repo = make_repo(self.tmp / "lfs", 40)
        blob = repo / ".git" / "lfs" / "objects" / "ab" / "cd" / "abcdef"
        blob.parent.mkdir(parents=True)
        blob.write_text("post-cutoff blob")
        ok, out, reason = self.sample(repo)
        self.assertTrue(ok, reason)
        self.assertFalse((out / ".git" / "lfs").exists())
        self.assertTrue(blob.exists(), "source repo must stay untouched")

    def test_shallow_clone_is_skipped(self):
        full = make_repo(self.tmp / "full", 40)
        shallow = self.tmp / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "30", full.as_uri(), str(shallow)],
                       check=True, capture_output=True)
        ok, _, reason = self.sample(shallow, min_commits=20)
        self.assertFalse(ok)
        self.assertIn("shallow", reason)

    def _repo_with_feature(self, name: str, feature_date: str, main_date: str = "2020-01-01T00:00:00"):
        """main: 40 commits dated main_date; feature (checked out): 10 more commits dated feature_date."""
        path = self.tmp / name
        path.mkdir()
        git(path, "init", "-q", "-b", "main")

        def commit(label: str, date: str) -> None:
            (path / label).write_text(label)
            git(path, "add", "-A")
            subprocess.run(["git", "commit", "-q", "-m", label], cwd=path, check=True, capture_output=True,
                           env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com", "GIT_COMMITTER_NAME": "t",
                                "GIT_COMMITTER_EMAIL": "t@e.com", "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})

        for i in range(40):
            commit(f"m{i}", main_date)
        git(path, "checkout", "-q", "-b", "feature")
        for i in range(10):
            commit(f"x{i}", feature_date)
        return path

    def test_the_branch_with_the_newest_commit_is_sampled_not_the_default(self):
        ok, out, reason = self.sample(self._repo_with_feature("newer", "2024-01-01T00:00:00"))
        self.assertTrue(ok, reason)
        manifest = json.loads((out / "SAMPLE_MANIFEST.json").read_text())
        self.assertEqual((manifest["branch_sampled"], manifest["total_commits"]), ("feature", 50))
        self.assertIn("newest commit", manifest["branch_chosen_by"])

    def test_a_tie_between_branches_goes_to_the_default_branch(self):
        same = "2022-01-01T00:00:00"
        ok, out, reason = self.sample(self._repo_with_feature("tie", same, main_date=same))
        self.assertTrue(ok, reason)
        manifest = json.loads((out / "SAMPLE_MANIFEST.json").read_text())
        self.assertEqual((manifest["branch_sampled"], manifest["total_commits"]), ("main", 40))

    def test_default_branch_prefers_main_over_checked_out_feature_when_asked(self):
        with unittest.mock.patch.dict(os.environ, {"SUBSET_USE_DEFAULT_BRANCH": "1"}):
            ok, out, reason = self.sample(self._repo_with_feature("old", "2024-01-01T00:00:00"))
        self.assertTrue(ok, reason)
        manifest = json.loads((out / "SAMPLE_MANIFEST.json").read_text())
        self.assertEqual((manifest["branch_sampled"], manifest["total_commits"]), ("main", 40))
        self.assertIn("main", manifest["branch_chosen_by"])

    def test_source_hooks_and_fsmonitor_never_run(self):
        repo = make_repo(self.tmp / "evil", 40)
        marker = self.tmp / "ran"
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        mon = self.tmp / "mon.sh"
        mon.write_text(f"#!/bin/sh\ntouch {marker}\nprintf '\\0'\n")
        mon.chmod(0o755)
        git(repo, "config", "core.hooksPath", str(hooks))
        git(repo, "config", "core.fsmonitor", str(mon))
        marker.unlink(missing_ok=True)
        ok, out, reason = self.sample(repo)
        self.assertTrue(ok, reason)
        self.assertFalse(marker.exists())
        cfg = (out / ".git" / "config").read_text().lower()
        self.assertNotIn("hookspath", cfg)
        self.assertNotIn("fsmonitor", cfg)

    def test_fsck_failure_is_a_skip(self):
        repo = make_repo(self.tmp / "bad", 40)
        real = sampler._git

        def fake(args, cwd, check=True, timeout=sampler.GIT_TIMEOUT_SECONDS):
            if args[:1] == ["fsck"]:
                return subprocess.CompletedProcess(args, 1, "broken link", "")
            return real(args, cwd, check, timeout)

        with unittest.mock.patch.object(sampler, "_git", fake):
            ok, out, reason = self.sample(repo)
        self.assertFalse(ok)
        self.assertIn("fsck", reason)
        self.assertFalse(out.exists())

    def test_zip_stores_symlinks_not_targets(self):
        secret = self.tmp / "host-secret"
        secret.write_text("TOPSECRET")
        run_dir = self.tmp / "subset-x"
        (run_dir / "samples").mkdir(parents=True)
        (run_dir / "samples" / "link").symlink_to(secret)
        (run_dir / "samples" / "dirlink").symlink_to(self.tmp, target_is_directory=True)
        (run_dir / "samples" / "real.txt").write_text("ok")
        with zipfile.ZipFile(subset.zip_run_dir(run_dir)) as zf:
            names = zf.namelist()
            self.assertEqual(zf.read("subset-x/samples/link"), str(secret).encode())
            self.assertEqual(zf.getinfo("subset-x/samples/link").external_attr >> 28, 0o12)
            self.assertIn("subset-x/samples/dirlink", names)
            self.assertFalse(any(n.startswith("subset-x/samples/dirlink/") for n in names))
            self.assertEqual(zf.read("subset-x/samples/real.txt"), b"ok")
            self.assertNotIn(b"TOPSECRET", b"".join(zf.read(n) for n in names if not n.endswith("/")))


class RepoSelectionTests(Base):
    def test_parent_that_is_a_repo_does_not_hijack_selection(self):
        parent = make_repo(self.tmp / "parent", 3)
        make_repo(parent / "child", 40)
        self.assertEqual([t.full_name for t in subset.discover_local_repos(parent)], ["parent"])
        got = subset.resolve_local_selectors(parent, ["child"])
        self.assertEqual([t.full_name for t in got], ["child"])
        self.assertEqual(got[0].local_path, parent.resolve() / "child")

    def test_ambiguous_bare_name_errors(self):
        make_repo(self.repos / "other" / "big", 30)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            subset.resolve_local_selectors(self.repos, ["big"])
        got = subset.resolve_local_selectors(self.repos, ["acme/big"])
        self.assertEqual([t.full_name for t in got], ["acme/big"])
        rc = subset.main(["--local-repos-dir", str(self.repos), "--repo", "big",
                          "--output-dir", str(self.tmp / "o")])
        self.assertEqual(rc, 2)

    def test_squirrel_style_invocation_samples_exact_repo(self):
        parent = make_repo(self.tmp / "parent2", 3)
        make_repo(parent / "child", 40)
        rc = subset.main(["--local-repos-dir", str(parent), "--repo", "child",
                          "--output-dir", str(self.tmp / "o2")])
        self.assertEqual(rc, 0)
        out = next((self.tmp / "o2").glob("subset-*/samples/child"))
        self.assertEqual(git(out, "rev-list", "--count", "HEAD"), "2")

    def test_repo_path(self):
        rc = subset.main(["--repo-path", str(self.repos / "solo"), "--output-dir", str(self.tmp / "o3")])
        self.assertEqual(rc, 0)
        self.assertTrue(next((self.tmp / "o3").glob("subset-*/samples/solo")).is_dir())
        self.assertEqual(subset.main(["--repo-path", str(self.tmp / "nope")]), 2)


class UiTests(Base):
    def setUp(self):
        super().setUp()
        subset.DEFAULT_OUTPUT = self.tmp / "ui-out"
        subset_ui.STATE.update(running=False, progress={}, error=None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), subset_ui.Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def post(self, path, **fields):
        data = urllib.parse.urlencode({"csrf_token": subset_ui.CSRF_TOKEN, **fields}, doseq=True).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data)) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_page_and_csrf(self):
        html = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn("Create samples", html)
        self.assertNotIn("How much", html)
        self.assertNotIn('name="pct"', html)
        self.assertIn("first 5%", html)
        self.assertNotIn("__", html.replace("__init__", ""))
        req = urllib.request.Request(self.base + "/start", data=b"pct=5")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)

    def test_discover_and_validation(self):
        code, body = self.post("/discover", local_dir=str(self.repos))
        self.assertEqual(json.loads(body)["items"], ["acme/big", "acme/small", "solo"])
        self.assertEqual(self.post("/discover", local_dir=str(self.tmp / "none"))[0], 400)
        self.assertEqual(self.post("/start")[0], 400)  # nothing to sample
        self.assertEqual(self.post("/start", remote_repos="github|never-detected")[0], 400)

    def test_detect_then_sample_ticked_remote(self):
        # a "detected" remote that is really a local bare-ish clone reachable by file://
        src = self.repos / "acme" / "big"
        subset_ui.STATE["detected"] = {
            "github|org/cloned": subset.Target("org/cloned", clone_url=src.as_uri(), platform="github"),
            "github|org/other": subset.Target("org/other", clone_url=src.as_uri(), platform="github"),
        }
        code, _ = self.post("/start", remote_repos=["github|org/cloned"])
        self.assertEqual(code, 200)
        for _ in range(100):
            snap = json.loads(urllib.request.urlopen(self.base + "/status").read())
            if snap["progress"].get("finished") and not snap["running"]:
                break
            threading.Event().wait(0.2)
        self.assertEqual((snap["progress"]["total"], snap["progress"]["sampled"]), (1, 1))

    def test_detect_endpoint_errors(self):
        code, body = self.post("/detect", platform="bitbucket_dc", token="x")
        self.assertEqual(code, 400)
        self.assertIn("server URL", json.loads(body)["error"])
        code, body = self.post("/detect", platform="nope")
        self.assertEqual(code, 400)

    def test_full_run_via_ui(self):
        code, _ = self.post("/start", local_dir=str(self.repos), pct="50", min_commits="1",
                            local_repos=["acme/big"])
        self.assertEqual(code, 200)
        for _ in range(100):
            snap = json.loads(urllib.request.urlopen(self.base + "/status").read())
            if snap["progress"].get("finished") and not snap["running"]:
                break
            threading.Event().wait(0.2)
        self.assertEqual((snap["progress"]["sampled"], snap["progress"]["skipped"]), (1, 0))
        zipped = urllib.request.urlopen(self.base + "/download").read()
        self.assertTrue(zipped.startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()
