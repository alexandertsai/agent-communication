#!/usr/bin/env python3
"""Send messages through Claude's native inbox and Codex's native queue."""

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import socket
import sqlite3
import stat
import subprocess
import sys
import uuid


CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
MAX_FRAME = 1_000_000
MAX_MESSAGE_BYTES = 2000
NAMES = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agent-message/names.json"


def claude_names():
    try:
        return json.loads(NAMES.read_text())
    except (OSError, ValueError):
        return {}


def claude_sessions():
    names = claude_names()
    sessions = []
    for path in (CLAUDE_HOME / "sessions").glob("*.json"):
        try:
            row = json.loads(path.read_text())
            os.kill(int(row["pid"]), 0)
            info = Path(row["messagingSocketPath"]).lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                if row.get("nameSource") == "derived" and names.get(row["sessionId"]):
                    row["name"] = names[row["sessionId"]]
                sessions.append(row)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sessions


def codex_sessions(limit=50):
    databases = sorted(
        CODEX_HOME.glob("state_*.sqlite"),
        key=lambda p: int(re.search(r"state_(\d+)", p.name).group(1)),
        reverse=True,
    )
    if not databases:
        return []
    with sqlite3.connect(databases[0].as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT id, name, cwd, approval_mode, rollout_path, git_origin_url FROM threads "
            "WHERE archived = 0 ORDER BY updated_at DESC LIMIT ?", (limit,)
        )]


def open_rollouts():
    result = subprocess.run(["lsof", "-nP", "-Fn", "-u", str(os.getuid())], capture_output=True, text=True)
    sessions = str(CODEX_HOME / "sessions")
    return {line[1:] for line in result.stdout.splitlines() if line.startswith("n" + sessions)}


def project_root(cwd, cache={}):
    if cwd not in cache:
        result = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        cache[cwd] = result.stdout.strip() if result.returncode == 0 else cwd
    return cache[cwd]


def origin_url(cwd):
    result = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], capture_output=True, text=True)
    return result.stdout.strip() or None


def relevance(mine, mine_origin, cwd, origin=None):
    if not cwd:
        return None
    if project_root(cwd) == mine:
        return "same project"
    if mine_origin and origin and mine_origin == origin:
        return "same repo, other clone"
    return None


def peers():
    identity = sender()
    mine = project_root(os.getcwd())
    mine_origin = origin_url(mine)
    rows = []
    for row in claude_sessions():
        if row["sessionId"] == identity.get("session"):
            continue
        reason = relevance(mine, mine_origin, row.get("cwd"))
        if reason:
            rows.append(f'claude  {row.get("name") or "-"}  {row["sessionId"]}  {row.get("status") or "-"}  {reason}')
    active = open_rollouts()
    for row in codex_sessions(10000):
        if row["id"] == identity.get("session") or row["rollout_path"] not in active:
            continue
        reason = relevance(mine, mine_origin, row.get("cwd"), row.get("git_origin_url"))
        if reason:
            rows.append(f'codex  {row.get("name") or "-"}  {row["id"]}  {reason}')
    return rows


def resolve_claude(target, sessions):
    matches = [row for row in sessions if target in (
        row.get("sessionId"), row.get("name"), str(row.get("pid"))
    )]
    if len(matches) != 1:
        raise ValueError(f"Expected one Claude session for {target!r}; found {len(matches)}. Run agent-message list.")
    return matches[0]


def sender():
    thread = os.environ.get("CODEX_THREAD_ID")
    if thread:
        rows = codex_sessions(10000)
        row = next((row for row in rows if row["id"] == thread), None)
        mode = None if row is None else "bypass" if row["approval_mode"] == "never" else "prompting"
        name = row and row["name"]
        address = name if name and sum(r["name"] == name for r in rows) == 1 else thread
        return {"name": f"{address} (codex)", "session": thread, "mode": mode, "address": address}
    parents = set()
    pid = os.getpid()
    for _ in range(24):
        parents.add(pid)
        result = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
        if not result.stdout.strip():
            break
        pid = int(result.stdout.strip())
        if pid <= 1:
            break
    sessions = claude_sessions()
    own = next((row for row in sessions if row["pid"] in parents), None)
    if own:
        name = own.get("name")
        address = name if name and sum(row.get("name") == name for row in sessions) == 1 else own["sessionId"]
        return {"name": f"{address} (claude)", "session": own["sessionId"], "socket": own["messagingSocketPath"], "address": address}
    return {"name": "Local agent bridge"}


