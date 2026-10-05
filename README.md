# clickup-inbox-cli

Experimental CLI for ClickUp's private Inbox and Assigned Comments APIs. It
complements the official ClickUp integration: use this CLI to discover your
assigned/delegated comments and manage Inbox state, and use the connected ClickUp
MCP to resolve/reopen, reassign, edit, delete, or reply to comments.

This is not an official ClickUp API client. Its endpoints and authentication
contract were observed from the ClickUp web application and can change without
notice.

## Install

On macOS, install the persistent Chrome helper and Keychain backend:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[macos]'
```

On a Linux VM, install only the browser helper. The CLI automatically uses a
private file instead of Keychain:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[browser]'
```

The helper uses the installed Google Chrome application. It does not require a
separate Playwright browser download.

## One-time login

```sh
.venv/bin/clickup-inbox auth login --workspace-id YOUR_WORKSPACE_ID
```

This opens a dedicated Chrome profile. Complete the ClickUp login once (using
1Password autofill or an agent that has been explicitly granted access), then
leave the Inbox page open until the helper reports success. The Chrome profile
keeps ClickUp's browser session. The CLI stores only the short-lived request
credential bundle in the selected credential store; it does not extract or copy
cookies.

Normal renewal is invisible:

```sh
.venv/bin/clickup-inbox auth refresh
.venv/bin/clickup-inbox auth status
```

Run `auth login` again only when ClickUp invalidates the persistent browser
session or requires MFA. `auth logout` deletes the short-lived credential
bundle; it intentionally leaves the dedicated Chrome profile intact so logout
does not silently destroy the long-lived session.

## Linux VM with 1Password

Use 1Password for the ClickUp username, password, and any agent authorization.
During the one-time `auth login`, retrieve those values through the 1Password
CLI or browser integration and enter them into the dedicated Chrome profile.
Do not pass a password as a command-line argument or write it to an `.env` file.

The VM defaults to this storage layout:

- Browser session: `${XDG_STATE_HOME:-~/.local/state}/clickup-inbox-cli/chromium-profile`
- Short-lived request bundle: `${XDG_STATE_HOME:-~/.local/state}/clickup-inbox-cli/session.json`

The directory is forced to mode `0700` and the session file to `0600`. Writes
are atomic. To make the selection explicit for an agent service:

```sh
export CLICKUP_INBOX_CREDENTIAL_STORE=file
export CLICKUP_INBOX_SESSION_FILE="$HOME/.local/state/clickup-inbox-cli/session.json"
.venv/bin/clickup-inbox auth login --workspace-id YOUR_WORKSPACE_ID
.venv/bin/clickup-inbox auth refresh
```

The agent can then run normal list and mutation commands without 1Password or a
visible browser until ClickUp invalidates the persistent browser session.

You can override storage per invocation. Global options must precede the
command:

```sh
.venv/bin/clickup-inbox --credential-store file --session-file /secure/path/session.json auth status
.venv/bin/clickup-inbox --credential-store keychain auth status
```

## List Inbox bundles

```sh
.venv/bin/clickup-inbox list --limit 20
.venv/bin/clickup-inbox list --unread --json
.venv/bin/clickup-inbox list --folder later --json
.venv/bin/clickup-inbox list --folder cleared --json
.venv/bin/clickup-inbox list --cursor '<next_cursor>' --json
```

JSON output is an object with `bundles` and `next_cursor`; pass a non-null
`next_cursor` back to `list --cursor` to enumerate the next page. Mutation
commands require the exact `bundles[].id` returned by `list --json`. This is a
bundle snapshot ID, not a ClickUp task ID:

```sh
.venv/bin/clickup-inbox read "$BUNDLE_SNAPSHOT_ID"
.venv/bin/clickup-inbox unread "$BUNDLE_SNAPSHOT_ID"
.venv/bin/clickup-inbox clear "$BUNDLE_SNAPSHOT_ID"
.venv/bin/clickup-inbox unclear "$CLEARED_BUNDLE_SNAPSHOT_ID"
.venv/bin/clickup-inbox snooze "$BUNDLE_SNAPSHOT_ID" --until '2026-08-26T08:00:00-05:00'
.venv/bin/clickup-inbox unsnooze "$SNOOZED_BUNDLE_SNAPSHOT_ID"
```

