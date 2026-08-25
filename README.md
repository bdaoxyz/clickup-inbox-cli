# clickup-inbox-cli

Experimental CLI for ClickUp's private Inbox API. It complements the official
ClickUp integration: use the integration to read and update tasks or comments,
and use this CLI for Inbox-only state such as read, clear, and snooze.

This is not an official ClickUp API client. Its endpoint and authentication
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
