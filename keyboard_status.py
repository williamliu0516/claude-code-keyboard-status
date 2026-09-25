#!/usr/bin/env python3
"""Claude Code status on a mechanical keyboard's 142x428 image display.

Renders the live state of your Claude Code sessions -- project, branch, model,
effort, 5-hour and weekly usage, and what Claude is currently doing -- as a
142x428 baseline JPEG, and POSTs it to a keyboard that accepts image uploads.

    python3 keyboard_status.py --install     set up venv, launchd agent, hooks
    python3 keyboard_status.py --daemon      run the push loop in the foreground
    python3 keyboard_status.py --once        render and push exactly one frame
    python3 keyboard_status.py --preview x.png   render to a file, push nothing

Why a daemon and hooks, rather than either alone
------------------------------------------------
Neither half can do the job by itself.

A hook-only design goes stale the moment you stop typing. Two of the five things
on screen -- the 5-hour and weekly reset countdowns -- change on wall-clock time,
not on anything a session does, so a display that only redraws on session events
freezes at whatever it last saw and quietly lies for hours.

A daemon-only design cannot see *state*. Whether Claude is thinking, waiting for
you to approve a tool call, or done, is not written to any file the daemon can
poll; it is delivered to hook commands and nowhere else. A poller can infer
liveness from transcript mtimes, but it cannot tell "waiting for permission"
from "still working" -- and that distinction is the single most useful thing a
glanceable display can tell you, because it is the one that wants your attention.

So the work is split along the grain of the data:

  * Five low-frequency hooks (SessionStart, UserPromptSubmit, Notification, Stop,
    SessionEnd) write one small JSON file and exit. They import nothing but the
    standard library, never touch the network, and always exit 0 -- a hook that
    can fail is a hook that can wedge a session.
  * The daemon owns everything expensive: Pillow, the usage poll, the JPEG, the
    POST. It ticks every few seconds, so wall-clock facts stay honest, and it
    reads the hooks' file, so event facts stay precise.

Everything the hooks provide is *optional*. Without them the daemon falls back to
reading session transcripts under ~/.claude/projects, which carry cwd, branch,
model, effort and an AI-written session title. That fallback cannot distinguish a
permission prompt from ongoing work, which is exactly the gap the hooks close.

Usage numbers
-------------
The 5h and weekly figures are resolved with the merge rule from its sibling
project, claude-status-bar, and through the same shared cache file, so the
keyboard and the terminal status line always agree. See `merge` for the rule and
README.md for why it needs to exist at all.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------- paths

CLAUDE_DIR = os.path.expanduser("~/.claude")
CONFIG_PATH = os.path.join(CLAUDE_DIR, "keyboard-status.json")
STATE_PATH = os.path.join(CLAUDE_DIR, "keyboard-status-state.json")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
SETTINGS_PATH = os.path.join(CLAUDE_DIR, "settings.json")
CREDENTIALS_PATH = os.path.join(CLAUDE_DIR, ".credentials.json")
GLOBAL_CONFIG_PATH = os.path.expanduser("~/.claude.json")

# Shared with claude-status-bar on purpose: same schema, same lock, same merge
# rule, so whichever of the two polls first spares the other a request.
USAGE_CACHE_PATH = os.path.join(CLAUDE_DIR, "statusline-usage.json")
USAGE_LOCK_PATH = USAGE_CACHE_PATH + ".lock"

INSTALL_PATH = os.path.join(CLAUDE_DIR, "keyboard-status.py")
VENV_PATH = os.path.join(CLAUDE_DIR, "keyboard-status-venv")
LOG_PATH = os.path.join(CLAUDE_DIR, "keyboard-status.log")
LAUNCH_LABEL = "com.williamliu.claude-keyboard-status"
LAUNCH_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCH_LABEL}.plist")

# ------------------------------------------------------------------------- defaults

# The panel's address has no sensible default: it is whatever DHCP handed your
# keyboard on your network, and a wrong guess fails as a silent no-op (frames
# POSTed into the void, an empty screen, nothing in the log that says why). So
# ship a placeholder that cannot be mistaken for an address and refuse to push
# until it is replaced -- see require_url.
URL_UNSET = "http://PANEL-IP-NOT-SET/image/upload"

DEFAULTS = {
    # Where the keyboard lives. Set it in ~/.claude/keyboard-status.json or
    # with CLAUDE_KEYBOARD_URL; the whole point is that this is not compiled in.
    "url": URL_UNSET,
    "width": 142,
    "height": 428,
    # Seconds between renders. The render is ~20 ms, so this is cheap; it sets
    # how fast the display reacts to a state change.
    "tick_seconds": 5.0,
    # Push even when the frame is byte-identical, this often. Covers a keyboard
    # that rebooted, was unplugged, or dropped the image on its own.
    "heartbeat_seconds": 300.0,
    # After a failed POST, wait this long before the next attempt, growing to a
    # minute. A keyboard that is off should cost one timeout a minute, not one a
    # tick -- but the ceiling stays low, because these panels sleep and wake on
    # their own and the display should catch the next window, not the one after.
    "offline_backoff_seconds": 20.0,
    "http_timeout_seconds": 4.0,
    # 95 rather than 88 because it is free here: the frame goes from 17 KB to
    # 26 KB against a 512 KB ceiling, and the worst-case error on a rendered
    # edge drops from 40/255 to 22/255. Measured with --testcard, not guessed.
    "jpeg_quality": 95,
    # A session whose transcript last moved within this many seconds counts as
    # actively working when no hook has said otherwise.
    "active_seconds": 45.0,
    # Sessions quieter than this stop being "the current session" at all.
    "session_ttl_seconds": 6 * 3600.0,
    "usage_poll_seconds": 60.0,
}

ENV_OVERRIDES = {
    "CLAUDE_KEYBOARD_URL": ("url", str),
    "CLAUDE_KEYBOARD_TICK": ("tick_seconds", float),
    "CLAUDE_KEYBOARD_HEARTBEAT": ("heartbeat_seconds", float),
    "CLAUDE_KEYBOARD_TIMEOUT": ("http_timeout_seconds", float),
    "CLAUDE_KEYBOARD_QUALITY": ("jpeg_quality", int),
}


def read_json(path, default=None):
    """Parse a JSON object from `path`, or return `default` for anything else.

    Every file this program reads is written by something else -- Claude Code,
    another session, a hand edit -- so "missing", "half-written" and "not even an
    object" are all normal, and none of them may take the display down.
    """
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {} if default is None else default
    if not isinstance(data, dict):
        return {} if default is None else default
    return data


def write_json_atomic(path, data):
    """Replace `path` in one step, so no reader ever sees a partial file."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def load_config():
    """Defaults, then ~/.claude/keyboard-status.json, then the environment."""
    config = dict(DEFAULTS)
    for key, value in read_json(CONFIG_PATH).items():
        if key in config and isinstance(value, type(config[key])):
            config[key] = value
        elif key in config and isinstance(value, (int, float)) and isinstance(config[key], float):
            config[key] = float(value)
    for name, (key, cast) in ENV_OVERRIDES.items():
        raw = os.environ.get(name)
        if raw:
            try:
                config[key] = cast(raw)
            except ValueError:
                pass
    return config


# --------------------------------------------------------------------------- usage
#
# Ported from claude-status-bar, deliberately unchanged. Both programs read and
# write ~/.claude/statusline-usage.json, so the merge rule and the tolerance have
# to match exactly or the two would fight over the same keys. See that project's
# README for the full argument; the short version is that the `rate_limits` block
# Claude Code exposes is a per-process in-memory cache with no refresh timer, so a
# single source is routinely hours stale, and correctness comes from merging
# several sources newest-wins rather than from trusting any one of them.

WINDOWS = ("five_hour", "seven_day")
WINDOW_TOLERANCE = 120.0
MAX_WINDOW = 8 * 86400
LOCK_STALE = 120.0
FAILURE_BACKOFF = 120.0
THROTTLED_BACKOFF = 300.0
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
KEYCHAIN_SERVICE = "Claude Code-credentials"
TOKEN_SKEW = 60.0


def credential_stores():
    """Every place Claude Code may keep its OAuth credentials, newest last.

    ~/.claude/.credentials.json is only one of two stores. On macOS the app
    keeps the live credentials in the login Keychain and leaves the file at
    whatever it last wrote -- here that was a token issued days earlier, long
    expired, while the Keychain held a valid one. Reading the file alone is the
    bug that silently emptied the usage meters, so read both and let
    `oauth_token` choose.
    """
    yield sub_dict(read_json(CREDENTIALS_PATH), "claudeAiOauth")
    if sys.platform != "darwin":
        return
    import subprocess
    try:
        # -w prints the secret alone. Short timeout: this runs on the daemon's
        # poll thread, and a Keychain that wants to prompt must not wedge it.
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return
    if proc.returncode != 0:
        return
    try:
        yield sub_dict(json.loads(proc.stdout), "claudeAiOauth")
    except ValueError:
        return


