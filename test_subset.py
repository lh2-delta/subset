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
