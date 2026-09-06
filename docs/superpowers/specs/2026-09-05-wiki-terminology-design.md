# Wiki review and terminology quality

Approved in the task on 2026-09-05. Existing invalid Wiki candidates are rejected
and hidden, never deleted. Previously deployed release: 20260905-local-qq-02.

- Parse Chinese candidate names from Wiki language and infobox fields; never use
  an English title as a Chinese canonical name.
- Keep rejection decisions across later synchronization. Only a changed valid
  translation candidate should reopen an approved page for review.
- Add selected-row and filtered batch review, pagination, editable Chinese names,
  review counts, and a rejected view. Refresh the list after actions.
- Migrate invalid imported Wiki records idempotently; preserve original pages.
- Use reviewed Chinese terminology scoped to the detected game. Correct surviving
  source terms with boundaries and longest-match precedence; protect URLs, code,
  ambiguous terms, numbers and already-correct names. Report unresolved misses.
- Validate cache and non-cache translation paths, migrations, review APIs and UI.
  Publish a new release only after local tests and server preflight pass.

No subagents. No automated QQ message delivery. Retain deployment backups.
