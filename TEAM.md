# TEAM.md — running call4me with parallel Claude Code agents

Seven specialist agents build call4me at the same time, each owning its own folders so they never edit the same files.

> **Status:** this is how the first build was run. Since then Twilio was replaced by Cartesia agents (`backend/cartesia/`), the iOS app was shelved for the mobile web app (`web/`), and Convex was added as an optional store (`convex/`, `backend/db/store.py`). Those newer areas were built after the team phase and have no specialist agent; the lead owns them. CLAUDE.md describes the current system.

## The team

| Agent | Owns | Main job |
|---|---|---|
| `platform` | `backend/main.py`, `config.py`, `contracts.py`, `db/`, `audit/`, `notify/`, Docker, infra | Scaffold, shared contracts, DB, endpoints, audit log, Azure deploy |
| `telephony` | `backend/telephony/`, `fakeline/` | Phone-mode line (`/phone` rep page), mock line, fake support line (originally Twilio) |
| `audio-speech` | `backend/audio/`, `backend/speech/` | Audio decoding, VAD, music detection, Cartesia STT and TTS |
| `call-engine` | `backend/engine/` | State machine, Jev decisions, menu navigation, human detection |
| `conversation-agent` | `backend/agent/` | Claude tool-use loop, `ask_user`, guard layer |
| `ios-app` | `ios/` | Shelved SwiftUI app (replaced by `web/`) |
| `qa-evals` | `evals/`, `tests/integration/`, `dashboard/` | Bot rep, 11 scenarios, metrics, backup console, reviews |
| lead (no agent) | `backend/cartesia/`, `web/`, `convex/`, `scripts/` | Cartesia real calls, mobile web app + judges view, Convex storage |

Definitions live in `.claude/agents/`. They can be used as subagents, as agent-team teammates, or as the role for a separate session.

## One-time setup (5 minutes)

1. Put these files at the root of your repo, then:
   ```bash
   git init && git add -A && git commit -m "Add CLAUDE.md and team setup"
   ```
   Worktrees need a git repo with at least one commit.
2. Create `.env` from the variables in CLAUDE.md section 12. `.worktreeinclude` copies it into every worktree automatically. Agents are blocked from reading it by `.claude/settings.json`.
3. Update Claude Code: `claude update`.
4. Run `claude` once in the repo and accept the workspace trust prompt (worktrees and teams need it).
5. Optional, for side-by-side panes: install tmux (`brew install tmux`), then add to `~/.claude/settings.json`:
   ```json
   { "teammateMode": "auto" }
   ```
   and start the lead inside a tmux session.

## Phase 0: foundation first (about 45 minutes, one agent)

Parallel work only goes smoothly once the shared interfaces exist. Start a single session:

```bash
claude
```
Prompt:
> Use the platform agent to do its Phase 0: scaffold the repo from CLAUDE.md section 5, write backend/contracts.py, the DB models, docker-compose, and stub every endpoint. Commit when done.

Check: `uv run pytest` passes and `backend/contracts.py` exists. Commit.

## Phase 1: run the team. Pick one method

### Method A: agent team (one lead coordinates everyone; recommended)

Agent teams are enabled by `.claude/settings.json` (experimental feature). Start the lead:

```bash
claude
```
Prompt the lead:
> Create an agent team for call4me. Spawn these teammates using their agent types from .claude/agents: telephony, audio-speech, call-engine, conversation-agent, and qa-evals. Each works only in the folders it owns (see TEAM.md). Build CLAUDE.md section 18 steps 3–11 toward Gate 2: a full call on the fake line including ask_user. Use the shared task list with dependencies: telephony's fake line and media stream come before call-engine integration. Wait for teammates to finish their tasks before integrating.

Tips:
- **Arrow keys + Enter** in the agent panel to view and message any teammate; **Ctrl+T** shows the task list.
- Keep it to 4–5 teammates; more adds coordination overhead and tokens.
- Approve permission prompts in the lead's window.
- Run `ios-app` separately (Method B) on your Mac, since it needs Xcode and a real iPhone.

### Method B: separate sessions in worktrees (you coordinate)

Each terminal gets its own isolated copy of the repo and branch:

```bash
# Terminal 1
claude --worktree telephony --agent telephony
# Terminal 2
claude --worktree audio --agent audio-speech
# Terminal 3
claude --worktree engine --agent call-engine
# Terminal 4
claude --worktree agent --agent conversation-agent
# Terminal 5 (on the Mac with Xcode)
claude --worktree ios --agent ios-app
```

If your Claude Code version doesn't accept `--agent`, start with `claude --worktree <name>` and begin your first prompt with: "You are the <agent-name> agent. Follow .claude/agents/<agent-name>.md."

Give each session the next steps from its agent file. When an area finishes a milestone, merge its branch into `main` from your main checkout:

```bash
git merge worktree-telephony
```
Merge in this order: platform → telephony → audio → engine → agent → ios → qa.

## Integration checkpoints

| When | Who | Check |
|---|---|---|
| Gate 1 (Fri night) | lead or you | Agent calls the fake line; audio reaches the server |
| Gate 2 (Sat ~1 PM) | `qa-evals` | Full fake-line call including `ask_user` passes |
| Gate 3 (Sat ~7 PM) | everyone | Feature freeze; app works end to end (the mobile web app after the iOS app was shelved) |

At each gate, ask `qa-evals` to review all areas against CLAUDE.md section 2 (safety rules).

## Rules for every agent
- Only edit files you own. Need a change elsewhere? Message the owner or the lead.
- `backend/contracts.py` changes go through `platform` and are announced to everyone.
- Commit small and often on your own branch; run your tests first.
- Never store or log `ask_user` answers.

## Troubleshooting
- **Teammates don't appear:** check `.claude/settings.json` has `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` set to `"1"`, and explicitly ask for "an agent team".
- **Lead starts coding instead of delegating:** "Wait for your teammates to complete their tasks before proceeding."
- **Too many permission prompts:** add the commands to `permissions.allow` in `.claude/settings.json`.
- **Merge conflicts:** almost always means two agents edited the same file; check the ownership table.
- **Leftover worktrees:** `git worktree list`, then `git worktree remove <path>`.

Docs: https://code.claude.com/docs/en/agent-teams · https://code.claude.com/docs/en/worktrees · https://code.claude.com/docs/en/sub-agents