def oauth_token(now):
    """The freshest unexpired Claude Code access token, or None.

    Expiry is checked here rather than left to the server because an expired
    token does not fail usefully: the endpoint answers 401, `poll_usage` treats
    every failure alike and retries two minutes later, and after days of that
    the edge starts returning 429 with a ~50 minute Retry-After. The meters go
    blank and the log stays silent. Prefer a store whose token still has
    TOKEN_SKEW seconds left; fall back to the longest-lived one we saw, so a
    store that simply omits `expiresAt` is still tried.
    """
    best = None
    for store in credential_stores():
        token = store.get("accessToken")
        if not isinstance(token, str) or not token:
            continue
        expires = store.get("expiresAt")
        # Milliseconds since the epoch, as Claude Code writes it.
        expires = expires / 1000.0 if isinstance(expires, (int, float)) else 0.0
        if expires and expires <= now + TOKEN_SKEW:
            continue  # expired or about to be: not worth a 401
        if best is None or expires > best[0]:
            best = (expires, token)
    return best[1] if best else None


def normalize(entry):
    """Reduce any source's shape to {used_percentage, resets_at}, or None.

    Header-derived payloads carry `used_percentage` with epoch seconds; the usage
    endpoint and ~/.claude.json carry `utilization` with an ISO 8601 string.
    """
    if not isinstance(entry, dict):
        return None
    pct = entry.get("used_percentage", entry.get("utilization"))
    resets = entry.get("resets_at")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    if isinstance(resets, str):
        try:
            # `fromisoformat` only learned to accept a trailing Z in 3.11.
            parsed = datetime.fromisoformat(resets.replace("Z", "+00:00"))
        except ValueError:
            return None
        # A naive stamp is UTC. Left naive, .timestamp() reads it as local time
        # and shifts every countdown by the machine's offset.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        resets = parsed.timestamp()
    if not isinstance(resets, (int, float)) or isinstance(resets, bool):
        return None
    return {"used_percentage": float(pct), "resets_at": float(resets)}


def plausible(entry, now):
    """Is this observation describing a window that is currently running?

    The upper bound matters: `merge` prefers the latest boundary, so one absurd
    future timestamp would win forever and freeze the countdown at nonsense.
    """
    return entry is not None and now < entry["resets_at"] <= now + MAX_WINDOW


def merge(entries, now):
    """Best current estimate for one window, from observations of unknown age.

    Boundaries are compared with a tolerance because each response recomputes
    `resets_at` at its own sub-second precision, so the same window arrives as
    ...800.997, ...800.225 and ...800.027. Exact grouping would read that drift
    as three different windows and discard all but one, freezing the number.
    Within a window the largest reading wins, since usage only grows until reset.
    """
    live = [e for e in (normalize(x) for x in entries) if plausible(e, now)]
    if not live:
        return None
    newest = max(e["resets_at"] for e in live)
    current = [e for e in live if newest - e["resets_at"] <= WINDOW_TOLERANCE]
    return {
        "used_percentage": max(e["used_percentage"] for e in current),
        "resets_at": newest,
    }


def blend_into_cache(cache, contributions, now):
    """Fold observations into the cache, preserving its non-window bookkeeping."""
    merged = dict(cache)
    for name in WINDOWS:
        best = merge([cache.get(name)] + [c.get(name) for c in contributions], now)
        if best is not None:
            merged[name] = best
        else:
            merged.pop(name, None)
    return merged


def sub_dict(mapping, key):
    """`mapping[key]` when it is a dict, else {}."""
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def poll_usage(now, timeout=8.0):
    """Refresh the shared usage cache from the OAuth usage endpoint.

    Returns quietly on every failure. The lock is the same one claude-status-bar
    takes, so the two programs never poll at once and never earn each other a 429.
    """
    import urllib.error
    import urllib.request

    try:
        handle = os.open(USAGE_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(handle)
    except FileExistsError:
        try:
            if now - os.stat(USAGE_LOCK_PATH).st_mtime < LOCK_STALE:
                return  # someone else is already polling
        except OSError:
            return
    except OSError:
        return

    try:
        # Re-check under the lock: a poll that became due while another process
        # held it would otherwise fire the instant the lock is released.
        cache = read_json(USAGE_CACHE_PATH)
        if now < cache.get("retry_after", 0):
            return
        token = oauth_token(now)
        if not token:
            return
        request = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": OAUTH_BETA,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            fresh = json.load(response)
        cache = blend_into_cache(read_json(USAGE_CACHE_PATH), [fresh], now)
        cache["polled_at"] = now
        cache.pop("retry_after", None)
        write_json_atomic(USAGE_CACHE_PATH, cache)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        # Expired token, offline, throttled or a bad body: stay quiet and serve
        # the cache. A 429 backs off harder, but not so hard that the figures go
        # visibly stale -- Claude Code keeps refreshing ~/.claude.json regardless.
        status = getattr(error, "code", None)
        backoff = THROTTLED_BACKOFF if status == 429 else FAILURE_BACKOFF
        if status == 429:
            headers = getattr(error, "headers", None)
            retry_after = headers.get("retry-after") if headers else None
            if retry_after and retry_after.strip().isdigit():
                # A floor, never a replacement: this endpoint answers
                # `Retry-After: 0`, which taken literally cancels the backoff.
                backoff = max(backoff, float(retry_after.strip()))
        cache = read_json(USAGE_CACHE_PATH)
        cache["polled_at"] = now
        cache["retry_after"] = now + backoff
        write_json_atomic(USAGE_CACHE_PATH, cache)
    finally:
        try:
            os.unlink(USAGE_LOCK_PATH)
        except OSError:
            pass


def current_usage(now, config, allow_poll=True, blocking=False):
    """{'five_hour': ..., 'seven_day': ...} merged across every source we have.

    Sources, newest-wins: the shared cache (which claude-status-bar also feeds),
    `cachedUsageUtilization` in ~/.claude.json, and our own throttled poll.

    In the daemon the poll runs on a thread: it is one request a minute against a
    four-second tick, and a slow or hanging response must not be able to freeze
    the clock cell. Whatever it fetches lands in the cache and shows up on the
    next frame. `--once` has no next frame, so it waits.
    """
    cache = read_json(USAGE_CACHE_PATH)
    due = now - cache.get("polled_at", 0) >= config["usage_poll_seconds"]
    if allow_poll and due and now >= cache.get("retry_after", 0):
        if blocking:
            poll_usage(now, timeout=8.0)
            cache = read_json(USAGE_CACHE_PATH)
        else:
            import threading

            threading.Thread(target=poll_usage, args=(now,), daemon=True).start()

    account = sub_dict(sub_dict(read_json(GLOBAL_CONFIG_PATH), "cachedUsageUtilization"), "utilization")
    merged = blend_into_cache(cache, [account], now)
    if merged != cache:
        write_json_atomic(USAGE_CACHE_PATH, merged)
    return {name: merged.get(name) for name in WINDOWS}


# ----------------------------------------------------------------------------- git


def git_dir(start):
    """Locate the git directory governing `start`, or None outside a repository.

    A linked worktree's `.git` is a *file* holding `gitdir: <path>`, and HEAD lives
    at that path -- there is no `.git` directory anywhere above it, so a plain walk
    up the tree reports "not a repository" for exactly the checkouts worktree users
    work in. Submodules use the same pointer, relative to the file's own directory.
    """
    try:
        path = os.path.abspath(start)
    except (OSError, ValueError):
        return None
    while True:
        candidate = os.path.join(path, ".git")
        if os.path.isdir(candidate):
            return candidate
        if os.path.isfile(candidate):
            try:
                with open(candidate) as handle:
                    pointer = handle.read().strip()
            except OSError:
                return None
            if not pointer.startswith("gitdir:"):
                return None
            target = pointer[len("gitdir:"):].strip()
            return target if os.path.isabs(target) else os.path.normpath(os.path.join(path, target))
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def git_branch(start):
    """Branch name for `start`, `@<short sha>` when HEAD is detached, or None.

    Read off disk rather than shelled out: the transcript records a `gitBranch`
    field, but it is the branch as of the last message, and switching branch fires
    no event -- two stats and a small read keep the cell honest for free.
    """
    directory = git_dir(start)
    if directory is None:
        return None
    try:
        with open(os.path.join(directory, "HEAD")) as handle:
            head = handle.read().strip()
    except OSError:
        return None
    if head.startswith("ref: "):
        ref = head[len("ref: "):]
        return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    return f"@{head[:7]}" if head else None


# --------------------------------------------------------------------- hook writer

# Which hook event means what. Claude Code delivers `Notification` both for a
# permission request and for "I have been idle 60 s"; only the message tells them
# apart, so the mapping happens in `record_hook_event` rather than here.
HOOK_EVENT_STATE = {
    "SessionStart": "working",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "Stop": "idle",
    "SubagentStop": "working",
    "SessionEnd": "ended",
}

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd")


def record_hook_event(payload, now, ttl):
    """Fold one hook payload into the shared state file. Never raises.

    This runs inside the user's Claude Code session, so it has exactly two jobs:
    be fast and be harmless. It imports nothing beyond the standard library
    already loaded, does no network, and the caller swallows anything that leaks.
    """
    session = payload.get("session_id") or payload.get("sessionId")
    if not isinstance(session, str) or not session:
        return
    event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
    message = payload.get("message") if isinstance(payload.get("message"), str) else ""

    if event == "Notification":
        # "Claude needs your permission to use Bash" is a state worth lighting up
        # the keyboard for. "Claude is waiting for your input" is just idle.
        state = "waiting" if "permission" in message.lower() else "idle"
    else:
        state = HOOK_EVENT_STATE.get(event, "working")

    store = read_json(STATE_PATH)
    sessions = store.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}

    if state == "ended":
        sessions.pop(session, None)
    else:
        previous = sessions.get(session)
        previous = previous if isinstance(previous, dict) else {}
        entry = {"state": state, "event": event, "at": now}
        cwd = payload.get("cwd") or previous.get("cwd")
        if isinstance(cwd, str) and cwd:
            entry["cwd"] = cwd
        transcript = payload.get("transcript_path") or previous.get("transcript")
        if isinstance(transcript, str) and transcript:
            entry["transcript"] = transcript
        if message:
            entry["message"] = message[:200]
        # Turn bookkeeping: a turn starts at UserPromptSubmit and ends at Stop.
        # The start survives intermediate events so "elapsed this turn" and
        # "how long the last turn took" are both one subtraction for a reader.
        if event == "UserPromptSubmit":
            entry["turn_started_at"] = now
        elif isinstance(previous.get("turn_started_at"), (int, float)):
            entry["turn_started_at"] = previous["turn_started_at"]
        if event == "Stop" and isinstance(entry.get("turn_started_at"), (int, float)):
            entry["last_turn_seconds"] = max(0.0, now - entry["turn_started_at"])
        elif isinstance(previous.get("last_turn_seconds"), (int, float)):
            entry["last_turn_seconds"] = previous["last_turn_seconds"]
        sessions[session] = entry

    # Sessions that died without firing SessionEnd -- a killed terminal, a crash --
    # would otherwise sit here forever and outvote the session you are looking at.
    sessions = {
        key: value
        for key, value in sessions.items()
        if isinstance(value, dict) and now - value.get("at", 0) < ttl
    }
    store["sessions"] = sessions
    write_json_atomic(STATE_PATH, store)


