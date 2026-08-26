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

STATE_STYLE = {
    "working": ("WORKING", CLAY),
    "waiting": ("NEEDS YOU", WARN),
    "idle": ("IDLE", SLEEP),
    "offline": ("NO SESSION", FAINT),
}

# Supersampling factor for every vector shape. FreeType antialiases text for us;
# ImageDraw does not antialias anything, and a jagged mascot on a 142 px panel is
# immediately obvious. Drawing the shapes 4x and resampling down fixes it for the
# cost of one 568x1712 RGBA buffer per frame.
SS = 4

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


def write(draw, x, y, text, fonts, fill, align="left", max_width=None, spacing=0.0):
    """Draw a possibly-mixed-script string, optionally letterspaced. Returns width.

    `spacing` exists for the small uppercase section labels: PIL has no tracking
    control, and at 9 px an untracked all-caps label reads as one grey smudge.
    """
    if max_width is not None:
        text = elide(draw, text, fonts, max_width)
    width = measure(draw, text, fonts) + spacing * max(0, len(text) - 1)
    if align == "center":
        x -= width / 2
    elif align == "right":
        x -= width
    if spacing:
        for char in text:
            face = fonts[1 if _is_wide(char) else 0]
            draw.text((x, y), char, font=face, fill=fill)
            x += draw.textlength(char, font=face) + spacing
    else:
        for wide, chunk in runs(text):
            face = fonts[1 if wide else 0]
            draw.text((x, y), chunk, font=face, fill=fill)
            x += draw.textlength(chunk, font=face)
    return width


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
    if state == "working":
        for index in range(3):
            angle = -phase * 1.6 + index * 2 * math.pi / 3
            radius = size * 0.66
            sx, sy = cx + math.cos(angle) * radius, cy + math.sin(angle) * radius * 0.82
            twinkle = 0.45 + 0.55 * math.sin(phase * 2.2 + index * 2.1)
            arm = size * (0.038 + 0.030 * twinkle)
            tint = (247, 196, 150) + (int(120 + 135 * twinkle),)
            capsule(draw, (sx - arm, sy), (sx + arm, sy), size * 0.011, size * 0.011, tint)
            capsule(draw, (sx, sy - arm), (sx, sy + arm), size * 0.011, size * 0.011, tint)
    elif state == "waiting":
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
LABEL_SPACING = 1.2          # tracking for the small uppercase section labels
STATE_TRACKING = 1.0         # the state badge is set larger, so it needs less

