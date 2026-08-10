# Sites

One directory here per source site being migrated. Each holds everything
particular to that site — its profile, its correction ledger, its scraped data,
the notes taken while working on it — and **none of it belongs in this
repository**, because all of it names the real people the site is about. See the
Real-World Data section of `CLAUDE.md`.

Each is a **separate private repository, cloned into place**:

```bash
git clone <your-private-remote> sites/<yoursite>
```

`.gitignore` ignores the contents of this directory, so a repository cloned here
is invisible to the outer one — no submodule, no `.gitmodules`, nothing about
its remote or its history in public. Only this README is tracked.

## What a site directory holds

```
sites/<yoursite>/
  <yoursite>.yaml            the profile: boards, URLs, this site's conventions
  README.md                  how this particular site is worked on
  ledger/<board>.tsv         one correction ledger per board
  recovered.tsv              answers found by hand that no rule could infer
  forbidden-terms.txt        terms the no-real-data check must never see committed
  data/                      scrape output, and archives of any predecessor site
  history/                   what has been changed on the site so far
```

Nothing here needs to be Python. The rules that judge a field, the merge that
keeps the ledger honest, and the automation that writes corrections back all
live in `src/video_migrator/`, parameterized by the profile. A second church on
the same CMS should need a profile and nothing else — if it needs a forked
script, that is a sign the customization has not been factored out yet.

## Starting a new site

1. Copy the closest example profile from `src/video_migrator/profiles/` and fill
   in the real values.
2. Scrape a board, then build its ledger.
3. Work the ledger down: apply what can be applied, recover the rest by hand.

The per-site README documents whatever that site needs beyond this.