# ---------------------------------------------------------------- transcript scan

# Fields worth pulling out of a session transcript, and the substring that marks a
# line as possibly carrying them. Checking the substring first means the common
# case -- a 5 MB transcript of tool calls -- costs a memchr per line instead of a
# JSON parse per line.
_TRANSCRIPT_CACHE = {}


def salient_input(tool_input):
    """The one field of a tool_use input worth showing on a tiny display.

    A file path becomes its basename; a command keeps its head. Anything
    longer than a display could ever want is cut here rather than in every
    renderer.
    """
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return os.path.basename(value.rstrip("/"))[:80]
    for key in ("command", "pattern", "query", "url", "description", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:80]
    return None


class Transcript:
    """Incrementally extracted facts about one session's .jsonl transcript.

    Re-reading a multi-megabyte transcript every tick would be silly, and reading
    only its tail would miss `ai-title`, which Claude Code writes once, early. So
    the first pass streams the whole file and every later pass seeks to where the
    last one stopped and reads only what was appended.
    """

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.model = None
        self.effort = None
        self.cwd = None
        self.title = None
        self.last_prompt = None
        self.last_event = 0.0
        # Latest tool call and latest TodoWrite payload, for displays that show
        # "what is it doing" / "how far through the plan is it". Same
        # incremental read; assistant records already pass the interesting-line
        # check, so absorbing their tool_use blocks costs nothing extra.
        self.tool_name = None
        self.tool_target = None
        self.todos = None

    def refresh(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return self
        if size < self.offset:
            # Truncated or replaced (`/clear` reuses the id): start over.
            self.__init__(self.path)
            size = 0
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
                # Stop at the last complete line; a partially flushed final line
                # will be read again, whole, on the next pass.
                cut = chunk.rfind(b"\n")
                if cut < 0:
                    return self
                self.offset += cut + 1
                for raw in chunk[:cut].split(b"\n"):
                    self._absorb(raw)
        except OSError:
            pass
        return self

    def _absorb(self, raw):
        if not raw or b'"type"' not in raw:
            return
        interesting = (
            b'"assistant"' in raw or b'"ai-title"' in raw or b'"last-prompt"' in raw
        )
        if not interesting:
            return
        try:
            record = json.loads(raw)
        except ValueError:
            return
        if not isinstance(record, dict):
            return
        kind = record.get("type")
        if kind == "ai-title":
            title = record.get("aiTitle")
            if isinstance(title, str) and title.strip():
                self.title = title.strip()
        elif kind == "last-prompt":
            prompt = record.get("lastPrompt")
            if isinstance(prompt, str) and prompt.strip():
                self.last_prompt = prompt.strip()
        elif kind == "assistant":
            message = record.get("message")
            if isinstance(message, dict) and isinstance(message.get("model"), str):
                self.model = message["model"]
            if isinstance(record.get("effort"), str):
                self.effort = record["effort"]
            if isinstance(record.get("cwd"), str):
                self.cwd = record["cwd"]
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    tool_input = block.get("input")
                    tool_input = tool_input if isinstance(tool_input, dict) else {}
                    if name == "TodoWrite":
                        todos = tool_input.get("todos")
                        if isinstance(todos, list):
                            self.todos = [t for t in todos if isinstance(t, dict)]
                    self.tool_name = name
                    self.tool_target = salient_input(tool_input)


def newest_transcripts(limit=6):
    """The most recently touched session transcripts, newest first.

    Bounded because ~/.claude/projects accumulates every session ever run, and
    only the handful at the top can possibly be the one you are looking at.
    """
    found = []
    try:
        projects = os.scandir(PROJECTS_DIR)
    except OSError:
        return []
    with projects:
        for project in projects:
            if not project.is_dir():
                continue
            try:
                entries = os.scandir(project.path)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    if not entry.name.endswith(".jsonl"):
                        continue
                    try:
                        found.append((entry.stat().st_mtime, entry.path, entry.name[:-6]))
                    except OSError:
                        continue
    found.sort(reverse=True)
    return found[:limit]


def transcript_facts(path):
    """Cached, incrementally updated `Transcript` for `path`."""
    reader = _TRANSCRIPT_CACHE.get(path)
    if reader is None:
        reader = _TRANSCRIPT_CACHE[path] = Transcript(path)
        # Keep the cache from growing once per session forever.
        if len(_TRANSCRIPT_CACHE) > 24:
            for stale in list(_TRANSCRIPT_CACHE)[:12]:
                if stale != path:
                    _TRANSCRIPT_CACHE.pop(stale, None)
    return reader.refresh()


# --------------------------------------------------------------- state resolution

# Long model ids are what the transcript carries; the display wants the short name.
MODEL_NAMES = (
    ("claude-opus-5", "Opus 5"),
    ("claude-sonnet-5", "Sonnet 5"),
    ("claude-fable-5", "Fable 5"),
    ("claude-haiku-4-5", "Haiku 4.5"),
    ("opus-4", "Opus 4"),
    ("sonnet-4", "Sonnet 4"),
    ("haiku-4", "Haiku 4"),
)


def short_model(model):
    if not isinstance(model, str) or not model:
        return None
    for needle, pretty in MODEL_NAMES:
        if needle in model:
            return pretty
    # Unknown id: strip the vendor prefix and the date suffix and hope for the best.
    name = model.replace("claude-", "").replace("-", " ")
    return name[:14].strip().title() or None


class Status:
    """One frame's worth of truth. Everything the renderer is allowed to know."""

    def __init__(self):
        self.state = "offline"      # working | waiting | idle | offline
        self.project = None
        self.branch = None
        self.model = None
        self.effort = None
        self.title = None
        self.detail = None          # the Notification text, when there is one
        self.five_hour = None
        self.seven_day = None
        self.last_activity = None   # epoch seconds
        self.pushed_at = None
        self.online = True          # did the last POST reach the keyboard


def collect(now, config, allow_poll=True, blocking=False):
    """Build a `Status` from hooks where available, transcripts where not."""
    status = Status()
    usage = current_usage(now, config, allow_poll=allow_poll, blocking=blocking)
    status.five_hour, status.seven_day = (usage.get(name) for name in WINDOWS)

    hooks = read_json(STATE_PATH).get("sessions")
    hooks = hooks if isinstance(hooks, dict) else {}
    transcripts = {sid: (mtime, path) for mtime, path, sid in newest_transcripts()}

    # One candidate per session, scored by whichever of its two clocks ran last.
    candidates = {}
    for sid, entry in hooks.items():
        if isinstance(entry, dict) and isinstance(entry.get("at"), (int, float)):
            candidates[sid] = entry["at"]
    for sid, (mtime, _path) in transcripts.items():
        candidates[sid] = max(candidates.get(sid, 0), mtime)
    if not candidates:
        return status

    session = max(candidates, key=candidates.get)
    activity = candidates[session]
    status.last_activity = activity
    if now - activity > config["session_ttl_seconds"]:
        # Nothing has happened in hours. Show the last project, but call it idle.
        status.state = "idle"

    hook = hooks.get(session) if isinstance(hooks.get(session), dict) else {}
    facts = transcript_facts(transcripts[session][1]) if session in transcripts else None

    cwd = hook.get("cwd") or (facts.cwd if facts else None)
    if isinstance(cwd, str) and cwd.strip("/"):
        status.project = os.path.basename(cwd.rstrip("/"))
        status.branch = git_branch(cwd)
    if facts:
        status.model = short_model(facts.model)
        status.effort = facts.effort
        # Not drawn any more; kept because `--status` is where you go to find
        # out which session the panel actually latched onto.
        status.title = facts.title or facts.last_prompt

    status.state = resolve_state(hook, transcripts.get(session), now, config)
    if status.state == "waiting":
        status.detail = hook.get("message")
    return status


def resolve_state(hook, transcript, now, config):
    """working / waiting / idle, from a hook event and a transcript mtime.

    The two disagree constantly and each is right about something different. The
    hook knows *what happened* -- a permission prompt is not something a mtime can
    reveal -- but it is a snapshot from whenever it last fired. The mtime knows
    *that something is happening right now* but not what. So: a permission prompt
    wins outright, a transcript still growing after the last hook event means work
    resumed and the hook is simply behind, and everything else falls back to
    liveness.
    """
    mtime = transcript[0] if transcript else 0.0
    fresh = now - mtime <= config["active_seconds"]
    hook_at = hook.get("at", 0) if hook else 0
    hook_state = hook.get("state") if hook else None

    if hook_state == "waiting":
        # Permission prompts do not expire on their own; they end with the next
        # event. Trust it until something else fires or the session goes quiet.
        if now - hook_at <= config["session_ttl_seconds"]:
            return "waiting"

    # A transcript that grew after the hook fired means the hook is stale: Stop
    # said idle, then a new turn started without any hook we subscribe to.
    if mtime > hook_at + 1.0:
        return "working" if fresh else "idle"

    if hook_state == "working":
        # Guard against a session killed mid-turn, which leaves "working" forever.
        return "working" if now - hook_at <= config["session_ttl_seconds"] else "idle"
    if hook_state == "idle":
        return "idle"
    return "working" if fresh else "idle"


# ------------------------------------------------------------------------- palette

BG = (23, 21, 19)
INK = (244, 239, 231)
DIM = (133, 125, 114)
FAINT = (58, 53, 48)
RULE = (44, 40, 36)
CLAY = (217, 119, 87)        # the accent this whole thing is built around
CLAY_DEEP = (150, 76, 53)
GOOD = (127, 176, 105)
WARN = (232, 176, 75)
BAD = (224, 108, 90)
SLEEP = (108, 118, 132)

# Short because they have to be. Across the 122 px between the margins, "NEEDS
# YOU" tops out at 14 arcmin and "WORKING" at 16 -- at or below the floor where
# type stops being glanceable -- while four characters clear the 22 arcmin target
# outright. The words got shorter rather than smaller; colour and pose already
# carry the nuance the extra letters were adding.
STATE_STYLE = {
    "working": ("BUSY", CLAY),
    "waiting": ("YOU", WARN),
    "idle": ("IDLE", SLEEP),
    "offline": ("OFF", FAINT),
}

# Supersampling factor for every vector shape. FreeType antialiases text for us;
# ImageDraw does not antialias anything, and a jagged mascot on a 142 px panel is
# immediately obvious. Drawing the shapes 4x and resampling down fixes it for the
# cost of one 568x1712 RGBA buffer per frame.
# Supersampling factor for the vector layer. Measured, because the obvious knob is
# not always the one that is stuck: stepping 4 -> 6 -> 8 -> 12 moves the number of
# distinct edge grey levels 223 -> 243 -> 249 -> 247 and the worst-case pixel by
# 60 -> 28 -> 20 of 255, so 8 is where it stops paying. Render goes 7.7 ms -> 15.7 ms
# and the transient layer 3.9 MB -> 15.6 MB, which is nothing against a 5 s tick.
SS = 8

FONT_ROUNDED = "/System/Library/Fonts/SFNSRounded.ttf"
FONT_TEXT = "/System/Library/Fonts/SFNS.ttf"
FONT_CJK = "/System/Library/Fonts/PingFang.ttc"
FONT_CJK_INDEX = 2           # PingFang SC Regular
FONT_CJK_BOLD_INDEX = 5      # PingFang SC Medium

_FONT_CACHE = {}


def _load(path, size, weight, index=0):
    """A TrueType font, with the variable Weight axis set when the face has one.

    San Francisco ships as a variable font, so one file covers every weight -- but
    the axis order differs between faces (SFNS has four axes, SFNSRounded two), so
    the axis is located by name rather than by position.
    """
    from PIL import ImageFont

    key = (path, size, weight, index)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        font = ImageFont.truetype(path, size, index=index)
    except OSError:
        font = ImageFont.load_default()
        _FONT_CACHE[key] = font
        return font
    if weight is not None:
        try:
            axes = font.get_variation_axes()
            values = []
            for axis in axes:
                name = axis.get("name")
                name = name.decode() if isinstance(name, bytes) else str(name)
                if name == "Weight":
                    values.append(max(axis["minimum"], min(axis["maximum"], weight)))
                else:
                    values.append(axis["default"])
            font.set_variation_by_axes(values)
        except (OSError, AttributeError, KeyError):
            pass  # not a variable font, or a Pillow built without FreeType support
    _FONT_CACHE[key] = font
    return font


def font(size, weight=400, rounded=True):
    """A (latin, cjk) pair. Text is drawn run by run so mixed strings just work."""
    path = FONT_ROUNDED if rounded else FONT_TEXT
    cjk_index = FONT_CJK_BOLD_INDEX if weight >= 550 else FONT_CJK_INDEX
    return (_load(path, size, weight), _load(FONT_CJK, size, None, cjk_index))


def _is_wide(char):
    """Does this character need the CJK face (and roughly a full em of width)?"""
    point = ord(char)
    return (
        0x1100 <= point <= 0x11FF
        or 0x2E80 <= point <= 0xA4CF
        or 0xA960 <= point <= 0xA97F
        or 0xAC00 <= point <= 0xD7FF
        or 0xF900 <= point <= 0xFAFF
        or 0xFE30 <= point <= 0xFE4F
        or 0xFF00 <= point <= 0xFF60
        or 0xFFE0 <= point <= 0xFFE6
    )


def runs(text):
    """Split into (needs_cjk_face, chunk) runs, so each is drawn with one font."""
    out = []
    for char in text:
        wide = _is_wide(char)
        if out and out[-1][0] == wide:
            out[-1][1].append(char)
        else:
            out.append((wide, [char]))
    return [(wide, "".join(chars)) for wide, chars in out]


def measure(draw, text, fonts):
    return sum(draw.textlength(chunk, font=fonts[1 if wide else 0]) for wide, chunk in runs(text))


def write(draw, x, y, text, fonts, fill, align="left", max_width=None):
    """Draw a possibly-mixed-script string. Returns the width it took.

    Nothing is letterspaced any more: tracking was there to keep 9 px all-caps
    labels from reading as one grey smudge, and there is no 9 px type left.
    """
    if max_width is not None:
        text = elide(draw, text, fonts, max_width)
    width = measure(draw, text, fonts)
    if align == "center":
        x -= width / 2
    elif align == "right":
        x -= width
    for wide, chunk in runs(text):
        face = fonts[1 if wide else 0]
        draw.text((x, y), chunk, font=face, fill=fill)
        x += draw.textlength(chunk, font=face)
    return width


def cap_box(draw, fonts, sample="8"):
    """(anchor-to-ink-top offset, ink height) for a cap-height glyph in `fonts`.

    The layout is solved in cap heights, because cap height is what the
    visual-angle arithmetic above is about. PIL's own origin is ascender-relative
    and the gap between the two drifts with size, so every row is placed by where
    its ink actually starts rather than by where PIL would like to begin drawing.
    """
    box = draw.textbbox((0, 0), sample, font=fonts[0])
    return box[1], box[3] - box[1]


def fit(draw, text, max_width, sizes, weight=700, rounded=True):
    """The largest of `sizes` at which `text` fits `max_width`; the smallest if none.

    Model names run from "Opus 5" to "Haiku 4.5" -- a third more width for the same
    row. Setting the row to whatever the longest name needs would punish every
    frame for the worst case, so each frame gets the biggest type its own name
    allows and the caller reserves the space either way.
    """
    for size in sizes:
        fonts = font(size, weight, rounded=rounded)
        if measure(draw, text, fonts) <= max_width:
            return fonts
    return font(sizes[-1], weight, rounded=rounded)


def elide(draw, text, fonts, max_width):
    """Trim from the middle, keeping both ends.

    Sibling branches and sibling directories routinely differ only in their
    suffix, so cutting the tail renders two different things identically.
    """
    if measure(draw, text, fonts) <= max_width:
        return text
    if len(text) <= 3:
        return text
    head, tail = len(text) // 2, len(text) // 2
    while head > 1 and tail > 1:
        head -= 1
        tail -= 1
        candidate = f"{text[:head]}…{text[len(text) - tail:]}"
        if measure(draw, candidate, fonts) <= max_width:
            return candidate
    return "…"


def capsule(draw, p0, p1, r0, r1, fill):
    """A tapered round-ended bar from `p0` (radius r0) to `p1` (radius r1)."""
    import math

    (x0, y0), (x1, y1) = p0, p1
    draw.ellipse([x0 - r0, y0 - r0, x0 + r0, y0 + r0], fill=fill)
    draw.ellipse([x1 - r1, y1 - r1, x1 + r1, y1 + r1], fill=fill)
    length = math.hypot(x1 - x0, y1 - y0) or 1.0
    nx, ny = -(y1 - y0) / length, (x1 - x0) / length
    draw.polygon(
        [
            (x0 + nx * r0, y0 + ny * r0),
            (x1 + nx * r1, y1 + ny * r1),
            (x1 - nx * r1, y1 - ny * r1),
            (x0 - nx * r0, y0 - ny * r0),
        ],
        fill=fill,
    )


def disc(draw, cx, cy, r, fill):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)


