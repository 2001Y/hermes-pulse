from __future__ import annotations

import fcntl
import json
import os
import re
import select
import shutil
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from hermes_pulse.models import CitationLink, CollectedItem, IntentSignals, ItemTimestamps, Provenance

BrowserPageReader = Callable[[str, int], dict[str, object]]
DEFAULT_CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

_SIGNAL_PAGES: dict[str, tuple[str, str]] = {
    "likes": ("x_likes", "https://x.com/{handle}/likes"),
    "home_timeline_reverse_chronological": ("x_home_timeline_reverse_chronological", "https://x.com/home"),
}


class XBrowserConnector:
    id = "x_signals"
    source_family = "x"

    def __init__(
        self,
        *,
        profile_root: str | Path,
        profile_directory: str,
        expected_handle: str,
        limit: int = 20,
        chrome_path: str | Path = DEFAULT_CHROME_PATH,
        page_reader: BrowserPageReader | None = None,
    ) -> None:
        if not 1 <= limit <= 50:
            raise ValueError("X browser signal limit must be between 1 and 50")
        self._profile_root = Path(profile_root)
        self._profile_directory = profile_directory
        self._expected_handle = expected_handle.lstrip("@")
        self._limit = limit
        self._chrome_path = Path(chrome_path)
        self._page_reader = page_reader or self._read_page

    def collect(self, signal_types: Sequence[str]) -> list[CollectedItem]:
        unsupported = [signal_type for signal_type in signal_types if signal_type not in _SIGNAL_PAGES]
        if unsupported:
            raise ValueError(f"Unsupported X browser signal type: {unsupported[0]}")
        if not signal_types:
            return []

        items: list[CollectedItem] = []
        for signal_type in signal_types:
            source, url_template = _SIGNAL_PAGES[signal_type]
            page_url = url_template.format(handle=self._expected_handle)
            snapshot = self._page_reader(page_url, self._limit)
            self._verify_identity(snapshot)
            items.extend(_items_from_snapshot(source, signal_type, snapshot, limit=self._limit))
        return items

    def _read_page(self, url: str, limit: int) -> dict[str, object]:
        return _read_authenticated_x_page(
            profile_root=self._profile_root,
            profile_directory=self._profile_directory,
            expected_handle=self._expected_handle,
            url=url,
            limit=limit,
            chrome_path=self._chrome_path,
        )

    def _verify_identity(self, snapshot: dict[str, object]) -> None:
        active_handle = snapshot.get("active_handle")
        if not isinstance(active_handle, str) or active_handle.lstrip("@").casefold() != self._expected_handle.casefold():
            raise RuntimeError(
                f"X browser identity mismatch: expected @{self._expected_handle}, "
                f"got @{active_handle.lstrip('@') if isinstance(active_handle, str) else 'unknown'}"
            )


def _items_from_snapshot(
    source: str,
    signal_type: str,
    snapshot: dict[str, object],
    *,
    limit: int,
) -> list[CollectedItem]:
    raw_posts = snapshot.get("posts")
    if not isinstance(raw_posts, list):
        raise ValueError("X browser snapshot posts must be a list")

    items: list[CollectedItem] = []
    seen_ids: set[str] = set()
    for raw_post in raw_posts:
        if len(items) >= limit:
            break
        if not isinstance(raw_post, dict):
            continue
        raw_tweet_id = raw_post.get("id")
        raw_username = raw_post.get("username")
        text = raw_post.get("text")
        raw_tweet_url = raw_post.get("tweet_url")
        if not isinstance(raw_tweet_id, str) or not raw_tweet_id:
            continue
        if not isinstance(raw_username, str) or not raw_username:
            continue
        if not isinstance(raw_tweet_url, str) or not raw_tweet_url:
            continue
        tweet_id = raw_tweet_id
        username = raw_username
        tweet_url = raw_tweet_url
        if tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)
        body = text if isinstance(text, str) else ""
        external_url = raw_post.get("external_url")
        target_url = external_url if isinstance(external_url, str) and external_url.startswith("http") else tweet_url
        title = _title_from_text(body, username=username)
        created_at = raw_post.get("created_at")
        items.append(
            CollectedItem(
                id=f"{source}:{tweet_id}",
                source=source,
                source_kind="post",
                title=title,
                excerpt=body,
                body=body,
                url=target_url,
                timestamps=ItemTimestamps(created_at=created_at if isinstance(created_at, str) else None),
                intent_signals=IntentSignals(liked=signal_type == "likes"),
                provenance=Provenance(
                    provider="x.com",
                    acquisition_mode="browser_automation_experimental",
                    authority_tier="primary",
                    primary_source_url=target_url,
                    raw_record_id=tweet_id,
                ),
                citation_chain=[CitationLink(label=title, url=target_url, relation="primary")],
                metadata={
                    "x_signal": signal_type,
                    "author_username": username,
                    "tweet_url": tweet_url,
                    "target_url": target_url,
                },
            )
        )
    return items


