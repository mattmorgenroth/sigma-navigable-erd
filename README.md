# Navigable Entity Relationship Diagram for Sigma

Column-level lineage and ownership for any [Sigma](https://www.sigmacomputing.com) workbook, with recommendations for logic that is written more than once.

Point it at a workbook and it traces every column back through the workbook's elements and data models to the warehouse table. You get one searchable HTML page where you can click a column and see every column that feeds it, where each one lives, whether it is a plain pass-through or a calculation, and **who owns that layer**. It answers two questions that come up constantly:

- **Who do I need to bring in?** When a number is wrong or a dashboard needs extending, the logic is often spread across a workbook and several data models owned by different people.
- **What can I delete?** Duplicated lookups, the same formula pasted into five elements, and hidden columns nobody uses all add up.

It runs as a skill inside [Claude](#use-it-with-claude), as a plain command-line tool you can use [without any AI at all](#use-it-from-the-command-line), or with [other LLMs and agents](#use-it-with-other-llms-and-agents).

> **Unofficial project.** This is a personal, open-source tool. It is not made, endorsed, or supported by Sigma Computing. "Sigma" is a trademark of its owner. It only reads from the public Sigma REST API.

> **Try it first, with no Sigma account.** `python3 examples/offline_demo.py` runs the whole tool against a fictional workbook and writes the result to `examples/sample-output/`. A ready-made copy is already there: open [`examples/sample-output/standalone.html`](examples/sample-output/standalone.html) in a browser.

---

## Contents

- [What you get](#what-you-get)
- [Quick start](#quick-start)
- [Requirements](#requirements)
- [Set up credentials safely](#set-up-credentials-safely)
- [Use it from the command line](#use-it-from-the-command-line)
- [Use it with Claude](#use-it-with-claude)
- [Use it with other LLMs and agents](#use-it-with-other-llms-and-agents)
- [Where results are saved](#where-results-are-saved)
- [Share the results](#share-the-results)
- [Reading the page](#reading-the-page)
- [The recommendations](#the-recommendations)
- [Ownership](#ownership)
- [How it works](#how-it-works)
- [Privacy and security](#privacy-and-security)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

---

## What you get

Every run produces a self-contained page with these parts:

| Part | What it does |
|---|---|
| **Trace a column** | Search any column by name. Click a result and an ERD of just that column appears: every column that feeds it, drawn left to right, each card showing the column's full name, where it lives (element and object), who owns that layer, and whether it is pass-through or calculated. |
| **Focus** and **Open** | Every card has **Focus** (make that column the new center of the diagram) and **Open ↗** (jump to that element in Sigma, in edit mode). |
| **Recommendations** | A collapsible section (collapsed by default, with live statistics) listing duplicated logic that could be removed. Filter by type, impact or owner. |
| **Isolate** | On any card named in a recommendation, one click reduces the diagram to just the cards that must change, each labelled with its role ("Remove this lookup", "Use this column instead", "Uses the lookup: re-point it"). The recommendation is pinned in a header above the diagram. |
| **Object lineage** | A diagram of the workbook, the data models behind it, and the warehouse sources, with owners. |
| **Who owns each object** | A roster of every workbook and data model in the chain, its owner, last editor, and how many columns have logic applied there. |
| **Column blame table** | Every element's columns with a Pass-through or Calculated badge, the layers that apply logic (with formulas), and the origin. Filter by element, person, or text. Shows calculated columns only by default. |

It also writes the recommendations as a Markdown file you can paste into a ticket.

```mermaid
flowchart LR
  A["Workbook URL"] --> B["run.py trace"]
  B --> C["Sigma REST API (read-only)"]
  C --> D["blame.py: column graph"]
  D --> E["recommend.py: duplicate logic"]
  D --> F["render.py: HTML page"]
  E --> F
  F --> G["index.html, standalone.html, recommendations.md, blame.json"]
```

---

## Quick start

```bash
git clone https://github.com/mattmorgenroth/sigma-navigable-erd.git
cd sigma-navigable-erd
pip install -r skills/sigma-navigable-erd/requirements.txt

# 1. See it work with no credentials (fictional data)
python3 examples/offline_demo.py

# 2. Put your Sigma API credentials in a private file (see "Set up credentials safely")
# 3. Check they work
python3 skills/sigma-navigable-erd/scripts/run.py check

# 4. Trace a workbook: paste its URL, its url-id, or its UUID
python3 skills/sigma-navigable-erd/scripts/run.py trace "https://app.sigmacomputing.com/<org>/workbook/<name>-<urlId>"
```

The script prints where it saved the page. Open `standalone.html` from that folder in any browser.

---

## Requirements

- **Python 3.9 or newer** and **PyYAML** (`pip install -r skills/sigma-navigable-erd/requirements.txt`). Nothing else: the tool uses only the standard library otherwise.
- **A Sigma API client** (client ID and secret) for the organization that holds the workbook. In Sigma, create one under Administration, Developer Access. The client acts as the user who created it, so that user must be able to **view the workbook and the data models behind it**. A dedicated read-only service user is a good choice.
- **Your Sigma API host.** It depends on your Sigma cloud and region. Examples: `https://api.sigmacomputing.com`, `https://aws-api.sigmacomputing.com`, `https://api.us-a.aws.sigmacomputing.com`, `https://api.eu.aws.sigmacomputing.com`, `https://api.uk.sigmacomputing.com`, `https://api.ca.sigmacomputing.com`, `https://api.az.sigmacomputing.com`. These are examples; confirm yours in Sigma's API documentation. The tool refuses any host that is not an HTTPS `*.sigmacomputing.com` host.
- **A browser** to open the result.

---

## Set up credentials safely

The tool reads credentials from environment variables or from an env file. It never accepts them as command-line flags, so they do not appear in your shell history or process list.

Create `~/.sigma-lineage/credentials.env` yourself:

```bash
mkdir -p ~/.sigma-lineage && chmod 700 ~/.sigma-lineage
cat > ~/.sigma-lineage/credentials.env <<'EOF'
SIGMA_BASE_URL="https://api.sigmacomputing.com"
SIGMA_CLIENT_ID="your-client-id"
SIGMA_CLIENT_SECRET="your-client-secret"
EOF
chmod 600 ~/.sigma-lineage/credentials.env
```

- The tool **refuses to run if the file is readable by other users** and tells you to `chmod 600` it.
- Use `--env-file PATH` or the `SIGMA_ENV_FILE` environment variable to keep the file somewhere else, for example one file per Sigma organization.
- Or export the three variables in your shell instead of using a file.
- **Never paste the secret into a chat with an AI assistant.** If you do by accident, treat it as exposed and rotate the key in Sigma.

Check it works without tracing anything:

```bash
python3 skills/sigma-navigable-erd/scripts/run.py check
```

---

## Use it from the command line

No AI is needed. Everything is a normal Python script.

```text
run.py check [--env-file FILE]
run.py trace <workbook URL | url-id | UUID> [--env-file FILE] [--out DIR] [--owners owners.json] [--title TITLE]
```

| Option | Meaning |
|---|---|
| `target` | The workbook. A full URL, the url-id at the end of it, or the workbook UUID all work. |
| `--env-file FILE` | Credentials file. Defaults to `$SIGMA_ENV_FILE`, then `~/.sigma-lineage/credentials.env`. |
| `--out DIR` | Central output folder. Defaults to `~/sigma-lineage`. Point it at a synced team folder to share runs. |
| `--owners FILE` | JSON mapping warehouse table prefixes to an owner. See [Ownership](#ownership). |
| `--title TITLE` | Page title. Defaults to "Navigable Entity Relationship Diagram". |

A large workbook can take a minute or two. The tool retries with backoff when Sigma returns a rate-limit or server error.

---

## Use it with Claude

The repository is a Claude skill. The skill is the folder [`skills/sigma-navigable-erd/`](skills/sigma-navigable-erd/). Its [`SKILL.md`](skills/sigma-navigable-erd/SKILL.md) tells Claude how to run the whole workflow: explain what the tool does, check your setup, run the trace, walk you through the page, and (optionally) share it.

### Claude Code

**Option 1: copy the skill (simplest).**

```bash
git clone https://github.com/mattmorgenroth/sigma-navigable-erd.git
mkdir -p ~/.claude/skills
cp -R sigma-navigable-erd/skills/sigma-navigable-erd ~/.claude/skills/
pip install -r ~/.claude/skills/sigma-navigable-erd/requirements.txt
```

Start Claude Code and ask, for example:

> Trace this Sigma workbook and tell me who owns the logic: https://app.sigmacomputing.com/…

Claude loads the skill from its description, starts with a short explanation of what it does and what you need, and waits for your go-ahead.

**Option 2: install as a plugin.** The repository includes plugin and marketplace manifests in `.claude-plugin/`:

```text
/plugin marketplace add mattmorgenroth/sigma-navigable-erd
/plugin install sigma-navigable-erd@sigma-navigable-erd
```

The manifests follow Claude Code's plugin format, but plugin commands change between versions, so if these fail use Option 1.

### Claude on the web or desktop

If your plan supports custom skills, zip the `skills/sigma-navigable-erd` folder and upload it in Settings. Note that the tool must run somewhere that can reach the Sigma API and write files. The sandbox in a chat window may not allow outbound network access. In that case run the tool on your own machine (see [the command line](#use-it-from-the-command-line)) and use Claude to explain or discuss the result.

### Sharing with your claude.ai organization

When Claude has the Artifact tool, it can publish the page for you. See [Share the results](#share-the-results).

### Tips for Claude

- Ask for the **recommendations** by name: "show me the duplicate logic in this workbook and who owns it".
- Ask Claude to **open the recommendations file** and draft tickets or messages to the owners.
- The skill tells Claude to refuse secrets pasted into chat and to point you to the credentials file instead.

---

## Use it with other LLMs and agents

The tool is a plain Python CLI, so any assistant that can run shell commands can drive it. The skill text is deliberately written as clear step-by-step instructions that are not specific to one product. Full recipes are in **[docs/using-with-other-llms.md](docs/using-with-other-llms.md)**. The short version:

1. **Give the agent the instructions.** Use the right mechanism for your tool:

   | Tool | Where to put the instructions |
   |---|---|
   | Codex CLI, and agents that read `AGENTS.md` | This repository already contains [`AGENTS.md`](AGENTS.md). Open the repo as your working directory. |
   | Gemini CLI | Copy `AGENTS.md` to `GEMINI.md`, or reference it from yours. |
   | Cursor / Windsurf / similar | Add the contents of `AGENTS.md` (or `SKILL.md`) as a project rule. |
   | Aider | `aider --read AGENTS.md` |
   | Any chat model with shell or code execution | Paste `skills/sigma-navigable-erd/SKILL.md` as the system or first message. |
   | A chat model with **no** tools | Run the CLI yourself, then paste `recommendations.md` into the chat and ask it to explain and prioritize. |

2. **Give it a shell and a Sigma-reachable network.** It needs to run `python3 skills/sigma-navigable-erd/scripts/run.py …` where the Sigma API is reachable.
3. **Keep secrets out of the conversation.** Tell the agent that credentials live in `~/.sigma-lineage/credentials.env` and that it must never ask for, print, or echo them.

A minimal prompt that works with most agents:

```text
Use the tool in skills/sigma-navigable-erd to trace this Sigma workbook: <URL>.
Follow skills/sigma-navigable-erd/SKILL.md. Credentials are already in
~/.sigma-lineage/credentials.env; never print or ask for them. When it finishes, tell me
where the page was saved, summarize the recommendations by owner, and draft a message to
each owner listing what to check.
```

---

## Where results are saved

Every run is kept, in an organized folder (default `~/sigma-lineage`):

```text
~/sigma-lineage/
  INDEX.md                                  one line per run
  <api-host>/<workbook-name>-<id>/
    <YYYY-MM-DD_HHMM>/
      index.html                            the page, meant for publishing as an Artifact
      standalone.html                       the same page as a complete file for email or opening from disk
      recommendations.md                    duplicate-logic findings as a shareable list
      blame.json                            the full result, for your own tooling
    latest/
      index.html, standalone.html           copies of the newest run
```

Pass `--out` to put this somewhere shared.

---

## Share the results

The page contains workbook and element names, column formulas, warehouse table paths, and Sigma owner names. **Review it before sharing**, and share it only with people who are allowed to see that. Opening an **Open ↗** link still follows each person's Sigma permissions; the page does not give anyone access to the workbook.

| How | When to use it |
|---|---|
| **Send `standalone.html`** | Always works. It is one complete file that opens in any browser. |
| **Publish as a Claude Artifact** | In Claude with the Artifact tool. Ask Claude to publish `index.html`. Then open the page's **Share** menu and choose who in your claude.ai organization can open it. Teammates need a claude.ai account in the same organization. Claude reports the sharing state shown in the publish result; sharing defaults can differ between organizations, so check the Share menu yourself. To update, publish the new file to the same link. |
| **Team folder** | Run with `--out` pointing at a synced folder. |
| **Paste `recommendations.md`** | Into a ticket, a pull request, or a chat. |

The page loads two fonts from Google Fonts. If a browser or network blocks that, it falls back to system fonts and everything still works.

---

## Reading the page

- **Pass-through** means the column is a plain reference all the way down. **Calculated** means at least one layer applies a formula. The layer that applies it, and its owner, are listed.
- **The column diagram** reads right to left. The column you chose is on the right; everything feeding it is to the left, down to the warehouse table. Lines show which column feeds which.
- **Card colours:** orange borders are workbook columns, teal are data-model columns, blue are warehouse columns. Dashed cards are input tables, custom SQL, or hidden upstream elements.
- **⚑** on a card means one or more recommendations involve it. **Isolate** shows only the cards involved.
- **Hidden upstream / partial** means the chain reaches a data model whose upstream element the API does not return. The model and its owner are shown instead.
- **Open ↗** opens `<object URL>/edit?:nodeId=<elementId>`. Workbook links are reliable. Data-model links may not open the exact element. The person clicking needs edit access to land in the editor.

---

## The recommendations

Each finding says what to check first. They are **heuristics**: columns are matched by name, so the tool cannot prove two same-named columns hold the same values.

| Finding | What triggers it |
|---|---|
| **Redundant lookup** | A column is pulled with `Lookup()` although a column with the same name is already available from the element's own sources, or from another source of a downstream element. Example: an ID looked up from one table when the consuming element already receives it from another. The isolate view shows the lookup to remove and the column to use instead. |
| **Repeated lookups** | Three or more `Lookup()` columns in one element that hit the same source on the same key. A join or relationship replaces them. |
| **Same formula repeated** | An identical formula of 25 characters or more written in more than one place. The finding names a shared upstream element where it could live once. |
| **Elements repeating the same calculated columns** | Two or more elements share at least five calculated columns with identical formulas (80% or more overlap). They are reported as **one group**, not as every pair. Only *calculated* columns count, because elements that read the same table share plain column names by definition. Filters and layout are not compared. |
| **Unused hidden columns** | Hidden calculated columns that no formula, filter, sort, grouping or control in the workbook refers to. |

Things it deliberately does **not** flag: pass-through columns, elements that merely read the same warehouse table, and same-named elements in different data models (usually regional copies).

**Before removing anything:** confirm the values really match, and that nothing outside the traced workbook (other workbooks, saved views, actions) depends on it.

---

## Ownership

"Owner" means the Sigma **owner of the workbook or data model** where the logic sits, and its last editor. It is the best place to start asking. It is not proof of who is responsible for the business logic, and it is not blame.

Warehouse tables have no owner in Sigma, so they show **"owner not set"** unless you map them. Copy [`owners.example.json`](skills/sigma-navigable-erd/owners.example.json), edit it, and pass it with `--owners`:

```json
{
  "ANALYTICS.MARTS": "Data Engineering (jane.doe@example.com)",
  "ANALYTICS.MARTS.FINANCE": "Finance Analytics (finance-data@example.com)",
  "RAW": "Platform Team"
}
```

Keys are `DATABASE`, `DATABASE.SCHEMA`, or `DATABASE.SCHEMA.TABLE` prefixes. The longest matching prefix wins. Keys starting with `_` are ignored, so you can leave comments.

---

## How it works

1. **Resolve** the workbook from a URL, url-id or UUID (a paged search of the files the API user can see).
2. **Read** the workbook spec and every data model it uses (`/spec`, `/lineage`, owners via `/members`). Data models are followed recursively as they are discovered.
3. **Trace** each column of each table-like element. A formula that is exactly one `[Source/Column]` reference is a pass-through. Anything else is a calculation, recorded with the object, element, formula and owner. References are resolved through the element's sources, then through other elements in the same object (for example `Lookup([Other Table/Col], …)`), then through the object's lineage list when a source element is not exposed by the API.
4. **Build a graph:** each column is a node with its location, owner, formula, a link, and the nodes it reads from. Warehouse columns, custom SQL, input tables and hidden upstream elements are terminal nodes.
5. **Recommend:** the detectors in `recommend.py` walk the graph and the elements' source and column inventories (including full column lists for warehouse tables).
6. **Render:** `render.py` fills a single-file HTML template with the graph and findings as embedded data. There is no server, no framework, and no build step.

### Sigma API endpoints used

`POST /v2/auth/token`, `GET /v2/whoami`, `GET /v2/files` (resolve a URL), `GET /v2/workbooks/{id}` and `/spec` and `/lineage`, `GET /v2/dataModels/{id}` and `/spec` and `/lineage`, `GET /v2/members/{id}`, `POST /v2/connection/{id}/lookup` (find a table, a lookup only), `GET /v2/connections/tables/{id}/columns`. Nothing is created, changed or deleted.

### Repository layout

```text
skills/sigma-navigable-erd/     the skill (SKILL.md) and the code
  SKILL.md                      instructions for an AI assistant (also readable as a user guide)
  scripts/run.py                command-line entry point (check, trace)
  scripts/blame.py              the tracer: workbook and data models to a column graph
  scripts/recommend.py          the recommendation engine
  scripts/render.py             renders the page
  scripts/template.html         the page template (HTML, CSS and JavaScript in one file)
  owners.example.json           example warehouse owner mapping
  requirements.txt              PyYAML
examples/
  fake_sigma.py                 a fictional Sigma API and workbook ("Acme Retail") for demos and tests
  offline_demo.py               runs the full tool against it with no network or credentials
  sample-output/                a ready-made result you can open
tests/test_offline.py           end-to-end tests (standard library only)
docs/using-with-other-llms.md   recipes for non-Claude assistants
AGENTS.md                       instructions for coding agents working with this repo
.claude-plugin/                 Claude Code plugin and marketplace manifests
```

---

## Privacy and security

- **Read-only.** It never changes a workbook, data model, permission or connection.
- **Credentials** come from an env file or environment variables only, never from flags, and the tool refuses a file other users can read.
- **Host allowlist.** The client secret is only ever sent to an HTTPS `*.sigmacomputing.com` host. Any other host is refused before a request is made.
- **No telemetry, no third-party services.** The only network calls are to your Sigma API host. The generated page fetches two fonts from Google Fonts when opened and works without them.
- **The output is sensitive.** It includes formulas, table paths and people's names. Treat it like the workbook itself. Do not commit generated runs to a public repository. The repository's `.gitignore` excludes `sigma-lineage/`, `.env` and `*.env`.
- **Repository hygiene is tested.** One of the tests fails if a credential-shaped value or a company-specific reference appears anywhere in the repository.

---

## Limitations

- **Name-based tracing.** Columns are matched by name through formulas. Unusual naming, or two different columns with the same name, can mislead.
- **Hidden upstream.** The API does not return every element of every data model. Where the chain reaches one, the trace stops at that model and says so. A user with view access to the data models gives the fullest result.
- **Sigma objects only.** It traces down to the warehouse table. It does not trace lineage inside the warehouse (dbt or SQL models).
- **Scope.** It follows the workbook and the data models it draws from. Other workbooks that use the same models are not scanned.
- **Heuristics.** Recommendations can be wrong in both directions. A "redundant" lookup can hold different values from the column it duplicates, and logic that is truly duplicated can slip past the thresholds (for example formulas shorter than 25 characters).
- **Display caps.** A column diagram shows up to about 140 columns (it says when it is capped). The column table shows up to 400 rows at a time; use the search box to narrow it.
- **Very large workbooks** produce multi-megabyte pages. Claude Artifacts accept up to 16 MB.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `… is readable by other users` | `chmod 600` the credentials file. |
| `Refusing base URL …` | Use the HTTPS API host for your Sigma cloud. Only `*.sigmacomputing.com` hosts are allowed. |
| `Missing SIGMA_CLIENT_ID …` | Put the three variables in the credentials file or export them. |
| 401 or 403 | Wrong client or host, or the API user cannot see that object. |
| `Could not find a workbook …` | The link belongs to another organization, or the API user lacks access to it. |
| Many "hidden upstream" columns | A data model the API user cannot fully read. Use a user with view access to those models. |
| 429 or slow | The tool retries with backoff. Run again later for very large workbooks. |
| `ModuleNotFoundError: yaml` | `pip install -r skills/sigma-navigable-erd/requirements.txt` |
| An **Open ↗** link shows a permissions page | You need edit access to open the editor. |

---

## Development

```bash
pip install -r skills/sigma-navigable-erd/requirements.txt
python3 -m unittest discover -s tests -v     # 23 tests, no network
python3 examples/offline_demo.py             # regenerate examples/sample-output/
```

The tests run the real tracer against `examples/fake_sigma.py`, a fictional workbook built to contain every kind of duplication the engine looks for, plus things that must not be flagged. To add a detector, add a case to the fixture and a test, then implement it in `recommend.py`.

Issues and pull requests are welcome. Please keep every example fictional: no real company names, people, URLs, workbook content or credentials.

---

## License

[MIT](LICENSE) © 2026 Matt Morgenroth.