def draw_mascot(draw, cx, cy, size, state, phase):
    """The character, centred on (cx, cy) and `size` across. Supersampled space.

    `phase` only matters while working: the bloom turns a few degrees per frame,
    so a display that is otherwise identical minute to minute visibly *moves* when
    Claude is busy. It is the cheapest possible "still alive" indicator.
    """
    import math

    body = {"working": CLAY, "waiting": WARN, "idle": (156, 99, 78), "offline": (74, 68, 62)}[state]
    core = tuple(min(255, int(channel * 1.12) + 12) for channel in body)
    rays = 10

    # Glow. ImageDraw replaces alpha rather than blending it, so the rings are
    # painted outside-in with rising opacity to fake a radial falloff.
    for step in range(6, 0, -1):
        alpha = int(12 * (7 - step) / 6)
        disc(draw, cx, cy, size * (0.30 + 0.055 * step), body + (alpha,))

    spin = phase if state == "working" else 0.0
    for index in range(rays):
        angle = spin + index * 2 * math.pi / rays
        # Alternating reach keeps it from reading as a gear or a sun clip-art.
        reach = 0.58 if index % 2 == 0 else 0.46
        inner = (cx + math.cos(angle) * size * 0.10, cy + math.sin(angle) * size * 0.10)
        outer = (cx + math.cos(angle) * size * reach, cy + math.sin(angle) * size * reach)
        capsule(draw, inner, outer, size * 0.078, size * 0.034, body + (255,))

    disc(draw, cx, cy, size * 0.255, core + (255,))

    # ------------------------------------------------------------------ the face
    eye_dx, eye_y = size * 0.098, cy - size * 0.038
    eye_w, eye_h = size * 0.044, size * 0.064
    dark = BG + (255,)

    if state == "idle":
        # Closed, curving up: asleep rather than switched off.
        for side in (-1, 1):
            box = [cx + side * eye_dx - eye_w * 1.5, eye_y - eye_h * 0.5,
                   cx + side * eye_dx + eye_w * 1.5, eye_y + eye_h * 1.1]
            draw.arc(box, 0, 180, fill=dark, width=max(1, int(size * 0.028)))
    elif state == "offline":
        for side in (-1, 1):
            capsule(draw, (cx + side * eye_dx - eye_w, eye_y), (cx + side * eye_dx + eye_w, eye_y),
                    size * 0.018, size * 0.018, dark)
    elif state == "waiting":
        for side in (-1, 1):
            disc(draw, cx + side * eye_dx, eye_y, eye_h * 0.95, dark)
            disc(draw, cx + side * eye_dx + eye_h * 0.28, eye_y - eye_h * 0.3, eye_h * 0.26, core + (255,))
    else:
        # Working: pupils track a slow orbit, which reads as "looking around".
        gaze = math.cos(phase * 0.7) * size * 0.018
        for side in (-1, 1):
            draw.ellipse(
                [cx + side * eye_dx - eye_w + gaze, eye_y - eye_h,
                 cx + side * eye_dx + eye_w + gaze, eye_y + eye_h],
                fill=dark,
            )

    mouth_y = cy + size * 0.098
    if state == "waiting":
        disc(draw, cx, mouth_y + size * 0.01, size * 0.038, dark)
    elif state == "idle":
        draw.arc([cx - size * 0.075, mouth_y - size * 0.055, cx + size * 0.075, mouth_y + size * 0.055],
                 0, 180, fill=dark, width=max(1, int(size * 0.026)))
    elif state == "offline":
        capsule(draw, (cx - size * 0.05, mouth_y), (cx + size * 0.05, mouth_y),
                size * 0.016, size * 0.016, dark)
    else:
        draw.ellipse([cx - size * 0.045, mouth_y - size * 0.032,
                      cx + size * 0.045, mouth_y + size * 0.045], fill=dark)

    # --------------------------------------------------------------- accessories
    # The working state used to throw three twinkles onto an orbit at 0.66 * size.
    # They were 10 px across -- about 9 arcmin from where this panel is read, below
    # the threshold where anything resolves -- and that orbit is what capped the
    # character's size, because it clipped both margins before the rays did.
    # Dropping them bought 6 px of bloom, which is legible. The turning is the
    # "still alive" signal; the sparks were only ever decoration.
    if state == "waiting":
        # A badge, so the one state that wants your attention has a silhouette you
        # can recognise before you have read a single word.
        bx, by = cx + size * 0.42, cy - size * 0.42
        disc(draw, bx, by, size * 0.145, BG + (255,))
        disc(draw, bx, by, size * 0.115, WARN + (255,))
        capsule(draw, (bx, by - size * 0.055), (bx, by + size * 0.012),
                size * 0.022, size * 0.017, BG + (255,))
        disc(draw, bx, by + size * 0.058, size * 0.021, BG + (255,))


