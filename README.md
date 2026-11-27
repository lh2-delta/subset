# subset (version 1.0.0)

Keep only the **first 5% of each repo's commit history** and package the results as a zip.
Sampling only: no metrics, no profiling. Network use is limited to listing repos with your token, `git clone`, and `cm` for Plastic SCM servers.

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
samples/sampling_log.jsonl      one line per repo: sampled (+ the branch), or skipped + the reason
samples/sampling_report.json    the same, summarised (sampled / skipped lists)
logs/subset.log                 full run log
```

**The samples contain real source code and history.** Treat the zip accordingly.

How a sample is cut: take the repo's default branch (or the branch you chose, see "Choosing the branch"), keep the oldest `ceil(total x 5 / 100)`
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
5. Optional: type a **Branch** for every repo, or click a repo's **branch** button to pick one of its branches
   from a dropdown (see "Choosing the branch").
6. Click **Create samples**. Watch progress, then **Download zip**. The same files are in `./outputs`.

When the run ends the page lists every repo that was **not** sampled and why.

## Without Docker

Needs Python 3.10+ and `git`. No pip packages.

```bash
python subset.py --ui                          # UI on http://127.0.0.1:8768
python subset.py --local-repos-dir ./repos
python subset.py --local-repos-dir ./repos --repo alpha --repo acme/beta
python subset.py --remote acme/widgets --remote https://gitlab.com/grp/proj
python subset.py --remote-file remote.txt --workers 8
python subset.py --local-repos-dir ./repos --branch develop --repo-branch acme/beta=release/2.0
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
| `--branch NAME` | Sample this branch in every repo instead of its default branch (Plastic: `/main/child`). |
| `--repo-branch REPO=NAME` | Branch for one repo (`org/repo` or bare name, repeatable); wins over `--branch`. |
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

## Choosing the branch

By default each repo's default branch is sampled: `origin/HEAD`, else `main` / `master` / `trunk`, else
the checked-out branch (Plastic: `/main`). To sample another branch:

| Where | How |
|---|---|
| CLI, every repo | `--branch develop` |
| CLI, one repo | `--repo-branch acme/beta=release/2.0` (repeatable; wins over `--branch`) |
| `--remote` / `--remote-file` line | `acme/beta [branch=release/2.0]` (wins over both flags) |
| UI, every repo | the **Branch** box under the repo lists |
| UI, one repo | the repo's **branch** button: lists its branches (local refs, `git ls-remote` for remote repos, `cm find branch` for Plastic) in a dropdown, defaulting to the default branch; wins over the Branch box |

A local branch is used first, then `origin/<name>` (a fresh clone only has the default branch locally).
**A repo that does not have the branch is not sampled**: it is logged as `branch 'X' not found (branches: ...)`
with `requested_branch` in `sampling_log.jsonl`. It is never swapped for the default branch.
The branch sampled is recorded in `SAMPLE_MANIFEST.json` (`branch_sampled`, `branch_chosen_by: "requested"`),
in each `sampling_log.jsonl` line, and in `sampling_report.json` (`branch`, `repo_branches`).

## Unity Version Control (Plastic SCM)

Plastic repos are sampled the same way as git repos: the history is turned into a git repo first, then the first 5% of
commits of Plastic's `/main` branch is kept (repos under 20 changesets on `/main` are skipped). Each Plastic changeset becomes one
git commit (message `Changeset: N <comment>`); child branches, labels and other refs are removed as usual.
`SAMPLE_MANIFEST.json` adds `"source_vcs": "plastic"` and the repo spec or export file name.

**Another Plastic branch.** `--branch /main/task001` (or the UI) samples a child branch instead of `/main`. The export already
holds every branch, so the branch is looked up among the imported git branches as `main/task001`, `main-task001` or `task001`
(the name `cm fast-export` gives it can vary by version); the manifest adds `"plastic_branch"`. If none of those exists the repo is
not sampled and the reason lists the imported branch names, so you can pass the exact one. For a `.fe` file the UI shows a text box
instead of a dropdown (the file is only read when sampled).

Three ways in:

| Input | Needs `cm`? | How |
|---|---|---|
| A repo on a Plastic server | yes | CLI `--plastic name@server:8087` (Cloud: `name@org@cloud`); UI: platform **Unity Version Control (Plastic SCM)**, enter the server, **Detect repos**, tick. CLI for all repos: `--plastic-server server:8087` (`--list-only` to look first, `--repo` to narrow). |
| A Plastic workspace folder (has `.plastic/`) | yes | Put it under the local folder; the repo is read from `.plastic/plastic.selector` and exported from its server. The folder itself holds no history. |
| A file made with `cm fast-export repo@server file.fe` | no | Put the `.fe` file under the local folder (it is listed like a repo), or CLI `--plastic-export file.fe`. Easiest when the client runs the export. |

How a server repo is read: `cm fast-export <repspec> <file>` then `git fast-import`, in the run's scratch folder; the export file
is deleted afterwards. The whole history is exported (Plastic changeset numbers span all branches, so the cut is made after import).

**The `cm` client.** Plastic lists and exports with `cm`'s own login, not a token from this tool. Without Docker, install Unity
Version Control and sign in (`cm configure`, or a `cm profile`); `PLASTIC_CM` can point at a `cm` that is not on `PATH`. With
Docker use the `subset-plastic` service, which adds `cm` to the image (amd64 only; it runs under emulation on Apple silicon):

```bash
docker compose --profile plastic run --rm subset-plastic cm configure   # once; kept in ./secrets/plastic4
docker compose --profile plastic up --build subset-plastic              # UI on http://localhost:8768
```

Run either `subset` or `subset-plastic`, not both (same port). The image installs `cm` with
`docker/install-plastic-cm.sh`, which checks the vendor's signed `Release`, then the `Packages` and `.deb` checksums.

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
| `branch 'X' not found (branches: ...)` | The repo has no such branch. Pick one from the list, or leave the branch blank for the default. |

A name passed to `--repo` that matches no repo is reported under `selectors_matching_no_repo`.

## Notes and limits

- Local repos are **copied** before slicing, so you need free disk space for one copy per worker at a time.
- One branch per repo is sampled: the default branch, or the one you chose.
- One run at a time in the UI. Close the terminal and a running job stops.
- The UI binds to `127.0.0.1` (or `0.0.0.0` inside Docker, published to your host only through the compose port).

## Tests

```bash
python -m unittest        # synthetic repos only, no network
```
