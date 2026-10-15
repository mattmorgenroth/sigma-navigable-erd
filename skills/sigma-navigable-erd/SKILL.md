---
name: sigma-navigable-erd
description: >-
  Build a Navigable Entity Relationship Diagram for any Sigma workbook: column-level
  lineage plus ownership, with recommendations for duplicated logic. Use when someone asks
  "where does this column come from", "who owns the logic behind this workbook", "who do
  I need to involve to fix or extend this dashboard", "trace a column to its source",
  "find redundant or duplicated logic in this workbook", or wants a lineage / ERD / blame
  map of a Sigma workbook. Reads the workbook and its data models through the Sigma REST
  API (read-only), traces every column to its warehouse table, marks where calculations
  happen and who owns each layer, and produces a single searchable HTML page with a
  per-column diagram. Results are saved in an organized local folder and can be shared as
  a standalone file or, in Claude, published as an Artifact to colleagues in the same
  claude.ai organization.
---

# Navigable Entity Relationship Diagram for Sigma

Paths and commands below are relative to this skill's folder (`skills/sigma-navigable-erd/` when you cloned the repository). Start every use of this skill by showing the user the intro below (adapt the wording, keep all three parts), then wait for them to confirm they are ready before running anything.

## 1. Opening message to the user

```
Navigable Entity Relationship Diagram for Sigma

What it does
Pick any Sigma workbook and I will trace every column back through its data models
to the warehouse table. You get a searchable page where you can click a column and
see every column that feeds it, where each one lives, whether it is a plain
pass-through or a calculation, and who owns that layer. Use it to decide who to
invite when something needs fixing or extending. It also flags logic that is written
more than once (redundant lookups, repeated formulas, elements that repeat the same
calculated columns, unused hidden columns) so you can remove it.

What you need
- Python 3.9+ and PyYAML (pip install -r requirements.txt)
- Your Sigma API host (it depends on your Sigma cloud and region)
- A Sigma sign-in. By default you sign in once with your browser and the tool then acts as
  you, so there is no secret to store. Your account type may need the "Use Sigma API with
  OAuth" permission. If browser sign-in is not available to you, use an API client (client
  ID and secret) saved in a private file on your machine, never pasted into this chat.
- Whoever signs in (or owns the API client) must be able to view the workbook and the data
  models behind it

How results are shared
The result is one HTML page, saved in a folder on your machine. You can send the
standalone file to anyone. If you are using Claude, I can also publish it as an Artifact;
you then use the page's Share menu to choose who in your claude.ai organization can open
it. Opening the workbook links still follows each person's Sigma permissions.

Ready to set up, or already configured?
```

## 2. Setup (do this with the user, never with their secrets in chat)

1. **Install:** `pip install -r requirements.txt` (only PyYAML). Scripts are in this skill's `scripts/` folder.
2. **Set the API host.** It matches the cloud and region of the Sigma organization, for example `https://api.sigmacomputing.com`, `https://aws-api.sigmacomputing.com`, `https://api.us-a.aws.sigmacomputing.com`, `https://api.eu.aws.sigmacomputing.com`, `https://api.uk.sigmacomputing.com`, `https://api.ca.sigmacomputing.com`, `https://api.az.sigmacomputing.com`. These are examples; confirm the one for your organization in Sigma's API documentation. The tool refuses any host that is not an HTTPS `*.sigmacomputing.com` host. Put it in `~/.sigma-lineage/credentials.env` (the host is not a secret) or pass `--base-url`:

   ```
   mkdir -p ~/.sigma-lineage && chmod 700 ~/.sigma-lineage
   echo 'SIGMA_BASE_URL="https://api.sigmacomputing.com"' > ~/.sigma-lineage/credentials.env
   ```
