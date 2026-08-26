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

DEFAULTS = {
    # Where the keyboard lives. Override in ~/.claude/keyboard-status.json or
    # with CLAUDE_KEYBOARD_URL; the whole point is that this is not compiled in.
    "url": "http://192.168.0.12/image/upload",
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
    "jpeg_quality": 88,
    # A session whose transcript last moved within this many seconds counts as
    # actively working when no hook has said otherwise.
    "active_seconds": 45.0,
    # Sessions quieter than this stop being "the current session" at all.
    "session_ttl_seconds": 6 * 3600.0,
    "usage_poll_seconds": 60.0,
    "show_task_title": True,
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
        token = sub_dict(read_json(CREDENTIALS_PATH), "claudeAiOauth").get("accessToken")
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
        entry = {"state": state, "event": event, "at": now}
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            entry["cwd"] = cwd
        transcript = payload.get("transcript_path")
        if isinstance(transcript, str) and transcript:
            entry["transcript"] = transcript
        if message:
            entry["message"] = message[:200]
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
        if config["show_task_title"]:
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
