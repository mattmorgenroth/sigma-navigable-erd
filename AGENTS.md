# Instructions for AI coding agents

This repository contains **a tool and a skill** that build a Navigable Entity Relationship Diagram for a Sigma workbook: column-level lineage, ownership, and recommendations for duplicated logic. These instructions are for any agent (Codex, Gemini CLI, Cursor, Aider, Claude Code, and others) that has been asked to **use** the tool or to **work on** this repository.

## If you were asked to trace a Sigma workbook

The full, authoritative workflow is in `skills/sigma-navigable-erd/SKILL.md`. Read it first. In short:

1. **Explain before you act.** Tell the user in a few sentences what the tool does, what they need (Python 3.9+ with PyYAML, a Sigma API client, the API host for their Sigma cloud, view access to the workbook and its data models, credentials in a private file), and how results are shared. Wait for them to say they are ready.
2. **Never handle secrets.**
   - Credentials live in `~/.sigma-lineage/credentials.env` (mode 600) or in environment variables. The user creates that file.
   - Do not ask for, print, echo, log, or commit the client secret. Do not put it on a command line. Do not write it to a file on the user's behalf.
   - If the user pastes a secret into the chat, say so plainly: it is now in the conversation history and should be rotated. Ask them to put the new one in the file themselves.
3. **Verify setup:** `python3 skills/sigma-navigable-erd/scripts/run.py check`
4. **Trace:** `python3 skills/sigma-navigable-erd/scripts/run.py trace "<workbook URL, url-id, or UUID>"` (optional: `--owners owners.json`, `--out DIR`, `--title TITLE`). It can take a minute or two.
5. **Report** the counts the script prints and where the page was saved (`standalone.html` opens in any browser). Summarize the recommendations by owner from `recommendations.md`, and explain that they are heuristics to verify.
6. **Share only with consent.** The output contains workbook and element names, formulas, table paths and people's names. Ask before publishing or sending it anywhere. Prefer sending `standalone.html`.

No credentials? `python3 examples/offline_demo.py` runs the whole tool against a fictional workbook and needs no network. The result is in `examples/sample-output/`.

## If you were asked to change this repository

- **Run the tests** after every change: `python3 -m unittest discover -s tests -v` (standard library only, no network, about a second).
- **Layout:** `skills/sigma-navigable-erd/scripts/` holds `run.py` (CLI), `blame.py` (the tracer), `recommend.py` (the engine), `render.py` and `template.html` (the page). `examples/fake_sigma.py` is a fictional Sigma API used by the tests and the demo.
- **Adding a detector:** put a case in `examples/fake_sigma.py` that should trigger it (and one that must not), write the test, then implement it in `recommend.py`. Findings are dictionaries with `type`, `sev`, `title`, `detail`, `action`, `nodes`, `roles`, `owners`, `where`, `saves`.
- **After changing `examples/` or any script that affects output,** regenerate the sample: `python3 examples/offline_demo.py`.
- **Keep everything fictional.** Do not add real company names, people, URLs, workbook content, or credentials anywhere. A test (`HygieneTests`) fails if a credential-shaped value or a company-specific reference appears.
- **The tool is read-only by design.** Do not add anything that creates, changes or deletes objects in Sigma. Do not weaken the host allowlist (HTTPS `*.sigmacomputing.com` only) or the credentials-file permission check.
- **Page changes** are in one file, `template.html` (HTML, CSS and JavaScript, no dependencies). It must work as a standalone file and when published as a Claude Artifact, in light and dark themes, at phone width, and with long column names (cards size themselves to their content).