3. **Sign in with the browser (the default).** The user runs this in a terminal: `python3 scripts/run.py login`. A browser tab opens, they sign in to Sigma, and the session is saved in the OS keychain (macOS Keychain, or Linux `secret-tool`). There is no client secret to create or store, and the tool sees only what that person can see. **An assistant cannot do this step for the user** because it needs their browser and a terminal: ask them to run it and tell you when it is done. Where there is no keychain (for example Windows), set `SIGMA_OAUTH_STORE=file` to keep the session in a private file instead. `python3 scripts/run.py logout` removes the saved session.
4. **Fallback: API client credentials.** For unattended runs, for hosts without browser sign-in, or when the account cannot use it. In Sigma, create a client (Administration, Developer Access) and add it to the same private file, then lock the file down:

   ```
   SIGMA_CLIENT_ID="..."
   SIGMA_CLIENT_SECRET="..."
   ```
   ```
   chmod 600 ~/.sigma-lineage/credentials.env
   ```
   The tool refuses a file that holds a secret and is readable by other users. It uses these automatically when there is no saved browser session. `--auth-mode client-credentials` forces them.
5. **Already hold a token?** A valid token in `SIGMA_API_TOKEN` is used as is. (Sigma's own CLI can print one.)
6. **Verify:** `python3 scripts/run.py check` shows how it signed in (browser sign-in, client credentials, or `SIGMA_API_TOKEN`) and who you are, without tracing anything.

**Sign-in modes** (`--auth-mode` or `SIGMA_AUTH_MODE`): `auto` (default) uses the saved browser sign-in, then client credentials, then offers browser sign-in if you are at a terminal. `browser` and `client-credentials` force one method.

**About browser sign-in.** Sigma documents browser (OAuth) sign-in for its own CLI and MCP server, and the API advertises the standard OAuth discovery that this tool follows. Sigma does not document this exact handshake for third-party tools, so treat it as the preferred route that can change. If `login` says it is unavailable, or your account type lacks the permission, use API client credentials.

**If the user pastes a client secret into the chat:** do not repeat it, do not put it on a command line, and do not write it to a file for them. Tell them plainly that it is now in the conversation history, recommend rotating the key, and ask them to put the new one in the credentials file themselves.

**Optional owners file.** Warehouse tables have no owner in Sigma. To show one, copy `owners.example.json`, map table prefixes (`DATABASE.SCHEMA` or `DATABASE.SCHEMA.TABLE`) to a team or person, and pass `--owners owners.json`. Unmapped tables show as "owner not set".

**No Sigma access yet?** `python3 ../../examples/offline_demo.py` runs the whole tool against a fictional workbook with no network, so the user can see the result first.

## 3. Run a trace

```
python3 scripts/run.py trace "<workbook URL, url-id, or UUID>" [--owners owners.json] [--title "Custom page title"] [--out DIR]
```

Takes a minute or two for large workbooks. Output goes to a central, organized folder (default `~/sigma-lineage`, change with `--out`, for example a synced team drive):

```
~/sigma-lineage/
  INDEX.md                                   one line per run
  <api-host>/<workbook-name>-<id>/
    <YYYY-MM-DD_HHMM>/index.html, blame.json   every run is kept
    <YYYY-MM-DD_HHMM>/standalone.html          the same page as a complete file for email or opening from disk
    <YYYY-MM-DD_HHMM>/recommendations.md       the duplicate-logic findings as a shareable list
    latest/index.html                          copy of the newest page
```

After the run, tell the user the counts printed by the script and where the page is saved. Offer to open it, then walk them through it: **Trace a column** (search, click, then **Focus** to re-center on any column in the chain or **Open** to jump to that element in Sigma), the owner roster, and the element-by-element table.

### Duplicate-logic recommendations

Every trace also runs a recommendation pass (`scripts/recommend.py`) and shows the results in a **Recommendations** section of the page (collapsed by default, with live stats), in `recommendations.md`, and as a flag on affected boxes in the column diagram. Each finding gives impact (high, medium, low), what to check, what to do, the owners to involve, and buttons to trace an affected column or **Show involved cards**. In a column trace, any card named in a recommendation has an **Isolate** toggle that reduces the diagram to just the cards that must change, each labelled with its role (for example "Remove this lookup", "Uses the lookup: re-point it", "Already provides this value"), plus the short paths connecting them. The recommendation is pinned in a header above the diagram, and **Show full trace** (or Back) returns to the full view.

| Finding | What it means |
|---|---|
| Redundant lookup | A column is pulled with `Lookup()` although the same column (by name) is already available from the element's own sources or from another source of a downstream element. Example: an Org ID looked up from one table when the consuming element already receives it from another. |
| Repeated lookups | Three or more lookups to the same source on the same key. A join or relationship replaces them. |
| Same formula repeated | An identical formula (25 characters or longer) written in several elements. Define it once upstream. The finding names a shared upstream element when one exists. |
| Elements repeating the same calculated columns | Two or more elements share at least five calculated columns with identical formulas (80% or more overlap) and are reported as one group. Only calculated columns count, because elements that read the same table share pass-through column names by definition. Filters and layout are not compared. |
| Unused hidden columns | Hidden calculated columns in the workbook that no formula, filter, sort, grouping or control refers to. |

Always present these as leads to verify, not instructions. Columns are matched by name, so the tool cannot prove two same-named columns hold the same values; each finding says what to confirm. Regional copies (same element name in different data models) and elements that only share pass-through columns are skipped on purpose.

## 4. Share the result

Offer this after the first successful run. Do not publish anything without the user saying yes.

1. **Review first.** The page contains workbook and element names, column formulas, warehouse table paths, and Sigma owner names. Ask the user to confirm that is fine for the audience.
2. **Always available:** send `standalone.html`, a complete page that opens in any browser with no server. Or point `--out` at a team-synced folder.
3. **In Claude, with the Artifact tool:** publish the saved `index.html` as an Artifact (it is the version meant for publishing; Artifacts accept up to 16 MB, and pages for very large workbooks can be several MB).
   - **Read the sharing line in the publish result** and report it exactly. Sharing defaults can differ between organizations, so never tell the user the page is private or shared with the organization unless the result says so. You cannot change sharing yourself.
   - **Tell the user to open the page's Share menu** to choose who can open it. Teammates need a claude.ai account in the same organization; anyone outside it cannot open the link unless the owner invites them.
   - **Updating later:** re-run the trace, then publish the new `index.html` to the same artifact URL so the link stays the same.
4. **In other assistants,** there is no Artifact step. Use the standalone file.

## 5. How to read the results (include this when presenting them)

- **Pass-through** means the column is a plain reference all the way down. **Calculated** means at least one layer applies a formula, and that layer's owner is listed.
- **Owner** is the Sigma owner of the workbook or data model where the logic sits. It is the place to start asking, not proof of who is responsible for the business logic.
- **Partial / hidden upstream** means the chain reaches a data model whose upstream element the API does not return. The model and its owner are shown instead.
- **Tracing is name-based.** Columns are matched by name through formulas, so unusual naming can mislead.
- **Recommendations** are heuristics. Before removing anything, check that the values really match, and that nothing outside the traced workbook (other workbooks, saved views, actions) depends on it.
- **Open links** open the object in edit mode at the element: `<object URL>/edit?:nodeId=<elementId>`. Workbook links are reliable; data-model links may not open the exact element. Viewers need edit access to land in the editor.

## 6. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `readable by other users` | `chmod 600` the credentials file |
| `Refusing API base URL` | Use the https Sigma API host for your cloud |
| `No Sigma sign-in is available` | Run `run.py login` in a terminal, or add API client credentials |
| `login` says browser sign-in is unavailable | This host or account cannot use it; use API client credentials |
| `Sigma did not issue a token` after signing in | Your account type may lack the "Use Sigma API with OAuth" permission; ask an admin |
| `Sign-in timed out` | Finish the sign-in within two minutes, or run `login` again |
| `No OS keychain was found` | Set `SIGMA_OAUTH_STORE=file`, or use API client credentials |
| `saved Sigma sign-in has expired` | Run `run.py login` again |
| 401 / 403 | Wrong host, an expired or revoked sign-in, or the signed-in user cannot see that object |
| `Could not find a workbook` | Link is for another organization, or the API user lacks access |
| Many "hidden upstream" columns | A data model the API user cannot fully read; use a user with view access to those models |
| Slow or 429 errors | The script retries with backoff; run again later for very large workbooks |

## What this does not do

Read-only: it never changes a workbook, data model or permission. It does not trace lineage inside the warehouse (dbt or SQL models), only through Sigma objects down to the table. It cannot tell who is accountable for a table unless you provide an owners file.
