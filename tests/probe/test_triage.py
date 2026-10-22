"""Offline tests for the Triager — clustering, sanitization, and prefilled-URL
issue construction. No network, no auto-submit.
"""

from __future__ import annotations

import subprocess
import urllib.parse

import pytest

from . import triage as T
from .report import Report, SpecOutcome
from .spec import JudgeResult


def _failing_report() -> Report:
    return Report(outcomes=[
        SpecOutcome(name="tts", feature="tts-synthesis", layer="media", results=[
            JudgeResult("asr_wer_below", False, "WER=0.4"),
            JudgeResult("asr_wer_below", False, "WER=0.5"),  # same cluster → count 2
            JudgeResult("not_silent", True),                 # pass: ignored
            JudgeResult("not_clipping", False, advisory=True),  # advisory: ignored
        ]),
        SpecOutcome(name="first-run", feature="first-run", layer="env", results=[
            JudgeResult("status_eq", False, "HTTP 500"),
        ]),
    ])


def test_sanitize_strips_home_and_secrets():
    assert T.sanitize("/home/alice/x.wav") == "~/x.wav"
    assert T.sanitize("/Users/bob/y") == "~/y"
    assert "[REDACTED]" in T.sanitize("token hf_abcdef123456 leaked")


def _checkout(tmp_path, origin):
    """Build a local Git checkout without depending on this clone's remotes."""
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    if origin is not None:
        subprocess.run(
            ["git", "remote", "add", "origin", origin], cwd=tmp_path, check=True,
        )
    return str(tmp_path)


@pytest.mark.parametrize("origin, expected", [
    ("https://github.com/example-owner/example-repo.git", ("example-owner", "example-repo")),
    ("https://github.com/another-owner/another-repo/", ("another-owner", "another-repo")),
    ("git@github.com:fork-owner/fork-repo.git", ("fork-owner", "fork-repo")),
    ("ssh://git@github.com/ssh-owner/ssh-repo.git", ("ssh-owner", "ssh-repo")),
    ("https://gitlab.com/example-owner/example-repo.git", None),
    (None, None),
])
def test_detect_repo_from_origin(tmp_path, origin, expected):
    assert T.detect_repo(_checkout(tmp_path, origin)) == expected


def test_clustering_dedupes_and_excludes_nonblocking():
    clusters = T.cluster_failures(_failing_report())
    sigs = {c.signature: c.count for c in clusters}
    assert sigs == {"media:tts-synthesis:asr_wer_below": 2, "env:first-run:status_eq": 1}
    # advisory/pass never appear
    assert all("not_silent" not in s and "not_clipping" not in s for s in sigs)


def test_build_issue_title_and_table():
    clusters = T.cluster_failures(_failing_report())
    title, body = T.build_issue(clusters)
    assert title == "probe: 3 failing checks across 2 features"
    assert "| Layer | Feature | Check | Count | Detail |" in body
    assert "`asr_wer_below`" in body and "| 2 |" in body


@pytest.mark.parametrize("origin, owner, repo", [
    ("https://github.com/example-owner/example-repo.git", "example-owner", "example-repo"),
    ("git@github.com:fork-owner/fork-repo.git", "fork-owner", "fork-repo"),
])
def test_triage_builds_github_url(tmp_path, origin, owner, repo):
    res = T.triage(_failing_report(), cwd=_checkout(tmp_path, origin))
    assert res.owner == owner and res.repo == repo
    assert res.url and res.url.startswith(
        f"https://github.com/{owner}/{repo}/issues/new?"
    )
    q = urllib.parse.parse_qs(urllib.parse.urlparse(res.url).query)
    assert q["title"][0] == res.title
    assert q["labels"][0] == "probe,bug"


def test_triage_no_url_when_all_pass():
    ok = Report(outcomes=[SpecOutcome(name="x", feature="x", layer="media",
                                      results=[JudgeResult("a", True)])])
    res = T.triage(ok)
    assert res.clusters == [] and res.url is None


def test_report_renders_issue_button_on_failure():
    from . import report as R

    rep = _failing_report()
    rep.issue_url = "https://github.com/debpalash/OmniVoice-Studio/issues/new?title=x"
    html = R.render_html(rep)
    assert "Draft GitHub issue" in html
    assert rep.issue_url in html
    # No button when the run is green
    ok = Report(outcomes=[SpecOutcome(name="x", results=[JudgeResult("a", True)])],
                issue_url="https://example.com")
    assert "Draft GitHub issue" not in R.render_html(ok)
