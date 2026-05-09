"""Abstract base class for SCM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod


class SCMProvider(ABC):
    """Common interface all SCM providers must implement."""

    # ------------------------------------------------------------------
    # Repository discovery
    # ------------------------------------------------------------------

    @abstractmethod
    def search_repos(self, query: str, limit: int = 10) -> tuple[list[dict], str]:
        """Search / list repositories matching query. Returns (repos, status)."""

    @abstractmethod
    def get_repo(self, owner: str, repo: str) -> tuple[dict, str]:
        """Fetch metadata for one repository. Returns (repo_dict, status)."""

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------

    @abstractmethod
    def list_branches(self, owner: str, repo: str) -> tuple[list[dict], str]:
        """List branches. Returns ([{name, sha, default}, ...], status)."""

    @abstractmethod
    def create_branch(self, owner: str, repo: str, branch: str, from_ref: str) -> tuple[dict, str]:
        """Create a branch from from_ref. Returns (branch_dict, status)."""

    # ------------------------------------------------------------------
    # Pull requests
    # ------------------------------------------------------------------

    @abstractmethod
    def list_prs(self, owner: str, repo: str, state: str = "open") -> tuple[list[dict], str]:
        """List pull requests. Returns ([pr_dict, ...], status)."""

    @abstractmethod
    def get_pr(self, owner: str, repo: str, pr_id: int | str) -> tuple[dict, str]:
        """Get a single PR by number/id. Returns (pr_dict, status)."""

    @abstractmethod
    def create_pr(
        self,
        owner: str,
        repo: str,
        from_branch: str,
        to_branch: str,
        title: str,
        description: str = "",
    ) -> tuple[dict, str]:
        """Create a PR. Returns (pr_dict, status)."""

    @abstractmethod
    def add_pr_comment(
        self,
        owner: str,
        repo: str,
        pr_id: int | str,
        text: str,
        file_path: str = "",
        line: int | None = None,
    ) -> tuple[dict, str]:
        """Add a comment to a PR. Returns (comment_dict, status)."""

    @abstractmethod
    def list_pr_comments(self, owner: str, repo: str, pr_id: int | str) -> tuple[list[dict], str]:
        """List comments on a PR. Returns ([comment_dict, ...], status)."""

    # ------------------------------------------------------------------
    # Git operations
    # ------------------------------------------------------------------

    @abstractmethod
    def get_clone_url(self, owner: str, repo: str) -> str:
        """Return an HTTPS clone URL for the repository."""

    @abstractmethod
    def push_files(
        self,
        owner: str,
        repo: str,
        branch: str,
        base_branch: str,
        files: list[dict],
        commit_message: str,
        files_to_delete: list[str] | None = None,
    ) -> tuple[dict, str]:
        """Push file changes to a branch. Returns (result_dict, status)."""

    # ------------------------------------------------------------------
    # Remote read operations (no local clone required)
    # ------------------------------------------------------------------

    @abstractmethod
    def read_remote_file(
        self, owner: str, repo: str, path: str, ref: str = ""
    ) -> tuple[str, str]:
        """Read a single file from a remote branch/ref via API.

        Returns (content, status).  status is "ok" on success.
        Does NOT require a local clone.
        """

    @abstractmethod
    def list_remote_dir(
        self, owner: str, repo: str, path: str = "", ref: str = ""
    ) -> tuple[list[dict], str]:
        """List the contents of a directory in a remote branch/ref via API.

        Returns ([{name, path, type, size}, ...], status).
        Does NOT require a local clone.
        """

    @abstractmethod
    def search_code(
        self, owner: str, repo: str, query: str, limit: int = 20
    ) -> tuple[list[dict], str]:
        """Search code in a remote repository.

        Returns ([{path, fragmentText, htmlUrl}, ...], status).
        """

    @abstractmethod
    def compare_refs(
        self,
        owner: str,
        repo: str,
        base: str,
        head: str,
        stat_only: bool = False,
    ) -> tuple[dict, str]:
        """Compare two branches or commit SHAs.

        Returns (comparison_dict, status).
        comparison_dict keys: aheadBy, behindBy, totalChangedFiles,
        additions, deletions, files, diff (if not stat_only).
        """

    @abstractmethod
    def get_default_branch(self, owner: str, repo: str) -> tuple[dict, str]:
        """Return the default branch and known protected branch names.

        Returns ({"defaultBranch": str, "protectedBranches": [str, ...]}, status).
        """

    @abstractmethod
    def get_branch_rules(self, owner: str, repo: str) -> tuple[dict, str]:
        """Return branch protection rules for the repository.

        Returns ({"rules": [...], "source": str}, status).
        Rules combine local policy config with provider-reported protection data.
        """

    # ------------------------------------------------------------------
    # Provider identity
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name, e.g. 'github' or 'bitbucket'."""
