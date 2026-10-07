# subset (version 1.0.0)

Keep only the **first 5% of each repo's commit history** and package the results as a zip.
Sampling only: no metrics, no profiling. Network use is limited to listing repos with your token and `git clone`.

Use it when you already know which repos you want a history sample of and don't need any
metrics about them.

## Fixed settings

Every sample is the **first 5% of commits** (rounded up, at least 1), and repos with **fewer than 20
commits are skipped** and logged. These are fixed in the code (`SAMPLE_PCT` and `MIN_COMMITS` in
`subset.py`); they cannot be changed from `.env`, the UI or the command line.

## What you get

Each run creates `outputs/subset-<timestamp>/` and a matching `outputs/subset-<timestamp>.zip`:

```
samples/<org__repo>/            a standalone git repo holding the first 5% of commits
samples/<org__repo>/SAMPLE_MANIFEST.json   branch, totals, cutoff commit, fsck result
samples/sampling_log.jsonl      one line per repo: sampled, or skipped + the reason
samples/sampling_report.json    the same, summarised (sampled / skipped lists)
logs/subset.log                 full run log
```

**The samples contain real source code and history.** Treat the zip accordingly.

How a sample is cut: take the repo's default branch, keep the oldest `ceil(total x 5 / 100)`
commits, delete every other branch, tag and remote, then `git gc`. Tags are deleted because a tag
past the cutoff would keep the later history reachable.

## Quick start (Docker, with the UI)

```bash
cp .env.example .env            # optional: set LOCAL_REPOS_DIR, UI_PORT
docker compose up --build
```

Open <http://localhost:8768>.

1. Put the git clones you want sampled in `./repos` (or set `LOCAL_REPOS_DIR` in `.env` to any
   host folder). Inside the container this folder is `/data/repos`, which is the UI's default.
2. Click **Load repos**. Every git repo found up to 3 folders deep is listed, grouped by folder (a toggle per group; click a group to expand it).
3. Tick the repos to sample. **Nothing ticked = every repo found.**
4. Optional: detect remote repos with a token (GitHub, GitLab, Bitbucket Cloud or Data Center) and tick the ones you want. See "Detecting remote repos".
5. Click **Create samples**. Watch progress, then **Download zip**. The same files are in `./outputs`.

When the run ends the page lists every repo that was **not** sampled and why.

## Without Docker

Needs Python 3.10+ and `git`. No pip packages.

```bash
python subset.py --ui                          # UI on http://127.0.0.1:8768
python subset.py --local-repos-dir ./repos
python subset.py --local-repos-dir ./repos --repo alpha --repo acme/beta
python subset.py --remote acme/widgets --remote https://gitlab.com/grp/proj
python subset.py --remote-file remote.txt --workers 8
```

| Flag | Meaning |
|---|---|
| `--local-repos-dir DIR` | Sample every git repo under DIR (3 levels deep). Copied, never modified. |
| `--remote SPEC` / `--remote-file F` | Clone then sample. SPEC is `owner/repo`, an https URL, or `git@host:owner/repo`. |
| `--discover PLATFORM` | `github`, `gitlab`, `bitbucket`, `bitbucket-dc`: list every repo the token can see and sample them (narrow with `--repo`). |
| `--host URL` / `--workspace W` | Data Center base URL or GitLab host / Bitbucket Cloud workspace, for `--discover`. |
| `--list-only` | With `--discover`: print the repos and exit. |
| `--insecure-tls` | Skip TLS verification (self-signed servers only). |
| `--default-platform` | What a bare `owner/repo` means: `github` (default), `gitlab`, `bitbucket`. |
| `--gitlab-host` | Host for bare `owner/repo` when the platform is `gitlab`. |
| `--repo NAME` | Only sample these (repeatable). `org/repo` or bare `repo`. Others are logged as not sampled. |
| `--workers N` | Repos processed in parallel (default 4). |
| `--output-dir DIR` | Default `./outputs` (`SUBSET_OUTPUT_DIR` also works). |

Exit code: 0 if at least one repo was sampled, 1 if none were, 2 for bad input.

## Detecting remote repos with a token

No need to type repo names. In the UI, under "Or detect remote repos with an access token":

1. Pick the platform: **GitHub**, **GitLab**, **Bitbucket Cloud** or **Bitbucket Data Center**.
2. Fill the extra field if shown: the **Server URL** for Data Center (for example
   `https://bitbucket.example.com`, a context path such as `/bitbucket` is fine), a **Host** for
   self-hosted GitLab, or an optional **Workspace** for Bitbucket Cloud.
3. Paste a token, or leave it blank to use one from the environment or `tokens` file (below).
4. Click **Detect repos**. Every repo the token can see is listed, one expandable toggle per org
   (GitHub owner, GitLab top-level group, Bitbucket workspace, Data Center project). Click an org to
   show its repos. The checkbox on an org ticks or unticks all its repos (shown as partly ticked when
   only some are) and the `3 / 12` count shows how many are ticked. The filter box searches repo names
   and opens the orgs that match. Tick the repos to sample. Run detection again for another platform; the lists are combined.

