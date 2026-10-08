# Comment resolution verification

## Implementation

`comments resolve COMMENT_ID` and `comments reopen COMMENT_ID` send one
authenticated `PUT /comments/v2/comment/{encoded_comment_id}` request with
only `{"resolved": true}` or `{"resolved": false}`. They use the existing
credential loading, refresh, origin, transport, and error handling.

## Contract evidence

Read-only inspection of the current ClickUp browser client on October 8, 2026:

- [main6-974626825a.js](https://app-cdn.clickup.com/main6-974626825a.js)
  defines `apiUrlCommentsV2Prefix` as `apiUrlBase + "/comments/v2"`.
  `editCommentV2(id, changes)` sends PUT to that prefix plus `/comment/{id}`
  and passes the changes unchanged. The base is the API origin, without `/api`.
- [chunk92-b6d78824d3.js](https://app-cdn.clickup.com/chunk92-b6d78824d3.js)
  has `updateAssignedThreadedCommentEffect`, which calls
  `legacyCommentsService.editCommentV2(commentId, {resolved: params.resolved})`.

The browser was inspected using debugger script sources. No comments were
created, resolved, reopened, or edited. Debugger inspection was disabled after
the source was captured.

## Automated verification

- Baseline: 71 existing unittest tests passed.
- New CLI tests failed before implementation because `resolve` and `reopen`
  were unrecognized subcommands.
- New client tests failed before implementation because `set_resolved` did
  not exist.
- After implementation: all 77 unittest tests passed with
  `.venv/bin/python -m unittest discover -s tests -v`.
- Tests cover both booleans, state-only request bodies, exact update endpoint,
  saved-session headers, opaque authorization, encoded IDs, empty successful
  responses, input validation, text/JSON output, and safe HTTP error output.
- Editable CLI help shows both new commands; `comments resolve --help` shows
  the comment ID positional argument and `--json`.
- No lint or typecheck command is configured in this repository.

## Review

Completed focused correctness and independent adversarial review with no
actionable findings. The reviewer independently reran all 77 unit tests and
the resolve/reopen CLI help checks. Review verdict: ready to merge; all planned
requirements covered. Review run: `20261008-164059-74b56eff`.

## Limits

The new mutation was verified against the live web client's source and tested
at the HTTP boundary using synthetic credentials and responses. It was not
run against an actual ClickUp comment. The private API may change.