# -------------------------------------------------------------------------- layout

PAD = 10                     # side margin; everything lives inside it

# ------------------------------------------------------------------ readability
#
# The panel is 2.79 in on the diagonal at 142x428 px, and that one number decides
# the whole layout:
#
#   diagonal   sqrt(142^2 + 428^2) = 450.9 px
#   density    450.9 / 2.79        = 161.6 ppi   ->  1 px = 0.157 mm
#   physical   22.3 mm x 67.3 mm                 ->  narrower than a finger
#
# Read while typing, it sits roughly 600 mm from your eyes. Character height for
# a given visual angle is D * tan(arcmin / 60 degrees), so at that distance:
#
#   16 arcmin -> 2.79 mm -> 17.8 px of cap height   the comfortable floor
#   22 arcmin -> 3.84 mm -> 24.4 px of cap height   easy at a glance  <- target
#   28 arcmin -> 4.89 mm -> 31.1 px of cap height   low light, peripheral
#
# San Francisco's cap height measures 0.71-0.73 of PIL's size parameter (measured,
# not assumed -- the ratio wanders by a point or two with hinting), so 24.4 px of
# cap height is size 34. Everything the panel exists to tell you is set there.
#
# The corollary is the part that took a redesign to accept: 161.6 ppi on a 22 mm
# strip does not buy detail, it buys about six legible characters per line. Words
# that could not be said in six were shortened or dropped, not shrunk -- a cell
# nobody can read is worse than no cell, because it still costs the space.
CORE_SIZE = 34               # 25 px cap = 22 arcmin at 600 mm
FLOOR_SIZE = 25              # 18 px cap = 16 arcmin; nothing readable goes below
MODEL_SIZES = tuple(range(CORE_SIZE, FLOOR_SIZE - 1, -1))