**For remote repos, nothing ticked means none** (a token can see thousands of repos). Local repos
work the other way round: nothing ticked means all.

| Platform | Token type | Listing call | Notes |
|---|---|---|---|
| GitHub | Fine-grained or classic PAT | `GET /user/repos` | Needs read access to contents/metadata. Lists owned, collaborator and org-member repos. |
| GitLab | PAT with `read_api` + `read_repository` | `GET /api/v4/projects?membership=true` | Fine-grained tokens return empty lists, use a classic PAT. |
| Bitbucket Cloud | Workspace/repository access token or OAuth token | `GET /2.0/repositories` | An app password does not work as a bearer token. Give a workspace if the token is workspace-scoped. |
| Bitbucket Data Center | HTTP access token (personal or project/repo) | `GET /rest/api/1.0/repos` | Sent as `Authorization: Bearer`. Repos named `PROJECT/slug`; personal repos show as `~USER/slug`. |

Bitbucket Data Center clones use the same Bearer token through git's `http.extraHeader`, passed in
the environment, so the token never appears in a URL, a command line or the logs. Only HTTP(S)
clone links are used; SSH is not.

**Self-signed or internal-CA servers:** tick "Skip TLS verification" (GitLab and Data Center).
It disables certificate checks for detection and clones of that platform, so only use it for a
server you trust. In the CLI it is `--insecure-tls`.

CLI equivalents:

```bash
python subset.py --discover bitbucket-dc --host https://bitbucket.example.com --list-only
python subset.py --discover bitbucket-dc --host https://bitbucket.example.com --repo PROJ/alpha
python subset.py --discover github --repo alpha --repo acme/beta     # sample only these
python subset.py --discover gitlab --host https://gitlab.example.com --list-only
```

`--discover` samples **every** repo found unless you narrow it with `--repo`, so use `--list-only`
first on a big account.

### Where tokens come from

For detection and for cloning private repos, most specific wins:

1. The token box in the UI (kept in memory for the session, never written to disk or logs).
2. Environment: `GH_TOKEN` / `GITHUB_TOKEN`, `GITLAB_TOKEN`, `BITBUCKET_TOKEN`, `BITBUCKET_DC_TOKEN`
   (plus `BITBUCKET_DC_URL` for the server URL).
3. A `tokens` file (CLI: `./tokens`, Docker: `secrets/tokens`). Copy `tokens.example`. A key
   containing `github`, `gitlab` or `bitbucket` is used for that platform; a bitbucket key that
   also contains `dc`, `server` or `datacenter` (for example `bitbucket_dc_token`) is the Data Center token.

Never commit `tokens`, `secrets/` or `.env`.

### Typing repos (CLI only)

`--remote owner/repo`, `--remote https://...` or `--remote-file F` still work for a handful of
public or known repos. They are not in the UI any more.

## Settings (`.env`)

| Key | Default | Meaning |
|---|---|---|
| `UI_PORT` | 8768 | Host port for the UI. |
| `LOCAL_REPOS_DIR` | `./repos` | Host folder mounted read-only at `/data/repos`. |
| `SAMPLE_REPOS` | empty | CLI only: comma-separated repos to limit sampling to. |
| `BITBUCKET_DC_URL` | empty | Bitbucket Data Center base URL; prefills the UI and `--discover bitbucket-dc`. |

## Why a repo was not sampled

Every skipped repo gets a `SAMPLE SKIPPED <repo>: <reason>` line in the log, an entry in
`sampling_log.jsonl`, and a row in the UI's "not sampled" table. Reasons:

| Reason | Fix |
|---|---|
| `only N commits on 'main', below the minimum of 20` | By design: a 5% slice of a very short history isn't meaningful. The limit is fixed. |
| `no branch to sample (detached HEAD and no origin/HEAD)` | Check out a branch in the clone. |
| `source is a partial (blobless) clone ...` | Re-clone with full history (no `--filter=blob:none`). |
| `not a non-bare git working tree` | Bare mirrors aren't supported; clone a working tree. |
| `clone failed: ...` | Check the token's clone access and network. Tokens are redacted in the message. |
| `not in the selected sample list` | It wasn't ticked / named in `--repo`. |

A name passed to `--repo` that matches no repo is reported under `selectors_matching_no_repo`.

## Notes and limits

- Local repos are **copied** before slicing, so you need free disk space for one copy per worker at a time.
- Only the default branch's history is sampled (the branch checked out in a local clone).
- One run at a time in the UI. Close the terminal and a running job stops.
- The UI binds to `127.0.0.1` (or `0.0.0.0` inside Docker, published to your host only through the compose port).

## Tests

```bash
python -m unittest        # synthetic repos only, no network
```
