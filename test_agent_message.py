from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import agent_message as bridge


class BridgeTests(unittest.TestCase):
    def make_agy_database(self, directory, workspaces):
        with closing(sqlite3.connect(Path(directory) / "conversation_summaries.db")) as db, db:
            db.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, workspace_uris TEXT, killed INTEGER, last_modified_time INTEGER)")
            db.executemany("INSERT INTO conversation_summaries VALUES (?, 'worker', ?, 0, 1)", workspaces)

    def test_agy_malformed_workspaces_do_not_hide_valid_sessions(self):
        invalid = ["invalid json", "null", "42", '{}', '"file:///project"', '[null, 42, {}]']
        with tempfile.TemporaryDirectory() as directory:
            self.make_agy_database(directory, [
                (f"bad-{index}", value) for index, value in enumerate(invalid)
            ] + [("valid", '[null, "file:///project", 42]')])
            with patch.object(bridge, "AGY_HOME", Path(directory)):
                sessions = bridge.agy_sessions()
                valid = next(row for row in sessions if row["id"] == "valid")
                self.assertEqual(valid["cwd"], "/project")
                self.assertEqual(valid["cwds"], ["/project"])
                self.assertTrue(all(row["cwd"] is None for row in sessions if row["id"].startswith("bad-")))

    def test_agy_peer_matches_any_workspace_once(self):
        for other_clone in (False, True):
            with self.subTest(other_clone=other_clone), tempfile.TemporaryDirectory() as directory:
                workspaces = '["file:///unrelated", "file:///shared%20project", "file:///shared%20project/subdir"]'
                self.make_agy_database(directory, [("conv-1", workspaces)])
                def root(cwd):
                    if cwd.startswith("/shared project"):
                        return "/other-clone" if other_clone else "/mine"
                    return "/unrelated" if cwd == "/unrelated" else "/mine"
                with patch.object(bridge, "AGY_HOME", Path(directory)), \
                        patch.object(bridge, "sender", return_value={}), \
                        patch.object(bridge, "project_root", side_effect=root), \
                        patch.object(bridge, "origin_url", side_effect=lambda cwd: "repo" if cwd != "/unrelated" else "other"), \
                        patch.object(bridge, "claude_sessions", return_value=[]), \
                        patch.object(bridge, "codex_sessions", return_value=[]), \
                        patch.object(bridge, "open_rollouts", return_value={str(Path(directory) / "presence/conv-1.lock")}):
                    reason = "same repo, other clone" if other_clone else "same project"
                    self.assertEqual(bridge.peers(), [f"agy  worker  conv-1  {reason}"])

    def test_routes_each_sender_and_mode_to_each_recipient(self):
        target = {"id": "target", "sessionId": "target", "name": "recipient", "lock_path": "/lock"}
        for provider in ("codex", "claude", "agy"):
            for mode in ("prompting", "bypass"):
                identity = {"name": f"sender ({provider})", "session": "source", "mode": mode}
                for recipient in ("codex", "claude", "agy", "antigravity"):
                    with self.subTest(sender=provider, mode=mode, recipient=recipient), \
                            patch.object(bridge.sys, "argv", ["agent-message", recipient, "recipient", "hello"]), \
                            patch.object(bridge, "sender", return_value=identity), \
                            patch.object(bridge, "open_rollouts", return_value={"/lock"}), \
                            patch.object(bridge, "claude_sessions", return_value=[target]), \
                            patch.object(bridge, "agy_sessions", return_value=[target]), \
                            patch.object(bridge, "resolve_codex", return_value="target"), \
                            patch.object(bridge, "send_codex") as codex, \
                            patch.object(bridge, "send_agy") as agy, \
                            patch.object(bridge, "post_claude") as claude, patch("builtins.print"):
                        bridge.main()
                        if recipient == "claude":
                            claude.assert_called_once()
                            content = claude.call_args.args[1]["message"]["content"]
                            self.assertIn(f'from-mode="{mode}"', content)
                            self.assertIn(identity["name"], content)
                            self.assertIn("hello", content)
                        else:
                            delivery = codex if recipient == "codex" else agy
                            delivery.assert_called_once_with("target", f'From {identity["name"]}\nhello')
                        self.assertEqual(codex.call_count + agy.call_count + claude.call_count, 1)

    def test_install_codex_rules_preserves_other_rules_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, "CODEX_HOME", Path(directory)):
            rules = Path(directory) / "rules"
            rules.mkdir()
            existing = rules / "default.rules"
            existing.write_text('prefix_rule(pattern=["other"], decision="prompt")\n')
            with patch("builtins.print"):
                bridge.install_codex_rules()
                destination = rules / "agent-message.rules"
                installed = destination.read_text()
                timestamp = destination.stat().st_mtime_ns
                bridge.install_codex_rules()
            self.assertEqual(installed, 'prefix_rule(\n    pattern = ["agent-message"],\n    decision = "allow",\n)\n')
            self.assertEqual(destination.stat().st_mtime_ns, timestamp)
            self.assertEqual(existing.read_text(), 'prefix_rule(pattern=["other"], decision="prompt")\n')

    def test_install_codex_rules_refuses_to_replace_custom_rule(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, "CODEX_HOME", Path(directory)):
            destination = Path(directory) / "rules/agent-message.rules"
            destination.parent.mkdir()
            destination.write_text("custom rule\n")
            with self.assertRaisesRegex(ValueError, "different content"):
                bridge.install_codex_rules()
            self.assertEqual(destination.read_text(), "custom rule\n")

    def test_install_codex_rules_does_not_require_peer_discovery(self):
        with patch.object(bridge.sys, "argv", ["agent-message", "install-codex-rules"]), \
                patch.object(bridge, "install_codex_rules") as install, \
                patch.object(bridge, "sender") as sender:
            bridge.main()
            install.assert_called_once_with()
            sender.assert_not_called()

    def test_codex_uses_daemon_without_changing_approval_settings(self):
        with patch.dict(bridge.os.environ, {}, clear=True), patch.object(Path, "exists", return_value=True), patch.object(bridge.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "Queued message"
            self.assertEqual(bridge.send_codex("thread", "hello"), "Queued message")
            command = run.call_args.args[0]
            self.assertEqual(command, ["codex", "queue", "--remote", "unix://", "--thread", "thread", "--message", "hello"])

    def test_ambiguous_name_requires_id(self):
        sessions = [{"name": "worker", "sessionId": "one", "pid": 1}, {"name": "worker", "sessionId": "two", "pid": 2}]
        with self.assertRaises(ValueError):
            bridge.resolve_claude("worker", sessions)
        self.assertEqual(bridge.resolve_claude("two", sessions)["pid"], 2)

    def test_frame_preserves_message_and_labels_sender(self):
        frame = bridge.frame_for('hello\n世界 "quoted"', {"name": "Codex", "session": "abc", "mode": "prompting"}, "message-id")
        self.assertEqual(frame["priority"], "next")
        self.assertIn('from-mode="prompting"', frame["message"]["content"])
        self.assertIn('hello\n世界 "quoted"', frame["message"]["content"])
        self.assertNotIn("from", frame)

    def test_unknown_permission_mode_is_not_claimed(self):
        frame = bridge.frame_for("hello", {"name": "bridge"}, "id")
        self.assertNotIn("from-mode", frame["message"]["content"])

    def test_socket_receives_exactly_one_json_line(self):
        with tempfile.TemporaryDirectory(prefix="bridge-", dir="/tmp") as directory:
            path = Path(directory) / "inbox.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                listener.listen(1)
                frame = bridge.frame_for("message", {"name": "bridge"}, "id")
                bridge.post_claude({"messagingSocketPath": str(path)}, frame)
                connection, _ = listener.accept()
                with connection:
                    raw = connection.makefile("rb").read()
                self.assertEqual(raw.count(b"\n"), 1)
                self.assertEqual(json.loads(raw), frame)

    def test_message_over_byte_limit_is_rejected(self):
        with patch.object(bridge.sys, "argv", ["agent-message", "codex", "thread", "é" * 1001]), patch.object(bridge, "send_codex") as send:
            with self.assertRaises(ValueError):
                bridge.main()
            send.assert_not_called()

    def test_symlink_socket_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="bridge-", dir="/tmp") as directory:
            path = Path(directory) / "inbox.sock"
            alias = Path(directory) / "alias.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                alias.symlink_to(path)
                with self.assertRaises(ValueError):
                    bridge.post_claude({"messagingSocketPath": str(alias)}, {})

    def test_claude_name_registry(self):
        live = {"sessionId": "me", "pid": 1, "name": "repo-1a", "nameSource": "derived", "messagingSocketPath": "/s"}
        other = {"sessionId": "you", "pid": 2, "name": "pinned", "nameSource": "user", "messagingSocketPath": "/s"}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bridge, "NAMES", Path(directory) / "names.json"), \
                patch.object(bridge, "sender", return_value={"session": "me"}), \
                patch.object(bridge, "open_rollouts", return_value=set()), \
                patch.object(bridge, "codex_sessions", return_value=[]), \
                patch.object(bridge, "agy_sessions", return_value=[]), \
                patch.object(bridge, "claude_sessions", side_effect=lambda: [dict(live, name=bridge.claude_names().get("me", "repo-1a")), other]):
            bridge.name_claude("reviewer")
            self.assertEqual(bridge.claude_names(), {"me": "reviewer"})
            with self.assertRaises(ValueError):
                bridge.name_claude("pinned")

    def test_user_set_name_beats_registry(self):
        rows = [{"sessionId": "a", "pid": 1, "name": "auto", "nameSource": "derived", "messagingSocketPath": "/s"},
                {"sessionId": "b", "pid": 1, "name": "mine", "nameSource": "user", "messagingSocketPath": "/s"}]
        with tempfile.TemporaryDirectory() as directory:
            names = Path(directory) / "names.json"
            names.write_text(json.dumps({"a": "reviewer", "b": "ignored"}))
            for row in rows:
                (Path(directory) / f'{row["sessionId"]}.json').write_text(json.dumps(row))
            with patch.object(bridge, "NAMES", names), patch.object(bridge, "CLAUDE_HOME", Path(directory).parent), \
                    patch.object(Path, "glob", return_value=[Path(directory) / "a.json", Path(directory) / "b.json"]), \
                    patch.object(bridge.os, "kill"), patch.object(Path, "lstat") as lstat, patch.object(bridge.stat, "S_ISSOCK", return_value=True):
                lstat.return_value.st_uid = bridge.os.getuid()
                self.assertEqual([row["name"] for row in bridge.claude_sessions()], ["reviewer", "mine"])

    def test_agy_peer_in_other_clone_is_listed(self):
        row = {"id": "conv-1", "name": "worker", "cwd": "/other/clone", "lock_path": "/lock"}
        with patch.object(bridge, "sender", return_value={}), \
                patch.object(bridge, "project_root", side_effect=lambda cwd: "/other/clone" if cwd == "/other/clone" else "/mine"), \
                patch.object(bridge, "origin_url", return_value="git@example.com:repo.git"), \
                patch.object(bridge, "claude_sessions", return_value=[]), \
                patch.object(bridge, "codex_sessions", return_value=[]), \
                patch.object(bridge, "agy_sessions", return_value=[row]), \
                patch.object(bridge, "open_rollouts", return_value={"/lock"}):
            self.assertEqual(bridge.peers(), ["agy  worker  conv-1  same repo, other clone"])

    def test_agy_uses_agentapi_when_ls_address_present(self):
        with tempfile.TemporaryDirectory() as directory:
            presence = Path(directory) / "presence"
            presence.mkdir()
            (presence / "conv-1.lock").write_text(json.dumps({"ANTIGRAVITY_LS_ADDRESS": "localhost:1234", "ANTIGRAVITY_CSRF_TOKEN": "tok"}))
            with patch.dict(bridge.os.environ, {}, clear=True), patch.object(bridge, "AGY_HOME", Path(directory)), patch.object(bridge.subprocess, "run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = '{"response":{"sendMessage":{}}}'
                self.assertEqual(bridge.send_agy("conv-1", "hello"), '{"response":{"sendMessage":{}}}')
                self.assertEqual(run.call_args.args[0], ["agy", "agentapi", "send-message", "conv-1", "hello"])
                self.assertEqual(run.call_args.kwargs["env"]["ANTIGRAVITY_LS_ADDRESS"], "localhost:1234")
                self.assertEqual(run.call_args.kwargs["env"]["ANTIGRAVITY_CSRF_TOKEN"], "tok")

    def test_agy_falls_back_to_file_inbox_without_ls_address(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(bridge.os.environ, {}, clear=True), \
                patch.object(bridge, "AGY_HOME", Path(directory)), \
                patch.object(bridge, "sender", return_value={"session": "sender-1"}):
            res = json.loads(bridge.send_agy("conv-1", "hello agy"))
            self.assertEqual(res["status"], "queued")
            inbox = Path(directory) / "brain/conv-1/.system_generated/messages"
            self.assertTrue((inbox / "undelivered" / res["message_id"]).exists())
            payload = json.loads((inbox / f'{res["message_id"]}.json').read_text())
            self.assertEqual(payload["content"], "hello agy")
            self.assertEqual(payload["sender"], "sender-1")

    def test_permission_mode_reads_nearest_agent_argv(self):
        self.assertEqual(bridge.permission_mode([(1, ["zsh"]), (2, ["agy", "--dangerously-skip-permissions"])]), "bypass")
        self.assertEqual(bridge.permission_mode([(1, ["/x/claude", "--permission-mode", "bypassPermissions"])]), "bypass")
        self.assertEqual(bridge.permission_mode([(1, ["/x/claude", "--session-id", "a"])]), "prompting")
        self.assertIsNone(bridge.permission_mode([(1, ["zsh"])]))

    def test_permission_mode_accepts_native_equals_flags(self):
        for argv in (["claude", "--permission-mode=bypassPermissions"], ["agy", "--dangerously-skip-permissions=true"]):
            with self.subTest(argv=argv):
                self.assertEqual(bridge.permission_mode([(1, argv)]), "bypass")
        self.assertEqual(bridge.permission_mode([(1, ["agy", "--dangerously-skip-permissions=false"])]), "prompting")

    def test_permission_mode_does_not_inherit_outer_agent_bypass(self):
        chain = [(1, ["zsh"]), (2, ["claude"]), (3, ["agy", "--dangerously-skip-permissions"])]
        self.assertEqual(bridge.permission_mode(chain), "prompting")

    def test_codex_env_drops_cmux_shims(self):
        with patch.dict(bridge.os.environ, {"PATH": "/tmp/cmux-cli-shims/a:/usr/bin"}):
            self.assertEqual(bridge.codex_env()["PATH"], "/usr/bin")

    def test_resolve_codex_prefers_active_session(self):
        rows = [{"id": "old", "name": "w", "rollout_path": "/old"}, {"id": "new", "name": "w", "rollout_path": "/new"}]
        with patch.object(bridge, "codex_sessions", return_value=rows), patch.object(bridge, "open_rollouts", return_value={"/new"}):
            self.assertEqual(bridge.resolve_codex("w"), "new")
            self.assertEqual(bridge.resolve_codex("new"), "new")

    def test_codex_rejects_inactive_and_unknown_targets_before_delivery(self):
        rows = [{"id": "old", "name": "previous", "rollout_path": "/old"}]
        for target in ("old", "previous", "unknown"):
            with self.subTest(target=target), \
                    patch.object(bridge.sys, "argv", ["agent-message", "codex", target, "hello"]), \
                    patch.object(bridge, "sender", return_value={"name": "sender"}), \
                    patch.object(bridge, "codex_sessions", return_value=rows), \
                    patch.object(bridge, "open_rollouts", return_value=set()), \
                    patch.object(bridge, "send_codex") as send:
                with self.assertRaisesRegex(ValueError, "found 0"):
                    bridge.main()
                send.assert_not_called()

    def test_resolve_codex_rejects_ambiguous_active_name(self):
        rows = [{"id": "one", "name": "worker", "rollout_path": "/one"}, {"id": "two", "name": "worker", "rollout_path": "/two"}]
        with patch.object(bridge, "codex_sessions", return_value=rows), \
                patch.object(bridge, "open_rollouts", return_value={"/one", "/two"}):
            with self.assertRaisesRegex(ValueError, "found 2"):
                bridge.resolve_codex("worker")

    def test_rename_codex_rejects_active_duplicate(self):
        rows = [{"id": "me", "name": "me", "rollout_path": "/me"}, {"id": "other", "name": "worker", "rollout_path": "/other"}]
        with patch.dict(bridge.os.environ, {"CODEX_THREAD_ID": "me"}), \
                patch.object(bridge, "open_rollouts", return_value={"/me", "/other"}), \
                patch.object(bridge, "claude_sessions", return_value=[]), \
                patch.object(bridge, "agy_sessions", return_value=[]), \
                patch.object(bridge, "codex_sessions", return_value=rows), \
                patch.object(bridge, "app_server_rename") as rename:
            with self.assertRaisesRegex(ValueError, "taken"):
                bridge.rename_codex("worker")
            rename.assert_not_called()

    def test_ensure_name_free_ignores_self_and_inactive(self):
        rows = [{"id": "me", "name": "worker", "rollout_path": "/me"}, {"id": "old", "name": "worker", "rollout_path": "/old"}]
        with patch.object(bridge, "open_rollouts", return_value={"/me"}), \
                patch.object(bridge, "claude_sessions", return_value=[]), \
                patch.object(bridge, "agy_sessions", return_value=[]), \
                patch.object(bridge, "codex_sessions", return_value=rows):
            bridge.ensure_name_free("worker", "me")


if __name__ == "__main__":
    unittest.main()
