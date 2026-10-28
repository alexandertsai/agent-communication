import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import agent_message as bridge


class BridgeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
