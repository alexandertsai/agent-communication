# agent-message

Simple tool for messaging between local Claude Code and Codex sessions: Claude to Codex, Claude to Claude, and Codex to Codex.

```sh
agent-message list
agent-message claude <session-id-or-name-or-pid> "Message text"
agent-message codex <thread-id-or-exact-name> "Message text"
```

`list` shows active sessions in the same project or repo as the current directory. Use `-` as the message to read from stdin.

Install: `ln -s "$PWD/agent_message.py" ~/.local/bin/agent-message`

Test: `python3 -m unittest -v`
