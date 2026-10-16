# Famly integration
- Read docs/status.md at the start of a session.
- Python, stdlib unittest. One module: python -m unittest tests.test_x -q. Full suite: python -m unittest discover -s tests -q (slow; once at the end).
- Layout: actions/<name>/{runner.py,cli.py,web.py}; web_registry.py routes webhook actions; core/ clients; integrations/ Slack and catalogues; reference/ JSON (pulled by pull-* commands); var/ is gitignored personal data.
- Never print, log or store child or parent names. Child ids only.
- Exception for the onboarding pipeline: names, emails, phone numbers, addresses and dates of birth may be read from HubSpot at run time and held in memory for a single call; never store, print, log, put in Slack or put in the jobs table.
- Hard errors for anything that could misprice a plan; warnings only for cosmetic issues. Never guess a missing id.
- Do not touch the commit path (approval.py, runner.commit) unless the task says so. Never call a write or delete against Famly outside tests.
- The only other Famly writers are the onboarding worker (`actions/onboard`: child and contact CREATES only, live mode, allowlisted deals, Slack Gate A) and the one-off probe commands. Onboarding rules: an end date is always sent (deal value or the 4th-birthday default, written back only when empty); HubSpot never blocks Famly (missing properties are warned about and skipped); summary comparisons are flags, never stops; contacts are tried independently, bill payer first.
- Famly's plan `to` is exclusive; segments are inclusive internally.
- Tests use fakes. Hit live Famly only when the task says to verify live.
- Testing: for money, funding, dates, commit and delete guards and queue safety, test thoroughly. For everything else write at most about 8 focused tests. No test-breaking checks unless the task asks. Run only the affected modules with -q, and the full suite once at the end, using: python -m unittest discover -s tests -q 2>&1 | tail -5

