# Golden replicas (synthetic)

These transcripts are **synthetic**. Each one replicates the shape of a real conformance session: the same record
types, order, nesting and counts. Nothing in them comes from a real conversation.

- All free text (prompts, answers, tool output, instructions) is placeholder text of similar length.
- Ids (sessions, calls, messages, agents, users) are regenerated; paths are `~/project/...`.
- Shell commands are replaced by `python3 -m pytest -q` (a verify run) or `ls`, keeping their class.
- The skill list is `hello` plus `skill-NN` placeholders. Skill names such as `hello`, `explicit-only`,
  `explicit-agents` and `mango-greeting` are the test skills' own names. Timestamps are shifted to January 2026.

| Folder | Replicates | Pass to the script |
|---|---|---|
| `claude-code/` | a Claude Code session with one subagent | the `<id>` folder (main transcript beside it) |
| `codex-desktop/` | a Codex Desktop parent rollout and its subagent rollout | both `rollout-*.jsonl` files |
| `cursor-chat1/` | a Cursor chat with one subagent | the `<id>` folder |
| `cursor-chat2/` | a Cursor chat that reads a skill without attaching it | the `<id>` folder |
| `devin-subagent/` | a Devin session with a subagent, a slash-command skill and off-chain nodes | `sessions.db --host devin --session-id golden-devin-subagent` |
| `devin-error-interrupted/` | a Devin session with a failed command (exit 2, success flag true) and an interruption | `sessions.db --host devin --session-id golden-devin-error-interrupted` |
| `devin-explicit-only/` | a Devin session with an explicit-only skill typed by hand and another called by the model | `sessions.db --host devin --session-id golden-devin-explicit-only` |

The Devin replicas are small SQLite files in the real table layout (sessions, message_nodes, subagent_heads,
refinery_schema_history). Message text, commands and rule paths are placeholders; session, message, call and agent ids
are regenerated; paths are `~/project/...` and `~/rules/...`; timestamps are shifted to January 2026.

`tests/test_golden.py` asserts the acceptance numbers for each. Do not add real transcripts here.