def _title_from_text(text: str, *, username: str) -> str:
    compact = " ".join(text.split())
    return compact[:80] if compact else f"X post by @{username}"


def refresh_x_browser_profile(
    *,
    source_user_data_dir: str | Path,
    source_profile_directory: str,
    destination_user_data_dir: str | Path,
    destination_profile_directory: str,
) -> Path:
    source_root = Path(source_user_data_dir)
    source_profile = source_root / source_profile_directory
    destination_root = Path(destination_user_data_dir)
    destination_profile = destination_root / destination_profile_directory
    destination_profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination_root, 0o700)
    os.chmod(destination_profile, 0o700)

    metadata_files = (
        (source_root / "Local State", destination_root / "Local State"),
        (source_profile / "Preferences", destination_profile / "Preferences"),
        (source_profile / "Secure Preferences", destination_profile / "Secure Preferences"),
    )
    for source_path, destination_path in metadata_files:
        if not source_path.is_file():
            raise FileNotFoundError(f"Required Chrome profile file is missing: {source_path}")
        shutil.copy2(source_path, destination_path)
        os.chmod(destination_path, 0o600)

    source_cookie_path = source_profile / "Cookies"
    if not source_cookie_path.is_file():
        raise FileNotFoundError(f"Required Chrome cookie database is missing: {source_cookie_path}")
    destination_cookie_path = destination_profile / "Cookies"
    temporary_cookie_path = destination_profile / ".Cookies.tmp"
    temporary_cookie_path.unlink(missing_ok=True)
    source_uri = source_cookie_path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source_connection:
            with sqlite3.connect(temporary_cookie_path) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.execute(
                    "DELETE FROM cookies "
                    "WHERE NOT (host_key = 'x.com' OR host_key LIKE '%.x.com' "
                    "OR host_key = 'twitter.com' OR host_key LIKE '%.twitter.com')"
                )
                destination_connection.commit()
        os.chmod(temporary_cookie_path, 0o600)
        os.replace(temporary_cookie_path, destination_cookie_path)
    finally:
        temporary_cookie_path.unlink(missing_ok=True)
    return destination_profile


def _read_authenticated_x_page(
    *,
    profile_root: Path,
    profile_directory: str,
    expected_handle: str,
    url: str,
    limit: int,
    chrome_path: Path,
) -> dict[str, object]:
    normalized_handle = expected_handle.lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{1,15}", normalized_handle) is None:
        raise ValueError("Invalid expected X handle")
    allowed_urls = {f"https://x.com/{normalized_handle}/likes", "https://x.com/home"}
    if url not in allowed_urls:
        raise ValueError(f"Unsupported X browser page: {url}")
    if not chrome_path.is_file():
        raise FileNotFoundError(f"Chrome executable is missing: {chrome_path}")
    if not (profile_root / profile_directory / "Cookies").is_file():
        raise FileNotFoundError(f"X browser profile is not initialized: {profile_root / profile_directory}")

    lock_path = profile_root / ".pulse-x-browser.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+b") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            with _ChromeCdpPipe(
                chrome_path=chrome_path,
                profile_root=profile_root,
                profile_directory=profile_directory,
            ) as browser:
                browser.navigate(url)
                _wait_for_authenticated_identity(browser, expected_handle=normalized_handle)
                if url == "https://x.com/home":
                    _select_following_timeline(browser)
                posts: list[dict[str, object]] = []
                seen_ids: set[str] = set()
                stable_rounds = 0
                previous_count = -1
                for _ in range(8):
                    snapshot = browser.evaluate_json(_x_dom_snapshot_expression(normalized_handle))
                    active_handle = snapshot.get("active_handle")
                    if not isinstance(active_handle, str) or active_handle.casefold() != normalized_handle.casefold():
                        raise RuntimeError(
                            f"X browser identity mismatch: expected @{normalized_handle}, "
                            f"got @{active_handle if isinstance(active_handle, str) else 'unknown'}"
                        )
                    raw_posts = snapshot.get("posts")
                    if isinstance(raw_posts, list):
                        for raw_post in raw_posts:
                            if not isinstance(raw_post, dict):
                                continue
                            tweet_id = raw_post.get("id")
                            if not isinstance(tweet_id, str) or not tweet_id or tweet_id in seen_ids:
                                continue
                            seen_ids.add(tweet_id)
                            posts.append(raw_post)
                            if len(posts) >= limit:
                                break
                    if len(posts) >= limit:
                        break
                    if len(posts) == previous_count:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                    if stable_rounds >= 2 and posts:
                        break
                    previous_count = len(posts)
                    browser.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 1.5, 1600)); true")
                    time.sleep(1.0)
                if not posts:
                    raise RuntimeError(f"No X posts loaded from authenticated page: {url}")
                return {"active_handle": normalized_handle, "posts": posts[:limit]}
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


