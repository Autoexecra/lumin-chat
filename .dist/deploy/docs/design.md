# Copilot Terminal Clone Design

## 1. Target

This project aims to build a Linux terminal agent that is functionally close to GitHub Copilot Terminal:

- natural-language task entry
- multi-turn agent loop
- tool calling driven by an LLM
- shell execution with approval control
- file read, write, search, and directory inspection
- resumable sessions
- interactive chat mode and one-shot mode
- deployable to a Linux host with minimal payload

It is not a byte-level reproduction of GitHub Copilot Terminal, because the official product's internal prompts, policies, UI protocol, telemetry, and private orchestration logic are not public. The goal here is feature parity at the workflow level.

## 2. Product Form

The tool exposes two modes:

- `chat`: interactive REPL-like terminal assistant
- `ask`: one-shot execution for scripts, CI, or remote testing

The interface is intentionally terminal-native. Rich is used only for readable panels and streaming output, not for a heavy TUI.

## 3. Core Capabilities

### 3.1 Conversation Engine

The agent keeps a structured message history and persists it to disk so a session can be resumed.

### 3.2 Tool Loop

The LLM can emit tool calls. The runtime executes them, appends results back into the conversation, and continues until the model stops asking for tools or the loop budget is reached.

### 3.3 Persistent Shell

On Linux, shell execution uses a persistent bash session backed by a PTY. This is important because it preserves:

- current working directory across commands
- exported environment variables across commands
- shell state closer to a real terminal session

When the runtime is not on Linux or when an explicit `cwd` override is provided, execution falls back to one-shot subprocess mode.

### 3.4 Tools

Current built-in tools:

- `run_shell_command`
- `change_directory`
- `list_directory`
- `search_text`
- `read_file`
- `write_file`
- `get_environment`

These cover the minimum feature set needed for terminal automation, coding assistance, deployment, and diagnostics.

### 3.5 Approval Policy

Three approval modes are supported:

- `prompt`: ask before shell execution and file write
- `auto`: execute directly
- `read-only`: refuse shell execution and file writes

This mirrors the core safety interaction pattern of Copilot-like terminal tools.

## 4. Runtime Architecture

### 4.1 Modules

- `main.py`: thin entrypoint
- `src/app.py`: CLI parsing and chat loop
- `src/agent.py`: multi-turn orchestration
- `src/ai_client.py`: LLM transport and streaming parser
- `src/toolkit.py`: local tool registry and execution
- `src/session_store.py`: session persistence
- `src/config_loader.py`: config loading and env override
- `src/prompts.py`: system prompt definition
- `src/ui.py`: streaming and terminal output rendering
- `src/models.py`: shared dataclasses

### 4.2 Message Flow

1. User enters a request.
2. Agent appends it to session history.
3. `AIClient` sends the full message list and tool definitions.
4. If the model returns tool calls, the agent executes them.
5. Tool results are appended as `tool` messages.
6. The agent continues calling the model until a final assistant response is produced.
7. Session state is saved.

## 5. Configuration

`config.json` contains model definitions, app defaults, and logging flags.

Recommended evolution:

- move secrets to environment variables for production
- keep multiple model tiers for cost/performance tradeoffs
- add per-model fallback rules

## 6. Linux Deployment Strategy

Deployment uses a small staging payload instead of uploading the full workspace.

Included in deploy payload:

- application source
- configuration
- requirements
- docs
- runtime scripts

Excluded intentionally:

- `.venv`
- `.dist` build artifacts
- `__pycache__`
- local editor metadata

## 7. Testing Strategy

### 7.1 Local

- syntax validation
- CLI help validation
- read-only agent request

### 7.2 Remote Linux

- upload via `scripts/deploy.ps1`
- bootstrap virtual environment
- install requirements
- run `python main.py --help`
- run a one-shot prompt in `read-only` mode
- run an interactive session in `prompt` or `auto` mode

## 8. Extension Roadmap

To get even closer to GitHub Copilot Terminal later, add:

- patch tool instead of whole-file rewrite for code edits
- richer diff visualization before writes
- command risk classification
- session summaries and compression
- shell environment variable persistence
- remote MCP-style tool plugins
- better slash command coverage

## 9. Suggested AI Coding Tasks

This design document can be used to drive future code generation tasks such as:

- implement persistent PTY backend for Linux
- add patch-based file editing tool
- add command allowlist or sandbox
- add test suite for tool executor
- add markdown transcript export
- add config validation command
