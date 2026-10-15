# Using the tool with other LLMs and agents

The Navigable Entity Relationship Diagram is a normal Python command-line tool. Any assistant that can run shell commands can drive it, and you do not need an assistant at all. This guide shows how to wire it into common tools.

There are three ways to use it, from least to most automated:

| Level | You | The LLM |
|---|---|---|
| **1. No LLM** | Run the commands yourself ([README](../README.md#use-it-from-the-command-line)). | Not involved. |
| **2. LLM as explainer** | Run the commands yourself, then paste `recommendations.md` into a chat. | Explains the findings, prioritizes them, drafts tickets and messages to owners. Needs no tools. |
| **3. LLM as operator** | Sign in once yourself (`run.py login`), then ask the agent to run it. | Runs the setup check and the trace, reads the output, and reports back. Needs a shell. |

## What every setup needs

1. **Python 3.9+ and PyYAML** where the agent runs commands: `pip install -r skills/sigma-navigable-erd/requirements.txt`.
2. **Network access from that machine to your Sigma API host** (for example `https://api.sigmacomputing.com`; yours depends on your Sigma cloud and region).
3. **A Sigma sign-in, never typed into the chat.** By default **you** run `python3 skills/sigma-navigable-erd/scripts/run.py login` once in a terminal. It opens your browser; you sign in; the session is kept in your OS keychain. An agent cannot do this step for you, because it needs your browser. After that the agent can run the trace without any secret. For unattended agents or machines with no browser, use API client credentials in `~/.sigma-lineage/credentials.env` (mode 600), or a token in `SIGMA_API_TOKEN`. See [Sign in](../README.md#sign-in).
4. **The instructions.** Either `AGENTS.md` (short, for coding agents) or `skills/sigma-navigable-erd/SKILL.md` (full workflow). Both tell the agent to explain first, never handle secrets, and share only with consent.

## Recipes

### Codex CLI and other agents that read `AGENTS.md`

Open this repository as the working directory. `AGENTS.md` is already at the root, so the agent reads it automatically. Then:

```text
Trace this Sigma workbook and summarize the recommendations by owner: <URL>
```

### Gemini CLI

Gemini CLI reads `GEMINI.md`. Either copy the file or point to it:

```bash
cp AGENTS.md GEMINI.md
```

Then ask the same question as above from the repository directory.

### Cursor, Windsurf and similar editors

Add the contents of `AGENTS.md` as a project rule (for Cursor, a file in `.cursor/rules/`; for other editors, their rules or custom-instructions setting). Run the agent in this repository and ask it to trace a workbook. Make sure the agent's terminal has network access to Sigma.

### Aider

```bash
aider --read AGENTS.md --read skills/sigma-navigable-erd/SKILL.md
```

Then ask it to run the trace. Aider runs commands you approve.

### Claude Code

See [Use it with Claude](../README.md#use-it-with-claude). The skill folder can be copied into `~/.claude/skills/`.

### A chat model with no tools (level 2)

Run the trace yourself:

```bash
python3 skills/sigma-navigable-erd/scripts/run.py trace "<workbook URL>"
```

Then paste the contents of `recommendations.md` (and, if useful, the "Who owns each object" table from the page) into the chat with a prompt such as:

```text
Below are duplicate-logic findings for a Sigma workbook. Each one is a heuristic.
1. Group them by owner.
2. For each owner, list what to verify first and why.
3. Rank everything by effort versus payoff.
4. Draft a short, polite message to each owner.
Do not assume any finding is certain.
```

Do not paste credentials. The findings contain formulas, table paths and names, so only paste them into a tool your organization allows for that kind of data.

### ChatGPT and other hosted chat products

A hosted chat window usually cannot reach your Sigma API or your files. Use level 2: run the tool locally and paste the findings. If your product offers a local agent or desktop app with a shell, use level 3.

## A generic system prompt

Use this when you build your own agent or custom assistant. It condenses the skill:

```text
You help users trace Sigma workbooks with the Navigable Entity Relationship Diagram tool in
skills/sigma-navigable-erd. You can run shell commands in the repository.

Before running anything, explain in a few sentences: what the tool does (traces every column
of a Sigma workbook through its data models to the warehouse table, shows who owns each layer,
and flags duplicated logic), what the user needs (Python 3.9+ with PyYAML, a Sigma API client,
their Sigma API host, view access to the workbook and its data models, credentials in a private
file), and how results are shared (a single HTML file). Wait for the user to confirm.

Sign-in: the user signs in themselves with `run.py login` (browser, once). If they use API client
credentials instead, those live in ~/.sigma-lineage/credentials.env (mode 600), created by the user.
Never ask for, print, echo, log, or write a client secret or token, and never put one on a command
line. If the user pastes one into the chat, tell them it is exposed and should be rotated.

Run: `python3 skills/sigma-navigable-erd/scripts/run.py check`, then
`python3 skills/sigma-navigable-erd/scripts/run.py trace "<workbook URL>"`.
Report the counts and where the page was saved. Summarize recommendations by owner from
recommendations.md and say they are heuristics to verify. The output contains workbook names,
formulas, table paths and people's names: ask before sharing it anywhere.
```

## Getting the most from an LLM

- Ask it to turn each finding into a **checklist for the owner**: what to verify, where to click (the card's Open link), and what to change.
- Ask it to **compare two runs** (`blame.json` and `recommendations.md` are kept for every run) and say what improved.
- Ask it to **sort by owner**, so each person gets one message instead of many.
- Remind it that **owners are Sigma object owners**, a place to start asking, not blame.

## Troubleshooting

| Symptom | Fix |
|---|---|
| The agent asks you for your client secret or a token | Tell it not to. Sign in yourself with `run.py login`, or put API client credentials in the private file. If you already pasted a secret, rotate it. |
| `No Sigma sign-in is available` | You have not signed in on this machine yet. Run `run.py login` in a terminal. |
| The agent cannot reach Sigma | Its shell is sandboxed without network access. Run the tool yourself (level 2). |
| The agent edits files you did not expect | Ask it to work read-only. The tool itself never writes outside its output folder. |
| `ModuleNotFoundError: yaml` | `pip install -r skills/sigma-navigable-erd/requirements.txt` in the same Python the agent uses. |
