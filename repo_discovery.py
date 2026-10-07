"""Find the repos a token can see (GitHub, GitLab, Bitbucket Cloud, Bitbucket Data Center).

Stdlib only. Every function returns `Target`s; nothing here clones or writes.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

PLATFORMS = ("github", "gitlab", "bitbucket", "bitbucket_dc")
PLATFORM_LABELS = {
    "github": "GitHub",
    "gitlab": "GitLab",
    "bitbucket": "Bitbucket Cloud",
    "bitbucket_dc": "Bitbucket Data Center",
}
GITHUB_API = "https://api.github.com"
BITBUCKET_CLOUD_API = "https://api.bitbucket.org/2.0"
PAGE_SIZE = 100
MAX_PAGES = 500  # 50k repos; guards a server that never reports the last page
HTTP_TIMEOUT_SECONDS = 30
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Target:
    full_name: str
    local_path: Path | None = None
    clone_url: str | None = None
    platform: str = "local"

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "__", self.full_name.strip("/")) or "repo"

    @property
    def key(self) -> str:
        return f"{self.platform}|{self.full_name}"


class DiscoveryError(RuntimeError):
    """Readable, token-free message about why repos could not be listed."""


def normalise_base_url(raw: str) -> str:
    text = raw.strip().rstrip("/")
    if not text:
        raise DiscoveryError("Enter the server URL, for example https://bitbucket.example.com")
    if not re.match(r"^https?://", text):
        text = "https://" + text
    parts = urlsplit(text)
    if not parts.hostname:
        raise DiscoveryError(f"Not a valid URL: {raw!r}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def strip_userinfo(url: str) -> str:
    """Bitbucket DC clone links carry `user@`; auth is added separately."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _ssl_context(insecure: bool) -> ssl.SSLContext | None:
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_get(url: str, headers: dict[str, str], *, insecure: bool = False,
             token: str = "") -> tuple[object, dict[str, str]]:
    """GET + decode JSON, retrying 429/5xx. Returns (json, response headers)."""
    request_headers = {"Accept": "application/json", "User-Agent": "subset", **headers}
    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS,
                                        context=_ssl_context(insecure)) as resp:
                return json.loads(resp.read().decode("utf-8")), {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                retry_after = exc.headers.get("Retry-After", "")
                time.sleep(min(int(retry_after) if retry_after.isdigit() else 2 ** attempt, 20))
                continue
            hint = {
                401: "the token was rejected (expired, wrong platform, or missing scopes)",
                403: "the token is valid but not allowed to list repositories (check its scopes/permissions)",
                404: "the URL or workspace was not found (check the server URL / workspace name)",
            }.get(exc.code, f"HTTP {exc.code}")
            raise DiscoveryError(f"Could not list repos: {hint}.") from None
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ValueError) as exc:
            last = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** attempt)
    reason = getattr(last, "reason", last)
    text = str(reason).replace(token, "***") if token else str(reason)
    if "CERTIFICATE_VERIFY_FAILED" in text:
        text += " (self-signed certificate? tick 'Skip TLS verification' only if you trust this server)"
    raise DiscoveryError(f"Could not reach the server: {text}")


def _github(token: str, insecure: bool) -> list[Target]:
    out: list[Target] = []
    for page in range(1, MAX_PAGES + 1):
        query = urlencode({"per_page": PAGE_SIZE, "page": page, "sort": "full_name",
                           "affiliation": "owner,collaborator,organization_member"})
        data, _ = http_get(f"{GITHUB_API}/user/repos?{query}",
                           {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"},
                           insecure=insecure, token=token)
        if not isinstance(data, list) or not data:
            break
        out += [Target(r["full_name"], clone_url=r["clone_url"], platform="github") for r in data]
        if len(data) < PAGE_SIZE:
            break
    return out


def _gitlab(token: str, host: str, insecure: bool) -> list[Target]:
    base = normalise_base_url(host or "gitlab.com")
    out: list[Target] = []
    for page in range(1, MAX_PAGES + 1):
        query = urlencode({"membership": "true", "simple": "true", "per_page": PAGE_SIZE, "page": page,
                           "order_by": "path", "sort": "asc"})
        data, _ = http_get(f"{base}/api/v4/projects?{query}", {"PRIVATE-TOKEN": token},
                           insecure=insecure, token=token)
        if not isinstance(data, list) or not data:
            break
        out += [Target(r["path_with_namespace"], clone_url=r["http_url_to_repo"], platform="gitlab") for r in data]
        if len(data) < PAGE_SIZE:
            break
    return out


def _bitbucket_cloud(token: str, workspace: str, insecure: bool) -> list[Target]:
    base = f"{BITBUCKET_CLOUD_API}/repositories"
    url = f"{base}/{quote(workspace.strip(), safe='')}" if workspace.strip() else f"{base}?role=member"
    url += ("&" if "?" in url else "?") + f"pagelen={PAGE_SIZE}"
    out: list[Target] = []
    for _ in range(MAX_PAGES):
        data, _ = http_get(url, {"Authorization": f"Bearer {token}"}, insecure=insecure, token=token)
        for r in data.get("values", []):
            https = next((c["href"] for c in r.get("links", {}).get("clone", []) if c.get("name") == "https"), None)
            if https:
                out.append(Target(r["full_name"], clone_url=strip_userinfo(https), platform="bitbucket"))
        url = data.get("next") or ""
        if not url:
            break
    return out


def _bitbucket_dc(token: str, base_url: str, insecure: bool) -> list[Target]:
    base = normalise_base_url(base_url)
    out: list[Target] = []
    start = 0
    for _ in range(MAX_PAGES):
        query = urlencode({"limit": PAGE_SIZE, "start": start})
        data, _ = http_get(f"{base}/rest/api/1.0/repos?{query}", {"Authorization": f"Bearer {token}"},
                           insecure=insecure, token=token)
        for r in data.get("values", []):
            http = next((c["href"] for c in r.get("links", {}).get("clone", []) if c.get("name") in ("http", "https")), None)
            project = (r.get("project") or {}).get("key", "")
            if http and project:
                out.append(Target(f"{project}/{r['slug']}", clone_url=strip_userinfo(http), platform="bitbucket_dc"))
        if data.get("isLastPage", True):
            break
        start = data.get("nextPageStart", start + PAGE_SIZE)
    return out


def discover_remote(platform: str, token: str, *, host: str = "", workspace: str = "",
                    insecure: bool = False) -> list[Target]:
    """List every repo `token` can see on `platform`, sorted by name.

    host: GitLab host (default gitlab.com) or the Bitbucket Data Center base URL (required).
    workspace: optional Bitbucket Cloud workspace; blank lists repos you are a member of.
    """
    if platform not in PLATFORMS:
        raise DiscoveryError(f"Unknown platform {platform!r}.")
    if not token:
        raise DiscoveryError(
            f"No {PLATFORM_LABELS[platform]} token. Paste one, set it in the environment, or add it to the tokens file."
        )
    if platform == "github":
        found = _github(token, insecure)
    elif platform == "gitlab":
        found = _gitlab(token, host, insecure)
    elif platform == "bitbucket":
        found = _bitbucket_cloud(token, workspace, insecure)
    else:
        found = _bitbucket_dc(token, host, insecure)
    unique = {t.full_name: t for t in found}
    return sorted(unique.values(), key=lambda t: t.full_name.lower())
