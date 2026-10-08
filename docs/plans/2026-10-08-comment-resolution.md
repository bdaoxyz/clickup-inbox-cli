# Resolve and reopen comments from the CLI

## Goal

Add single-comment resolution and reopening using the CLI's existing saved
ClickUp browser session. Users can take the exact `comments[].id` returned by
Assigned Comments and change its resolved state directly.

## Interface and behavior

- `clickup-inbox comments resolve COMMENT_ID` sets `resolved` to `true`.
- `clickup-inbox comments reopen COMMENT_ID` sets `resolved` to `false`.
- Both accept `--json`, returning `{ "id": COMMENT_ID, "resolved": BOOLEAN }`
  after an accepted update; otherwise print a short confirmation.
- Change only the resolved field. Preserve text, rich formatting, assignee,
  thread identity, and existing listing behavior.
- Reuse `load_credentials`, automatic session refresh, the configured API
  origin, session headers, redirect blocking, and safe transport errors.
- Require a nonempty string ID and encode it as one URL path segment.
- Do not add bulk changes, reassignment, comment editing, or a second auth mode.

## Implementation

1. Establish the browser-session update method, path, and payload from the
   current ClickUp web client. The public Update Comment API is background
   documentation, not proof of the private session contract.
2. Add failing CLI and client tests for resolve/reopen, minimal payload,
   authentication headers, encoded IDs, and error behavior.
3. Implement `AssignedCommentsClient.set_resolved(comment_id, resolved=...)`
   and add the two comment subcommands using that method.
4. Update README examples and the connector handoff section, retaining the
   connector instructions for the other comment operations.
5. Run the full unittest suite and CLI help smoke checks; review the change.

## Files

- `src/clickup_inbox_cli/comments.py`
- `src/clickup_inbox_cli/cli.py`
- `tests/test_comments.py`
- `tests/test_comments_cli.py`
- `README.md`

## Verification

Tests exercise the real CLI/client through the HTTP boundary with synthetic
credentials and responses. Assert exactly one request, only `resolved` in the
body, success output after an accepted response, and no success output on HTTP
failure. Existing assigned/delegated, reply, inbox, and auth tests must pass.
Read the live web client's implementation without resolving anyone's actual
work as a test. A live mutation requires a user-designated comment.

## Sources

- Existing `AssignedCommentsClient`, `InboxClient._request`, and CLI patterns.
- [ClickUp public Update Comment documentation](https://developer.clickup.com/reference/updatecomment).
- Current ClickUp web client update implementation, to be recorded in the
  verification receipt once inspected.