_FD_SETUP_LOCK = threading.Lock()


class _ChromeCdpPipe:
    def __init__(self, *, chrome_path: Path, profile_root: Path, profile_directory: str) -> None:
        self._chrome_path = chrome_path
        self._profile_root = profile_root
        self._profile_directory = profile_directory
        self._process: subprocess.Popen[bytes] | None = None
        self._write_fd: int | None = None
        self._read_fd: int | None = None
        self._buffer = bytearray()
        self._next_id = 0
        self._session_id: str | None = None

    def __enter__(self) -> _ChromeCdpPipe:
        self._process, self._write_fd, self._read_fd = _spawn_chrome_cdp_pipe(
            chrome_path=self._chrome_path,
            profile_root=self._profile_root,
            profile_directory=self._profile_directory,
        )
        target_id = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self._session_id = self.call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )["sessionId"]
        self.call("Page.enable", session_id=self._session_id)
        self.call("Runtime.enable", session_id=self._session_id)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        write_fd, read_fd, process = self._write_fd, self._read_fd, self._process
        self._write_fd = None
        self._read_fd = None
        self._process = None
        for fd in (write_fd, read_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def navigate(self, url: str) -> None:
        assert self._session_id is not None
        self.call("Page.navigate", {"url": url}, session_id=self._session_id)

    def evaluate(self, expression: str) -> object:
        assert self._session_id is not None
        payload = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            session_id=self._session_id,
        )
        result = payload.get("result") or {}
        return result.get("value")

    def evaluate_json(self, expression: str) -> dict[str, object]:
        value = self.evaluate(expression)
        if not isinstance(value, str):
            raise RuntimeError("Chrome did not return serialized X page data")
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise RuntimeError("Chrome returned invalid X page data")
        return payload

    def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        if self._write_fd is None or self._read_fd is None:
            raise RuntimeError("Chrome CDP pipe is not open")
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, object] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        if session_id is not None:
            message["sessionId"] = session_id
        _write_all(self._write_fd, json.dumps(message, separators=(",", ":")).encode() + b"\0")
        deadline = time.monotonic() + timeout
        while True:
            response = self._read_message(deadline)
            if response.get("id") != request_id:
                continue
            error = response.get("error")
            if isinstance(error, dict):
                raise RuntimeError(f"Chrome CDP {method} failed: {error.get('message', 'unknown error')}")
            result = response.get("result")
            return result if isinstance(result, dict) else {}

    def _read_message(self, deadline: float) -> dict[str, Any]:
        assert self._read_fd is not None
        while b"\0" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for Chrome CDP response")
            readable, _, _ = select.select([self._read_fd], [], [], remaining)
            if not readable:
                raise TimeoutError("Timed out waiting for Chrome CDP response")
            chunk = os.read(self._read_fd, 65536)
            if not chunk:
                exit_code = None if self._process is None else self._process.poll()
                raise RuntimeError(f"Chrome CDP pipe closed unexpectedly (exit={exit_code})")
            self._buffer.extend(chunk)
        raw, remainder = self._buffer.split(b"\0", 1)
        self._buffer = bytearray(remainder)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("Chrome CDP returned a non-object message")
        return payload