# The top of the physical panel sits behind the device's own cutouts, so the
# first 40 rows are unusable -- not dim, not cropped, covered. Every coordinate
# below is derived from MASCOT_TOP so nothing can drift back up into them.
DEAD_ZONE = 40
MASCOT_TOP = 44              # first row the character is allowed to touch
MASCOT_SIZE = 80             # its outermost glow reaches MASCOT_REACH * this
MASCOT_REACH = 0.63          # matches the last ring draw_mascot paints

# The character and the badge are the two elements that are emphasis rather than
# information -- the meters and the model name are the reason the panel exists. So
# these two are the ones that give ground when something has to: at 80 px the
# character is 12.6 mm across, which subtends 72 arcmin and is in no danger of
# being hard to see, and the badge is set below CORE_SIZE on purpose. Nothing that
# carries a number went below the floor to pay for this.
STATE_SIZE = 28              # 20 px cap = 18.0 arcmin; above FLOOR, below CORE
PILL_PAD = 7                 # ink-to-edge inside the state badge
CLOCK_SIZE = 28              # same: legible, visibly subordinate
BAR_H = 20                   # 3.1 mm: a meter you read as a shape, not as a line
GAP_MASCOT = 13              # character to badge
GAP_SECTION = 18             # between the badge, the model and the two meters
GAP_BAR = 6                  # a meter's number to its bar
GAP_FOOTER = 14              # last meter to the footer rule


def usage_color(pct):
    if pct >= 90:
        return BAD
    if pct >= 75:
        return (226, 140, 88)
    if pct >= 50:
        return WARN
    return GOOD


def render(status, now, config, phase=0.0):
    """Compose one frame. Returns an RGB `Image` of exactly width x height.

    Two passes over two surfaces: every vector shape goes onto a 4x RGBA layer
    that is resampled down for antialiasing, then text is drawn at final size on
    top so FreeType's own hinting and antialiasing survive.

    Four things are on screen -- the character, the state, the model, and the two
    usage meters -- and every one of them is set at CORE_SIZE, or as close to it
    as 122 px of width allows. The readability note above the layout constants is
    where those sizes come from; the short version is that this is a 22 mm-wide
    strip read from 600 mm away, which fits about six legible characters a line.
    """
    from PIL import Image, ImageDraw

    width, height = config["width"], config["height"]
    base = Image.new("RGB", (width, height), BG)
    shapes = Image.new("RGBA", (width * SS, height * SS), (0, 0, 0, 0))
    glyphs = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    vector = ImageDraw.Draw(shapes)
    draw = ImageDraw.Draw(glyphs)

    inner = width - 2 * PAD
    cx = width / 2
    word, tint = STATE_STYLE[status.state]
    core_cap = cap_box(draw, font(CORE_SIZE, 800))[1]

    # ------------------------------------------------------------------ mascot
    # MASCOT_REACH is how far the outermost glow ring gets from the centre, so
    # deriving the centre from it puts the character's first non-background pixel
    # on MASCOT_TOP exactly -- the check that keeps it clear of the cutouts. The
    # size is emphasis, not information -- see the note on MASCOT_SIZE.
    radius = MASCOT_SIZE * MASCOT_REACH
    cy = MASCOT_TOP + radius
    draw_mascot(vector, cx * SS, cy * SS, MASCOT_SIZE * SS, status.state, phase)
    if status.state == "idle":
        # The one part of the character that is cheaper to set in type than to
        # draw from primitives, because it is literally a letter.
        # Placed and sized off MASCOT_SIZE so they follow the character instead of
        # having to be re-found by hand every time it changes.
        write(draw, cx + MASCOT_SIZE * 0.30, cy - MASCOT_SIZE * 0.42, "z",
              font(max(9, int(MASCOT_SIZE * 0.17)), 700), (118, 128, 142))
        write(draw, cx + MASCOT_SIZE * 0.46, cy - MASCOT_SIZE * 0.62, "z",
              font(max(11, int(MASCOT_SIZE * 0.24)), 700), (132, 142, 156))

    # ------------------------------------------- state badge under the character
    state_font = font(STATE_SIZE, 800)
    state_top, state_cap = cap_box(draw, state_font, "H")
    pill_h = state_cap + 2 * PILL_PAD
    pill_w = min(inner, measure(draw, word, state_font) + 2 * PILL_PAD + 6)
    pill_y = cy + radius + GAP_MASCOT
    vector.rounded_rectangle(
        [(cx - pill_w / 2) * SS, pill_y * SS, (cx + pill_w / 2) * SS, (pill_y + pill_h) * SS],
        radius=pill_h / 2 * SS, fill=tint + (40,), outline=tint + (170,), width=SS,
    )
    write(draw, cx, pill_y + PILL_PAD - state_top, word, state_font, tint, align="center")

    # ------------------------------------------------------------------- model
    # Sized per frame: "Opus 5" clears the 22 arcmin target outright, "Haiku 4.5"
    # is three characters longer than the panel can carry at that size and gets
    # the biggest type it can instead of dragging every other name down with it.
    # The slot advances by core_cap either way, so the rows below never move.
    cursor = pill_y + pill_h + GAP_SECTION
    name = status.model or "--"
    name_font = fit(draw, name, inner, MODEL_SIZES, weight=700)
    write(draw, PAD, cursor - cap_box(draw, name_font)[0], name, name_font,
          INK if status.model else DIM, max_width=inner)
    cursor += core_cap

    # ------------------------------------------------------------------- usage
    # "5H 100%" is 145 px at CORE_SIZE and the panel has 122, so the per-cent sign
    # went rather than the type size -- a number sitting on top of a meter does not
    # need one. Labels are two characters for the same reason.
    label_font = font(CORE_SIZE, 700)
    label_top = cap_box(draw, label_font)[0]
    value_font = font(CORE_SIZE, 800)
    value_top = cap_box(draw, value_font)[0]
    for label, data in (("5H", status.five_hour), ("7D", status.seven_day)):
        cursor += GAP_SECTION
        if data is None:
            pct, colour, reading = 0.0, FAINT, "--"
        else:
            pct = max(0.0, min(100.0, data["used_percentage"]))
            colour = usage_color(pct)
            reading = f"{pct:.0f}"
        write(draw, PAD, cursor - label_top, label, label_font, DIM)
        write(draw, width - PAD, cursor - value_top, reading, value_font, colour, align="right")
        bar_y = cursor + core_cap + GAP_BAR
        vector.rounded_rectangle(
            [PAD * SS, bar_y * SS, (width - PAD) * SS, (bar_y + BAR_H) * SS],
            radius=BAR_H / 2 * SS, fill=FAINT + (255,))
        filled = inner * pct / 100.0
        if filled >= 1:
            vector.rounded_rectangle(
                [PAD * SS, bar_y * SS, (PAD + max(BAR_H, filled)) * SS, (bar_y + BAR_H) * SS],
                radius=BAR_H / 2 * SS, fill=colour + (255,))
        cursor = bar_y + BAR_H

    # ------------------------------------------------------------------ footer
    # The clock is back. It was cut last round to buy cap height, and shrinking the
    # character and the badge bought the row again -- 45 px for a fact you glance at
    # while your hands are already here. Set at STATE_SIZE, not CORE_SIZE: legible
    # at 600 mm, and visibly not competing with the numbers above it.
    cursor += GAP_FOOTER
    vector.rectangle(
        [PAD * SS, cursor * SS, (width - PAD) * SS, cursor * SS + SS], fill=RULE + (255,))
    clock_font = font(CLOCK_SIZE, 600, rounded=False)
    cursor += 11
    write(draw, cx, cursor - cap_box(draw, clock_font)[0],
          time.strftime("%H:%M", time.localtime(now)), clock_font, DIM, align="center")

    # Shapes first, then type, so the pill outline and the bars sit *behind* their
    # labels no matter what order the layout drew them in.
    flattened = shapes.resize((width, height), Image.LANCZOS)
    base.paste(flattened, (0, 0), flattened)
    base.paste(glyphs, (0, 0), glyphs)
    return base


# ---------------------------------------------------------------------------- push