def frame_for(message, identity, message_id):
    attrs = []
    if identity.get("socket"):
        attrs.append(f'from="uds:{identity["socket"]}"')
    if identity.get("session"):
        attrs.append(f'from-session="{identity["session"]}"')
    name = re.sub(r'["<>\x00-\x1f\x7f]', "", identity["name"])[:64]
    attrs.append(f'from-name="{name}"')
    if identity.get("mode"):
        attrs.append(f'from-mode="{identity["mode"]}"')
    content = f'<cross-session-message {" ".join(attrs)}>\n{message.replace("<cross-session-message", "＜cross-session-message").replace("</cross-session-message", "＜/cross-session-message")}\n</cross-session-message>'
    frame = {"type": "user", "message": {"role": "user", "content": content}, "priority": "next", "msg_id": message_id}
    if identity.get("socket"):
        frame["from"] = f'uds:{identity["socket"]}'
    return frame


def post_claude(target, frame):
    path = Path(target["messagingSocketPath"])
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Target must be a socket owned by the current user.")
    payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode()
    if len(payload) > MAX_FRAME:
        raise ValueError("Message frame is too large; send a file path instead.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)


def send_codex(target, message):
    command = ["codex", "queue"]
    remote = os.environ.get("AGENT_MESSAGE_CODEX_REMOTE")
    if remote is None and (CODEX_HOME / "app-server-control/app-server-control.sock").exists():
        remote = "unix://"
    if remote:
        command.extend(["--remote", remote])
    command.extend(["--thread", target, "--message", message])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "codex queue failed")
    return result.stdout.strip()


def app_server_rename(command, thread, name):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def call(request_id, method, params):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.select(10):
                line = process.stdout.readline()
                if not line:
                    break
                data = json.loads(line)
                if data.get("id") == request_id:
                    if "error" in data:
                        raise ValueError(data["error"].get("message", "app-server error"))
                    return data["result"]
        raise ValueError("app-server did not respond")

    try:
        call(1, "initialize", {"clientInfo": {"name": "agent-message", "version": "1"}})
        process.stdin.write('{"jsonrpc": "2.0", "method": "initialized"}\n')
        process.stdin.flush()
        call(2, "thread/name/set", {"threadId": thread, "name": name})
        return call(3, "thread/read", {"threadId": thread, "includeTurns": False})["thread"].get("name")
    finally:
        process.terminate()


def rename_codex(name):
    thread = os.environ.get("CODEX_THREAD_ID")
    if not thread:
        raise ValueError("Not a Codex session")
    commands = [["codex", "app-server", "--stdio"]]
    if (CODEX_HOME / "app-server-control/app-server-control.sock").exists():
        commands.insert(0, ["codex", "app-server", "proxy"])
    error = None
    for command in commands:
        try:
            if app_server_rename(command, thread, name) == name:
                return
        except (OSError, ValueError) as caught:
            error = caught
    raise ValueError(f"Rename failed: {error}" if error else "Rename failed")


def name_claude(name):
    session = sender().get("session")
    if not session:
        raise ValueError("Not inside a Claude or Codex session")
    active = open_rollouts()
    taken = [row for row in claude_sessions() if row["sessionId"] != session and row.get("name") == name]
    taken += [row for row in codex_sessions(10000) if row["name"] == name and row["rollout_path"] in active]
    if taken:
        raise ValueError(f"Name {name!r} is taken by an active session")
    names = claude_names()
    names[session] = name
    NAMES.parent.mkdir(parents=True, exist_ok=True)
    temp = NAMES.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(names, indent=2))
    os.replace(temp, NAMES)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List active Claude and Codex sessions in the same project or repo")
    commands.add_parser("name", help="Name the current session").add_argument("name")
    for kind in ("claude", "codex"):
        command = commands.add_parser(kind, help=f"Send a message to {kind}")
        command.add_argument("target", help="Exact session ID or name; Claude also accepts PID")
        command.add_argument("message", help="Message text, or - to read stdin")
    args = parser.parse_args()
    if args.command == "list":
        print("\n".join(peers()) or "No relevant peers")
        return
    if args.command == "name":
        name = args.name.strip()
        if not name:
            raise ValueError("Name is empty")
        rename_codex(name) if os.environ.get("CODEX_THREAD_ID") else name_claude(name)
        print(f"Named {name}")
        return
    message = sys.stdin.read() if args.message == "-" else args.message
    if not message.strip():
        raise ValueError("Message is empty")
    if len(message.encode()) > MAX_MESSAGE_BYTES:
        raise ValueError("Message too long")
    identity = sender()
    if args.command == "codex":
        print(send_codex(args.target, f'From {identity["name"]}\n{message}'))
    else:
        target = resolve_claude(args.target, claude_sessions())
        message_id = str(uuid.uuid4())
        post_claude(target, frame_for(message, identity, message_id))
        print(json.dumps({"status": "posted", "message_id": message_id, "session_id": target["sessionId"], "note": "Posted to native inbox; recipient inbound rules still apply. A reply confirms processing."}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(f"agent-message: {error}", file=sys.stderr)
        sys.exit(1)
