from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx

from . import register

TIMEOUT = 30.0


def git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{p.stderr.strip()}")
    return p.stdout.strip()


class VcsAdapter:
    """Contract: branch, commit_all, push, open_pr. Never merges — a human does."""

    def __init__(self, cfg):
        self.cfg = cfg

    def branch(self, repo: Path, name: str) -> None:
        git(repo, "checkout", "-B", name)

    def commit_all(self, repo: Path, message: str) -> str:
        git(repo, "add", "-A")
        status = git(repo, "status", "--porcelain")
        if not status:
            raise SystemExit("nothing to commit — no fix was applied")
        subprocess.run(["git", "commit", "-m", message], cwd=repo,
                       capture_output=True, text=True)
        return git(repo, "rev-parse", "HEAD")

    def diff(self, repo: Path, base: str = "main") -> str:
        return git(repo, "diff", f"{base}...HEAD", check=False)

    def push(self, repo: Path, branch: str) -> None:
        raise NotImplementedError

    def open_pr(self, repo: Path, branch: str, title: str, body: str,
                base: str = "main") -> dict[str, Any]:
        raise NotImplementedError

    # --- evidence --------------------------------------------------------
    # A PR that *references* a video on someone else's disk is not evidence.
    # Drivers that can host a file return a URL; the rest fall back to
    # committing the files onto the branch.

    def upload_asset(self, pr: dict[str, Any], path: Path) -> str | None:
        return None

    def update_pr_body(self, pr: dict[str, Any], body: str) -> bool:
        return False

    def commit_assets(self, repo: Path, branch: str,
                      paths: list[Path]) -> dict[str, str]:
        """Fallback: put the files on the branch itself and link them."""
        dest_dir = repo / ".bugforge" / "evidence"
        dest_dir.mkdir(parents=True, exist_ok=True)
        rel: dict[str, str] = {}
        for p in paths:
            target = dest_dir / p.name
            target.write_bytes(Path(p).read_bytes())
            rel[p.stem] = str(target.relative_to(repo))
        git(repo, "add", "-f", str(dest_dir.relative_to(repo)))
        subprocess.run(["git", "commit", "-m", "evidence: before/after recordings"],
                       cwd=repo, capture_output=True, text=True)
        return rel

    def health(self) -> tuple[bool, str]:
        return True, "ok"


@register("vcs", "gitea")
class GiteaVcs(VcsAdapter):
    """cfg: url, repo ("owner/name"), token, base (default main)"""

    def _hdr(self):
        return {"Authorization": f"token {self.cfg.opts.get('token', '')}"}

    def push(self, repo, branch):
        git(repo, "push", "-u", self.cfg.opts.get("remote", "origin"), branch, "--force")

    def open_pr(self, repo, branch, title, body, base=None):
        base = base or self.cfg.opts.get("base", "main")
        owner_repo = self.cfg.opts["repo"]
        r = httpx.post(
            f"{self.cfg.url}/api/v1/repos/{owner_repo}/pulls",
            json={"title": title, "body": body, "head": branch, "base": base},
            headers=self._hdr(), timeout=TIMEOUT,
        )
        if r.status_code >= 400:
            raise SystemExit(f"gitea PR failed ({r.status_code}): {r.text[:500]}")
        d = r.json()
        return {"number": d.get("number"), "url": d.get("html_url"), "driver": "gitea"}

    def upload_asset(self, pr, path):
        """Gitea hosts attachments on the issue backing the PR (same index)."""
        path = Path(path)
        owner_repo = self.cfg.opts["repo"]
        url = f"{self.cfg.url}/api/v1/repos/{owner_repo}/issues/{pr['number']}/assets"
        with path.open("rb") as fh:
            r = httpx.post(url, headers=self._hdr(),
                           files={"attachment": (path.name, fh, "image/gif")},
                           params={"name": path.name}, timeout=120)
        if r.status_code >= 400:
            return None
        return r.json().get("browser_download_url")

    def update_pr_body(self, pr, body):
        owner_repo = self.cfg.opts["repo"]
        r = httpx.patch(f"{self.cfg.url}/api/v1/repos/{owner_repo}/pulls/{pr['number']}",
                        json={"body": body}, headers=self._hdr(), timeout=TIMEOUT)
        return r.status_code < 400

    def health(self):
        try:
            r = httpx.get(f"{self.cfg.url}/api/v1/version", timeout=10)
            return r.status_code < 500, f"http {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


@register("vcs", "github")
class GithubVcs(VcsAdapter):
    def push(self, repo, branch):
        git(repo, "push", "-u", "origin", branch, "--force")

    def open_pr(self, repo, branch, title, body, base=None):
        base = base or self.cfg.opts.get("base", "main")
        p = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body,
             "--base", base, "--head", branch],
            cwd=repo, capture_output=True, text=True,
        )
        if p.returncode != 0:
            raise SystemExit(f"gh pr create failed:\n{p.stderr.strip()}")
        url = p.stdout.strip()
        return {"url": url, "number": url.rstrip("/").split("/")[-1], "driver": "github",
                "_repo": str(repo)}

    # GitHub has no public attachment-upload endpoint, so evidence rides on the
    # branch instead (commit_assets) and the body uses repo-relative links.

    def update_pr_body(self, pr, body):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(body)
            tmp = fh.name
        p = subprocess.run(["gh", "pr", "edit", str(pr["number"]), "--body-file", tmp],
                           cwd=pr.get("_repo"), capture_output=True, text=True)
        return p.returncode == 0


@register("vcs", "local")
class LocalVcs(VcsAdapter):
    """No remote. Commits to a branch and writes the PR body to disk.

    Useful before a git host is wired up, and in locked-down environments.
    """

    def push(self, repo, branch):
        return None

    def open_pr(self, repo, branch, title, body, base=None):
        out = repo / ".bugforge" / f"PR-{branch.replace('/', '-')}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body if body.lstrip().startswith("#") else f"# {title}\n\n{body}\n")
        return {"url": str(out), "driver": "local", "branch": branch, "path": str(out)}

    def upload_asset(self, pr, path):
        """No host — link the file on disk so the local preview still renders."""
        return Path(path).resolve().as_uri()

    def update_pr_body(self, pr, body):
        Path(pr["path"]).write_text(body)
        return True