def testcard(config):
    """A geometry-only resolution card. No layout, no state, no fonts on the fine bands.

    This exists to answer one question the pretty picture cannot: is the panel
    showing our pixels, or its own resampling of them? Every band below is a pattern
    that 4:4:4 JPEG carries almost losslessly -- a 1 px checkerboard round-trips
    through our own encoder with a maximum error of 4/255 at quality 95, because it
    is very nearly a pure DCT basis function -- so whatever softness shows up on the
    glass was added after the POST.

    Reading it, top to bottom:

      0-48    an 8 px ladder. The band immediately above the 40 px line is red, the
              one below it green: if no red is visible, DEAD_ZONE is right.
      48-96   1 px checkerboard      -- flat grey here means the device is rescaling
      96-144  1 px vertical lines    -- vertical resolution of the scaler
      144-192 1 px horizontal lines  -- horizontal resolution of the scaler
      192-224 2 px vertical lines
      224-256 4 px vertical lines    -- if even these are soft, it is the panel
      256-304 16 px hard-edged blocks -- JPEG-safe control. Soft here = device, full
              stop, because nothing in our pipeline can blur an edge this coarse.
      304-390 text at 34 / 28 / 22, drawn at native size with no supersampling
      390-428 an A/B on antialiasing, split by a hairline at x=70. Left disc and
              diagonal go through the same supersampled vector layer the character
              and the meters use. Right disc and diagonal are drawn straight onto
              the native canvas with ImageDraw, which does not antialias at all --
              that is the control, and it is *meant* to look stepped. Same radius,
              same slope, same stroke width, so the only difference is the path.
              Left smooth and right stepped means our antialiasing reaches the
              glass. Both stepped means it does not. Both smooth means the panel
              is softening everything, which the gratings above would already have
              shown.

    An earlier version of this card drew both shapes the native way and described
    them as coming off the vector layer, which is exactly backwards and sent one
    round of diagnosis down the wrong path. Hence the A/B.
    """
    from PIL import Image, ImageDraw

    width, height = config["width"], config["height"]
    card = Image.new("RGB", (width, height), (0, 0, 0))
    px = card.load()
    draw = ImageDraw.Draw(card)

    # 0-48: where does the cutout actually end?
    ladder = [(90, 90, 90), (150, 150, 150), (90, 90, 90), (150, 150, 150),
              (220, 40, 40), (40, 200, 90)]
    for index, colour in enumerate(ladder):
        draw.rectangle([0, index * 8, width - 1, index * 8 + 7], fill=colour)

    def band(y0, y1, keep):
        for y in range(y0, y1):
            for x in range(width):
                px[x, y] = (255, 255, 255) if keep(x, y) else (0, 0, 0)

    band(48, 96, lambda x, y: (x + y) % 2 == 0)          # 1 px checkerboard
    band(96, 144, lambda x, y: x % 2 == 0)               # 1 px vertical
    band(144, 192, lambda x, y: y % 2 == 0)              # 1 px horizontal
    band(192, 224, lambda x, y: (x // 2) % 2 == 0)       # 2 px vertical
    band(224, 256, lambda x, y: (x // 4) % 2 == 0)       # 4 px vertical
    band(256, 304, lambda x, y: (x // 16) % 2 == 0)      # 16 px blocks

    # Text, at native size, straight onto the RGB image -- no supersampling anywhere.
    y = 306
    for size in (34, 28, 22):
        write(draw, PAD, y, "Opus 8", font(size, 700), (255, 255, 255))
        y += cap_box(draw, font(size, 700))[1] + 8

    # Left: through the vector layer, the way every curve in the real layout is
    # drawn. Same geometry on the right, drawn natively as the aliased control.
    shapes = Image.new("RGBA", (width * SS, height * SS), (0, 0, 0, 0))
    vector = ImageDraw.Draw(shapes)
    white = (255, 255, 255, 255)
    disc(vector, 21 * SS, 409 * SS, 13 * SS, white)
    capsule(vector, (38 * SS, 422 * SS), (64 * SS, 396 * SS), 1.5 * SS, 1.5 * SS, white)
    flattened = shapes.resize((width, height), Image.LANCZOS)
    card.paste(flattened, (0, 0), flattened)

    draw.line([(70, 392), (70, 426)], fill=(80, 80, 80), width=1)
    draw.ellipse([78, 396, 104, 422], fill=(255, 255, 255))
    draw.line([(108, 422), (134, 396)], fill=(255, 255, 255), width=3)
    return card


def encode(image, config):
    """Baseline JPEG bytes, sized to fit the device's 512 KB ceiling.

    The keyboard rejects progressive JPEG, so `progressive` is never passed --
    Pillow's default is baseline and it stays that way. At 142x428 even quality 95
    lands around 20 KB, so the shrink loop below is a guard against a future
    larger panel rather than something this resolution will ever reach.
    """
    import io

    quality = max(30, min(95, int(config["jpeg_quality"])))
    while True:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True, subsampling=0)
        data = buffer.getvalue()
        if len(data) <= 512 * 1024 or quality <= 30:
            return data
        quality -= 10


def push(data, config):
    """POST the frame. Returns (ok, note); never raises, never blocks for long.

    A keyboard that is asleep, unplugged or on another network is the normal case,
    not an error -- this runs on a laptop that moves. The caller backs off and the
    display simply keeps whatever frame it last received.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        config["url"], data=data, method="POST",
        headers={"Content-Type": "image/jpeg", "Content-Length": str(len(data))},
    )
    try:
        with urllib.request.urlopen(request, timeout=config["http_timeout_seconds"]) as response:
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}"
    except urllib.error.URLError as error:
        return False, str(getattr(error, "reason", error))
    except (OSError, ValueError, TimeoutError) as error:
        return False, str(error)


# -------------------------------------------------------------------------- daemon


def log(message):
    """One line, timestamped, to stderr -- launchd routes it into the log file."""
    sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    sys.stderr.flush()


def run_daemon(config, once=False):
    """Render on a fixed tick; push when the frame changed or the heartbeat is due.

    The tick is what keeps the countdowns honest; the change check is what keeps
    the network quiet. Between them the keyboard sees a POST roughly once a minute
    while idle -- the clock cell rolling over -- and once per tick while Claude is
    working and the character is spinning.
    """
    import hashlib

    last_digest = None
    last_push = 0.0
    offline_until = 0.0
    online = True
    phase = 0.0
    failures = 0

    while True:
        started = time.time()
        try:
            status = collect(started, config, blocking=once)
            status.online = online
            phase += 0.55
            frame = encode(render(status, started, config, phase=phase), config)
            digest = hashlib.sha1(frame).digest()

            due = digest != last_digest or started - last_push >= config["heartbeat_seconds"]
            if due and started >= offline_until:
                ok, note = push(frame, config)
                if ok:
                    if not online:
                        log(f"keyboard back online ({note})")
                    online, failures, offline_until = True, 0, 0.0
                    last_digest, last_push = digest, started
                else:
                    failures += 1
                    online = False
                    # Grows, but only to a minute: a panel that sleeps between
                    # uploads has to be caught while it is briefly awake.
                    wait = min(60.0, config["offline_backoff_seconds"] * min(failures, 3))
                    offline_until = started + wait
                    if failures == 1 or failures % 20 == 0:
                        log(f"push failed ({note}); retrying in {wait:.0f}s [{failures}]")
        except Exception as error:  # noqa: BLE001 - a daemon that dies is a bug
            log(f"tick failed: {type(error).__name__}: {error}")
        if once:
            return 0 if online else 1
        elapsed = time.time() - started
        time.sleep(max(0.5, config["tick_seconds"] - elapsed))


# ---------------------------------------------------------------------- installer

HOOK_MARKER = "keyboard-status.py"      # how we recognise our own hook entries
HOOK_TIMEOUT = 5


def venv_python():
    return os.path.join(VENV_PATH, "bin", "python3")


def ensure_venv():
    """A private virtualenv with Pillow in it, at ~/.claude/keyboard-status-venv.

    Pillow is the one thing this needs that claude-status-bar did not, and there
    is no polite way to install it into a system Python on a modern macOS (PEP 668
    marks it externally managed, and rightly). A venv beside the script keeps the
    dependency entirely inside ~/.claude, so uninstalling is `rm -rf`.
    """
    import subprocess

    python = venv_python()
    if not os.path.exists(python):
        print(f"creating virtualenv at {VENV_PATH}")
        subprocess.run([sys.executable, "-m", "venv", VENV_PATH], check=True)
    probe = subprocess.run([python, "-c", "import PIL"], capture_output=True)
    if probe.returncode != 0:
        print("installing Pillow")
        subprocess.run([python, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=False)
        subprocess.run([python, "-m", "pip", "install", "--quiet", "Pillow"], check=True)
    return python


def write_plist(python):
    """A launchd agent that starts at login and is restarted if it ever dies."""
    import plistlib

    os.makedirs(os.path.dirname(LAUNCH_PLIST), exist_ok=True)
    agent = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [python, INSTALL_PATH, "--daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        # Without this a crash-on-start loops as fast as launchd can fork.
        "ThrottleInterval": 15,
        "StandardOutPath": LOG_PATH,
        "StandardErrorPath": LOG_PATH,
        # Tells the scheduler this is not interactive, so it yields to real work.
        "ProcessType": "Background",
    }
    with open(LAUNCH_PLIST, "wb") as handle:
        plistlib.dump(agent, handle)
    return LAUNCH_PLIST


def launchctl(*args, quiet=True):
    import subprocess

    try:
        result = subprocess.run(["launchctl", *args], capture_output=True, text=True)
        if result.returncode != 0 and not quiet:
            sys.stderr.write(result.stderr)
        return result.returncode == 0
    except OSError:
        return False


def reload_agent():
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LAUNCH_LABEL}")          # ignore "not loaded"
    if not launchctl("bootstrap", domain, LAUNCH_PLIST, quiet=False):
        # Older macOS, or a domain that refuses bootstrap: the legacy verbs still work.
        launchctl("unload", LAUNCH_PLIST)
        launchctl("load", "-w", LAUNCH_PLIST, quiet=False)


def merge_hooks(settings, command):
    """Add our hook to each event, leaving every other hook exactly as it was.

    settings.json is the user's file and routinely holds hooks from other tools.
    Each event maps to a list of matcher groups, each holding a list of commands,
    so the safe edit is: find the group that already contains *our* command and
    update it in place, otherwise append one group of our own. Re-running install
    must not accumulate duplicates, which is what the marker search is for.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    entry = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
        replaced = False
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for index, existing in enumerate(group["hooks"]):
                if isinstance(existing, dict) and HOOK_MARKER in str(existing.get("command", "")):
                    group["hooks"][index] = entry
                    replaced = True
        if not replaced:
            groups.append({"hooks": [entry]})
        hooks[event] = groups
    settings["hooks"] = hooks
    return settings


def strip_hooks(settings):
    """Remove only our hook entries, and only empty groups we left behind."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept.append(group)
                continue
            group["hooks"] = [
                item for item in group["hooks"]
                if not (isinstance(item, dict) and HOOK_MARKER in str(item.get("command", "")))
            ]
            # Only drop the group if it is now empty *and* carries nothing else,
            # so a matcher someone configured by hand is never silently discarded.
            if group["hooks"] or set(group) - {"hooks"}:
                kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


def save_settings(settings):
    with open(SETTINGS_PATH, "w") as handle:
        json.dump(settings, handle, indent=2)
        handle.write("\n")


def load_settings_for_edit():
    """settings.json, with a backup taken first if it is present but unreadable."""
    settings = read_json(SETTINGS_PATH)
    if os.path.exists(SETTINGS_PATH) and not settings:
        # Unreadable rather than absent. Overwriting would silently drop every
        # other setting, so keep a copy of whatever is there before replacing it.
        backup = f"{SETTINGS_PATH}.bak"
        os.replace(SETTINGS_PATH, backup)
        print(f"settings.json was unreadable; kept a copy at {backup}")
    return settings


def install(with_hooks=True):
    os.makedirs(CLAUDE_DIR, exist_ok=True)

    source = os.path.abspath(__file__)
    if source != INSTALL_PATH:
        with open(source) as src, open(INSTALL_PATH, "w") as dst:
            dst.write(src.read())
        os.chmod(INSTALL_PATH, 0o755)
        print(f"installed {INSTALL_PATH}")

    if not os.path.exists(CONFIG_PATH):
        write_json_atomic(CONFIG_PATH, {"url": DEFAULTS["url"]})
        print(f"wrote {CONFIG_PATH} (edit it to change the keyboard address)")

    python = ensure_venv()
    write_plist(python)
    reload_agent()
    print(f"loaded launchd agent {LAUNCH_LABEL}")

    if with_hooks:
        settings = merge_hooks(load_settings_for_edit(), f"python3 {INSTALL_PATH} --hook")
        save_settings(settings)
        print(f"registered {len(HOOK_EVENTS)} hooks in {SETTINGS_PATH}")
        print("open a new session (or restart an existing one) for the hooks to load")
    else:
        print("skipped hook registration (--no-hooks); the daemon still works, but it")
        print("cannot tell a permission prompt from ongoing work")

    print(f"logs: {LOG_PATH}")


def uninstall():
    launchctl("bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}")
    launchctl("unload", LAUNCH_PLIST)
    for path in (LAUNCH_PLIST, INSTALL_PATH, STATE_PATH):
        try:
            os.unlink(path)
            print(f"removed {path}")
        except OSError:
            pass
    import shutil

    if os.path.isdir(VENV_PATH):
        shutil.rmtree(VENV_PATH, ignore_errors=True)
        print(f"removed {VENV_PATH}")

    if os.path.exists(SETTINGS_PATH):
        save_settings(strip_hooks(load_settings_for_edit()))
        print(f"removed hooks from {SETTINGS_PATH}")
    # ~/.claude/keyboard-status.json is yours; statusline-usage.json belongs to
    # claude-status-bar. Neither is ours to delete.
    print(f"left {CONFIG_PATH} in place")


# ------------------------------------------------------------------------- entries


def hook_main(config):
    """The in-session entry point. Fast, silent, and incapable of failing loudly.

    Claude Code treats a non-zero exit from most hooks as something worth telling
    the user about, and a hook that writes to stdout can perturb the session, so
    this swallows everything and always exits 0. The worst possible outcome is a
    keyboard that shows a slightly stale state for a few seconds.
    """
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            record_hook_event(payload, time.time(), config["session_ttl_seconds"])
    except Exception:  # noqa: BLE001 - deliberate: never disturb the session
        pass
    return 0


def preview(config, path, state=None):
    """Render one frame to a file and push nothing. For design work and for docs."""
    now = time.time()
    status = collect(now, config, allow_poll=False)
    if state:
        status.state = state
    image = render(status, now, config, phase=1.0)
    if path.lower().endswith((".jpg", ".jpeg")):
        with open(path, "wb") as handle:
            handle.write(encode(image, config))
    else:
        image.save(path)
    print(f"wrote {path} ({status.state}, {image.width}x{image.height})")
    return 0


def show_status(config):
    """Everything the renderer would see, as JSON. The first stop when debugging."""
    now = time.time()
    status = collect(now, config, allow_poll=False)
    print(json.dumps({
        key: value for key, value in vars(status).items()
    }, indent=2, default=str))
    return 0


USAGE = """usage: keyboard_status.py [command]

  --install [--no-hooks]  install to ~/.claude, create the venv, load the launchd
                          agent, and register the session hooks
  --uninstall             undo all of that
  --daemon                run the push loop in the foreground (what launchd runs)
  --once                  render and push a single frame, then exit
  --preview PATH [STATE]  render to PATH (.png or .jpg) without pushing; STATE is
                          one of working/waiting/idle/offline to force a pose
  --testcard [PATH]       render the resolution card -- geometry only, no state --
                          to PATH, or POST it to the panel when PATH is omitted
  --status                dump the resolved status as JSON
  --hook                  read a hook payload on stdin and record it (internal)
"""


def require_url(config):
    """Stop, loudly, before pushing frames at a placeholder.

    Returns an exit code when the address is unset and None when it is fine, so
    callers can `return code` without a second branch.
    """
    if URL_UNSET not in config["url"]:
        return None
    sys.stderr.write(
        "The keyboard's address is not set yet.\n"
        "\n"
        f"Edit {CONFIG_PATH} and set `url` to your panel's own address:\n"
        "\n"
        '    {"url": "http://192.168.1.50/image/upload"}\n'
        "\n"
        "Find it on the keyboard's own display or settings app, or look for a\n"
        "new device in your router's client list. Then restart the daemon:\n"
        "\n"
        f"    launchctl kickstart -k gui/{os.getuid()}/{LAUNCH_LABEL}\n")
    return 2


def main(argv):
    config = load_config()
    command = argv[1] if len(argv) > 1 else "--help"

    if command == "--hook":
        return hook_main(config)
    if command == "--install":
        install(with_hooks="--no-hooks" not in argv)
        return 0
    if command == "--uninstall":
        uninstall()
        return 0
    if command in ("--daemon", "--once"):
        code = require_url(config)
        if code is not None:
            return code
    if command == "--daemon":
        log(f"starting: {config['url']} every {config['tick_seconds']:g}s")
        return run_daemon(config)
    if command == "--once":
        return run_daemon(config, once=True)
    if command == "--preview":
        if len(argv) < 3:
            sys.stderr.write("--preview needs an output path\n")
            return 2
        return preview(config, argv[2], argv[3] if len(argv) > 3 else None)
    if command == "--testcard":
        image = testcard(config)
        if len(argv) > 2:
            image.save(argv[2])
            print(f"wrote {argv[2]} ({image.width}x{image.height})")
            return 0
        data = encode(image, config)
        ok, note = push(data, config)
        print(f"testcard: {len(data)} bytes at quality {config['jpeg_quality']} -> {note}")
        return 0 if ok else 1
    if command == "--status":
        return show_status(config)
    sys.stdout.write(USAGE)
    return 0 if command in ("--help", "-h") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))