def _spawn_chrome_cdp_pipe(
    *,
    chrome_path: Path,
    profile_root: Path,
    profile_directory: str,
) -> tuple[subprocess.Popen[bytes], int, int]:
    with _FD_SETUP_LOCK:
        originally_open = {fd: _fd_is_open(fd) for fd in (3, 4)}
        for fd in (3, 4):
            if originally_open[fd]:
                continue
            while not _fd_is_open(fd):
                os.open(os.devnull, os.O_RDWR)
        saved_fds = {fd: os.dup(fd) for fd in (3, 4) if originally_open[fd]}
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        process: subprocess.Popen[bytes] | None = None
        try:
            os.dup2(input_read, 3, inheritable=True)
            os.dup2(output_write, 4, inheritable=True)
            process = subprocess.Popen(
                [
                    str(chrome_path),
                    "--headless=new",
                    "--disable-translate",
                    "--disable-features=Translate,TranslateUI",
                    "--remote-debugging-pipe",
                    f"--user-data-dir={profile_root}",
                    f"--profile-directory={profile_directory}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1440,2400",
                    "about:blank",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(3, 4),
            )
        finally:
            for fd in (3, 4):
                if originally_open[fd]:
                    os.dup2(saved_fds[fd], fd)
                    os.close(saved_fds[fd])
                else:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            os.close(input_read)
            os.close(output_write)
        if process is None:
            os.close(input_write)
            os.close(output_read)
            raise RuntimeError("Chrome failed to start")
        return process, input_write, output_read


def _fd_is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _wait_for_authenticated_identity(browser: _ChromeCdpPipe, *, expected_handle: str) -> None:
    expression = _x_identity_expression()
    deadline = time.monotonic() + 30.0
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = browser.evaluate_json(expression)
        active_handle = last.get("active_handle")
        if isinstance(active_handle, str):
            if active_handle.casefold() != expected_handle.casefold():
                raise RuntimeError(
                    f"X browser identity mismatch: expected @{expected_handle}, got @{active_handle}"
                )
            return
        if last.get("login_required") is True:
            raise RuntimeError("X browser profile requires login")
        time.sleep(0.5)
    raise RuntimeError(f"Unable to verify X browser identity for @{expected_handle}: {last.get('url', 'unknown URL')}")


def _select_following_timeline(browser: _ChromeCdpPipe) -> None:
    click_result = browser.evaluate_json(
        """JSON.stringify((() => {
          const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
          const tab = tabs.find((node) => ['Following', 'フォロー中'].includes((node.innerText || '').trim()));
          if (!tab) return {found: false, selected: false};
          const selected = tab.getAttribute('aria-selected') === 'true';
          if (!selected) tab.click();
          return {found: true, selected};
        })())"""
    )
    if click_result.get("found") is not True:
        raise RuntimeError("Unable to find the X Following timeline tab")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        selected = browser.evaluate_json(
            """JSON.stringify((() => {
              const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
              const tab = tabs.find((node) => ['Following', 'フォロー中'].includes((node.innerText || '').trim()));
              return {selected: !!tab && tab.getAttribute('aria-selected') === 'true'};
            })())"""
        )
        if selected.get("selected") is True:
            browser.evaluate("window.scrollTo(0, 0); true")
            time.sleep(1.0)
            return
        time.sleep(0.5)
    raise RuntimeError("X Following timeline did not become active")


def _x_identity_expression() -> str:
    return """JSON.stringify((() => {
      const profileLink = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
      const href = profileLink ? profileLink.getAttribute('href') || '' : '';
      const match = href.match(/^\\/([A-Za-z0-9_]{1,15})$/);
      return {
        active_handle: match ? match[1] : null,
        login_required: location.pathname.startsWith('/i/flow/login') || location.pathname === '/login',
        url: location.href,
        ready: document.readyState,
      };
    })())"""


def _x_dom_snapshot_expression(expected_handle: str) -> str:
    expected_json = json.dumps(expected_handle)
    return f"""JSON.stringify((() => {{
      const profileLink = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
      const profileHref = profileLink ? profileLink.getAttribute('href') || '' : '';
      const profileMatch = profileHref.match(/^\\/([A-Za-z0-9_]{{1,15}})$/);
      const posts = [];
      const seen = new Set();
      for (const article of Array.from(document.querySelectorAll('article'))) {{
        const articleText = article.innerText || '';
        if (/(^|\\n)(Ad|Promoted|広告)(\\n|$)/.test(articleText)) continue;
        const timeNode = article.querySelector('time');
        const statusLink = timeNode ? timeNode.closest('a[href*="/status/"]') : null;
        const href = statusLink ? statusLink.getAttribute('href') || '' : '';
        const match = href.match(/^\\/([^/]+)\\/status\\/(\\d+)/);
        if (!match || seen.has(match[2])) continue;
        seen.add(match[2]);
        const tweetText = article.querySelector('[data-testid="tweetText"]');
        const text = tweetText ? (tweetText.innerText || '').trim() : '';
        let externalUrl = null;
        if (tweetText) {{
          for (const anchor of Array.from(tweetText.querySelectorAll('a[href]'))) {{
            const candidate = anchor.href || '';
            if (!candidate.startsWith('http')) continue;
            try {{
              const host = new URL(candidate).hostname.toLowerCase();
              if (host === 'x.com' || host.endsWith('.x.com') || host === 'twitter.com' || host.endsWith('.twitter.com')) continue;
              externalUrl = candidate;
              break;
            }} catch (_) {{}}
          }}
        }}
        posts.push({{
          id: match[2],
          username: match[1],
          text,
          created_at: timeNode ? timeNode.getAttribute('datetime') : null,
          tweet_url: `https://x.com/${{match[1]}}/status/${{match[2]}}`,
          external_url: externalUrl,
        }});
      }}
      return {{
        expected_handle: {expected_json},
        active_handle: profileMatch ? profileMatch[1] : null,
        posts,
      }};
    }})())"""
