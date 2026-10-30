# Check the user's setup

1. Check which CLIs the user has: `codex`, `claude`, and `agy`. Skip anything that isn't installed. Run `agent-message list` to find other sessions, including multiple sessions of the same kind. If there isn't another session, ask the user to open one in the same project.

2. Have the sessions pick different names with `agent-message name <role>`. Send a short test message and get a reply. Repeat in the other direction. Test the agent combinations the user has, including same-kind sessions. If they use both normal and bypass modes, check a mixed-mode pair too.

3. Check that sending and replying don't keep asking the user for approval. Use the messaging rules in the README. If messages work both ways without repeated prompts, setup is done. Say which combinations were checked and which weren't available.

## If something fails

- Command not found: check the symlink and the agent's PATH.
- No peers: check that both sessions are active in the same project and have started a conversation.
- Codex sandbox error: check the installed `agent-message` rule and run the command outside the sandbox through the normal approval mechanism.
- Claude holds a message: check `crossSessionInbound: accept` in its settings.
- Repeated approval prompts: check the messaging allow rule. Existing ask or deny rules can override it. Keep the messaging command separate from unrelated shell commands.
- Antigravity sandbox error: use its messaging allow rule for execution outside the sandbox. Business or enterprise clients may also need `unsandboxed(agent-message)` in the allow list.
- Antigravity queues a message but doesn't reply: its file inbox is read on the next turn. Have the recipient read pending messages. Running `agent-message name <role>` inside Antigravity registers available connection details for API delivery.
- A native CLI command isn't supported: check that client's version and command help.

`posted` or `queued` alone isn't a successful test. YOU MUST GET A REPLY.

For changes to the bridge itself, also run `python3 -m unittest -v`.
