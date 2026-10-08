"""Durable, append-only publication state; never force-push or retry Telegram."""
import copy
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


class StateError(RuntimeError):
    pass


def merge_state(remote, local):
    merged = copy.deepcopy(remote)
    files = merged.setdefault("files", {})
    for key, entry in local.get("files", {}).items():
        previous = files.get(key)
        if previous is not None and previous != entry:
            same_attempt = entry.get("attempt_id") and entry.get("attempt_id") == previous.get("attempt_id")
            if not (same_attempt and previous.get("status") == "pending"
                    and entry.get("status") == "published"):
                raise StateError("Conflicting publication record: " + key)
        files[key] = entry
    # A reservation from another run must block all further sends.
    own_pending = {k for k, v in local.get("files", {}).items() if v.get("status") == "pending"}
    if any(v.get("status") == "pending" and k not in own_pending for k, v in files.items()):
        raise StateError("Unresolved remote publication; manual reconciliation required")
    merged["updated"] = max(remote.get("updated", ""), local.get("updated", ""))
    return merged


def atomic_write(path, state):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as fh:
        tmp = fh.name
        try:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        except BaseException:
            os.unlink(tmp)
            raise
    os.replace(tmp, path)


def persist(path, state, attempts=5):
    """Build each state-only commit on latest main using an isolated Git index."""
    path = Path(path).resolve()
    def git(*args, data=None, env=None):
        return subprocess.run(["git", *args], cwd=path.parent, input=data,
                              text=True, capture_output=True, check=True,
                              timeout=60, env=env).stdout.strip()
    try:
        root = Path(git("rev-parse", "--show-toplevel"))
        relative = path.relative_to(root).as_posix()
        env = dict(os.environ, GIT_AUTHOR_NAME="b24-autopost-bot",
                   GIT_AUTHOR_EMAIL="b24-autopost-bot@users.noreply.github.com",
                   GIT_COMMITTER_NAME="b24-autopost-bot",
                   GIT_COMMITTER_EMAIL="b24-autopost-bot@users.noreply.github.com")
        for attempt in range(attempts):
            try:
                git("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
                parent = git("rev-parse", "refs/remotes/origin/main")
                remote = json.loads(git("show", parent + ":" + relative))
                merged = merge_state(remote, state)
                if merged == remote:
                    atomic_write(path, merged)
                    state.clear()
                    state.update(merged)
                    return
                with tempfile.TemporaryDirectory() as tmp:
                    index_env = dict(env, GIT_INDEX_FILE=os.path.join(tmp, "index"))
                    git("read-tree", parent, env=index_env)
                    blob = git("hash-object", "-w", "--stdin",
                               data=json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
                    git("update-index", "--add", "--cacheinfo", "100644", blob, relative, env=index_env)
                    tree = git("write-tree", env=index_env)
                    commit = git("commit-tree", tree, "-p", parent,
                                 data="state: durable publication checkpoint\n", env=env)
                    git("push", "origin", commit + ":refs/heads/main")
                atomic_write(path, merged)
                state.clear()
                state.update(merged)
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                # Re-fetch also reconciles a push that succeeded but lost its response.
                if attempt + 1 == attempts:
                    raise StateError("State push failed after bounded retries")
                time.sleep(1)
    except StateError:
        raise
    except Exception as exc:
        raise StateError("Cannot persist publication state") from exc
