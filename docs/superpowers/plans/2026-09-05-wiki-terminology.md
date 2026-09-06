# Wiki and terminology implementation plan

Execute inline in the current task. The user explicitly prohibits subagents.

## Wiki quality

- [x] Add regressions in `tests/test_wiki.py` and `tests/test_wiki_review.py` for
  missing Chinese names, nested language fields, persistent rejection, safe batch
  scope, idempotent repair and refreshing counts.
- [x] Use `mwparserfromhell` in `tgmon/kb/wiki.py`; share review operations through
  `tgmon/kb/wiki_review.py` and run legacy repair from `tgmon/bootstrap.py`.
- [x] Add filters, checkboxes, batch commands and corrected candidate inputs in
  `tgmon/admin/templates/fragments/wiki_review.html`; route actions through
  `tgmon/admin/routes/glossary_ui.py`.

## Translation quality

- [x] Add regressions for wrong-game terms, untranslated approved names, overlapping
  aliases, protected URLs/code, cached translations and unresolved Chinese errors.
- [x] Update `tgmon/glossary.py` and `tgmon/translate.py` to scope, validate and
  normalize approved terminology consistently across bot and ingestion paths.

## Verification and release

- [x] Run targeted tests until green, then full pytest and compile checks.
- [x] Exercise desktop/mobile review workflows in a local isolated app database.
- [x] Package the reviewed source, build on the server, preflight a database copy,
  deploy with a backup, and validate pages/model/translation/album dry-run.
- [x] Record exact release, migration counts and remaining external test limits.

Released `20260905-wiki-terms-05`. All 261 tests passed; runtime hashes and public
health passed. Recovered 2116 Chinese Wiki candidates and rejected 261 invalid
pages, retaining all 2377 original pages. All 2474 live index documents have
vectors. Translation uses a simulated provider for verification; QQ upload and
message delivery remain local dry-runs. Evidence is in
`D:/hongkong/task-artifacts/20260905-wiki-03/verification-result.json`.