After clear or snooze, list the corresponding folder to obtain the new snapshot
ID before reversing the action. Snapshot IDs change when Inbox state changes.

## Assigned Comments

These commands reuse the same login and automatic session refresh as Inbox:

```sh
.venv/bin/clickup-inbox comments assigned
.venv/bin/clickup-inbox comments delegated --json
.venv/bin/clickup-inbox comments assigned --all --json
.venv/bin/clickup-inbox comments delegated --resolved --all --json
.venv/bin/clickup-inbox comments assigned --limit 20 --cursor '<next_cursor>' --json
```

Both views default to unresolved comments across **all assignment dates**.
ClickUp's web page defaults “Assigned to me” to the last 90 days; this CLI omits
that date filter so older assignments are included. “Delegated by me” matches
the web app's filter: assignments made by you to others, excluding yourself and
the special “Anyone” assignee. `--resolved` selects resolved comments only.
Assigned replies are included alongside top-level comments.

`--limit` controls page size (1–100; default 100). JSON output contains `comments`
and `next_cursor`. Pass that **response** cursor back with `--cursor`, or use
`--all` to collect every remaining page. Individual comment cursors are not
pagination cursors. Fetch all pages with the same view and resolved setting.

Your user ID is inferred from the saved web-session token. If your environment
credential is opaque, add `--user-id YOUR_NUMERIC_CLICKUP_USER_ID` to either view.

Each JSON row includes `id`, `task_id`, `parent_id`, `parent_type`, root parent
IDs/types, plain text, assignee/assigner/author IDs, resolved state, reply count,
and a task URL when applicable. Timestamp fields are Unix milliseconds as
returned by ClickUp. For assigned replies, `task_id` comes from the root task;
`parent_id` is the thread's parent comment ID. Non-task comments keep their
parent metadata and have an empty `task_id`/task URL.

### Hand off actions to the connected ClickUp MCP

The connected MCP already supports these operations, so the CLI does not
duplicate them. Given a row from `comments … --json`:

| Action | ClickUp MCP arguments |
| --- | --- |
| Resolve | `clickup_update_comment(comment_id=row.id, resolved=true)` |
| Reopen | `clickup_update_comment(comment_id=row.id, resolved=false)` |
| Reassign | `clickup_update_comment(comment_id=row.id, assignee=USER_ID)` |
| Read the task discussion | `clickup_get_task_comments(task_id=row.task_id)` |
| Read a reply's thread | `clickup_get_threaded_comments(comment_id=row.parent_id)` |
| Reply to a top-level task comment | `clickup_create_comment(entity_type="task", entity_id=row.task_id, reply_to_id=row.id, comment_text="…")` |
| Reply in an assigned reply's thread | `clickup_create_comment(entity_type="task", entity_id=row.task_id, reply_to_id=row.parent_id, comment_text="…")` |

Pass the CLI session's `workspace_id` to MCP when you have multiple workspaces.
For resolve/reopen and reassign, omit `comment_text` to preserve existing content.
Use task-comment operations only when `task_id` is non-empty; the JSON parent
metadata identifies other entities for the appropriate MCP operation.

## Environment-only fallback

The original environment-variable authentication remains supported. If any of
these variables is present, all four are required and take precedence over the
selected credential store:

```sh
export CLICKUP_WORKSPACE_ID="your-workspace-id"
export CLICKUP_INBOX_AUTHORIZATION="Bearer your-web-session-token"
export CLICKUP_INBOX_CSRF="your-csrf-value"
export CLICKUP_INBOX_SESSION_ID="your-session-id"
```

Avoid shell history, committed `.env` files, screenshots, and logs.

## Test

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Safety boundary

- Session cookies remain inside the dedicated Chrome profile.
- `auto` uses macOS Keychain on macOS and a private local file elsewhere.
- The file backend protects permissions but does not encrypt the file itself;
  use an encrypted VM disk and restrict the agent account.
- Human-readable output omits private bundle snapshot IDs; automation should use
  `--json`.
- HTTP errors never print response bodies or credential-bearing headers.
- Private endpoints may change without notice; use this as a local tool, not a
stable public integration contract.