# The top of the physical panel sits behind the device's own cutouts, so the
# first 40 rows are unusable -- not dim, not cropped, covered. Every coordinate
# below is derived from MASCOT_TOP so nothing can drift back up into them.
DEAD_ZONE = 40
MASCOT_TOP = 44              # first row the character is allowed to touch
MASCOT_SIZE = 100            # its outermost glow reaches MASCOT_REACH * this
MASCOT_REACH = 0.63          # matches the last ring draw_mascot paints
PILL_H = 28                  # state badge
BAR_H = 15                   # usage meter
FOOTER_H = 30                # rule, clock and reachability dot


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

    The layout carries five facts -- the character, the state, the model, and the
    two usage meters -- and nothing else. This is a keyboard strip read at arm's
    length in whatever light the desk has, so the panel is worth more as five
    things you can read in a glance than as nine things you have to lean in for.
    Everything starts below DEAD_ZONE, which the device's own cutouts own.
    """
    from PIL import Image, ImageDraw

    width, height = config["width"], config["height"]
    base = Image.new("RGB", (width, height), BG)
    shapes = Image.new("RGBA", (width * SS, height * SS), (0, 0, 0, 0))
    glyphs = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    vector = ImageDraw.Draw(shapes)
    draw = ImageDraw.Draw(glyphs)

    label_font = font(12, 700, rounded=False)
    model_font = font(22, 700)
    chip_font = font(11, 600, rounded=False)
    pct_font = font(24, 800)
    state_font = font(15, 800)
    clock_font = font(15, 600, rounded=False)

    inner = width - 2 * PAD
    word, tint = STATE_STYLE[status.state]

    # ------------------------------------------------------------------ mascot
    # MASCOT_REACH is how far the outermost glow ring gets from the centre, so
    # deriving the centre from it puts the character's first non-background pixel
    # on MASCOT_TOP exactly -- which is what keeps it clear of the cutouts.
    cx = width / 2
    radius = MASCOT_SIZE * MASCOT_REACH
    cy = MASCOT_TOP + radius
    draw_mascot(vector, cx * SS, cy * SS, MASCOT_SIZE * SS, status.state, phase)
    if status.state == "idle":
        # The one part of the character that is cheaper to set in type than to
        # draw from primitives, because it is literally a letter.
        write(draw, cx + 28, cy - 48, "z", font(20, 700), (128, 138, 152))
        write(draw, cx + 43, cy - 64, "z", font(14, 700), (110, 120, 134))

    # ------------------------------------------- state badge under the character
    pill_w = min(inner, measure(draw, word, state_font) + STATE_TRACKING * (len(word) - 1) + 22)
    pill_y = cy + radius + 14
    vector.rounded_rectangle(
        [(cx - pill_w / 2) * SS, pill_y * SS, (cx + pill_w / 2) * SS, (pill_y + PILL_H) * SS],
        radius=PILL_H / 2 * SS, fill=tint + (36,), outline=tint + (160,), width=SS,
    )
    write(draw, cx, pill_y + 6, word, state_font, tint, align="center", spacing=STATE_TRACKING)

    # ------------------------------------------------------------------- model
    # The effort chip rides on the label row rather than beside the model name:
    # at 22 px "Sonnet 5" alone is most of the panel's width, and the label row is
    # empty on the right anyway.
    cursor = pill_y + PILL_H + 16
    write(draw, PAD, cursor, "MODEL", label_font, DIM, spacing=LABEL_SPACING)
    if status.effort:
        chip_w = measure(draw, status.effort, chip_font) + 14
        chip_x = width - PAD - chip_w
        vector.rounded_rectangle(
            [chip_x * SS, (cursor - 2) * SS, (width - PAD) * SS, (cursor + 16) * SS],
            radius=9 * SS, fill=CLAY_DEEP + (70,), outline=CLAY_DEEP + (200,), width=SS,
        )
        write(draw, chip_x + 7, cursor + 1, status.effort, chip_font, CLAY)
    cursor += 15
    write(draw, PAD, cursor, status.model or "--", model_font,
          INK if status.model else DIM, max_width=inner)
    cursor += 26

    # ------------------------------------------------------------------- usage
    # Percentage and bar only. A window's reset time is the one usage fact that
    # cannot be set large enough to read here, so it lives in `--status` instead
    # of stealing a row from the two that can.
    for label, data in (("5 HR", status.five_hour), ("WEEK", status.seven_day)):
        cursor += 16
        if data is None:
            pct, colour, text = 0.0, FAINT, "--"
        else:
            pct = max(0.0, min(100.0, data["used_percentage"]))
            colour = usage_color(pct)
            text = f"{pct:.0f}%"
        write(draw, PAD, cursor, label, label_font, DIM, spacing=LABEL_SPACING)
        write(draw, width - PAD, cursor - 6, text, pct_font, colour, align="right")
        bar_y = cursor + 26
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
    # Clock and a reachability dot. The "how long since Claude last did anything"
    # stamp that used to sit here was 9 px of the least glanceable fact on the
    # panel -- the character already says whether anything is happening.
    footer_top = height - FOOTER_H
    vector.rectangle(
        [PAD * SS, footer_top * SS, (width - PAD) * SS, footer_top * SS + SS], fill=RULE + (255,))
    write(draw, PAD, footer_top + 7, time.strftime("%H:%M", time.localtime(now)), clock_font, DIM)
    dot = CLAY if status.online else FAINT
    vector.ellipse(
        [(width - PAD - 9) * SS, (footer_top + 12) * SS,
         (width - PAD) * SS, (footer_top + 21) * SS], fill=dot + (255,))

    # Shapes first, then type, so the pill outlines and bars sit *behind* their
    # labels no matter what order the layout drew them in.
    flattened = shapes.resize((width, height), Image.LANCZOS)
    base.paste(flattened, (0, 0), flattened)
    base.paste(glyphs, (0, 0), glyphs)
    return base


# ---------------------------------------------------------------------------- push


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
  --status                dump the resolved status as JSON
  --hook                  read a hook payload on stdin and record it (internal)
"""


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
    if command == "--status":
        return show_status(config)
    sys.stdout.write(USAGE)
    return 0 if command in ("--help", "-h") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
