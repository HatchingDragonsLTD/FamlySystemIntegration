# Famly integration
- Read docs/status.md at the start of a session.
- Python, stdlib unittest. One module: python -m unittest tests.test_x -q. Full suite: python -m unittest discover -s tests -q (slow; once at the end).
- Layout: actions/<name>/{runner.py,cli.py,web.py}; web_registry.py routes webhook actions; core/ clients; integrations/ Slack and catalogues; reference/ JSON (pulled by pull-* commands); var/ is gitignored personal data.
- Never print, log or store child or parent names. Child ids only.
- Exception for the onboarding pipeline: names and emails may be read from HubSpot at run time and held in memory for a single call; never store, print or log them.
- Hard errors for anything that could misprice a plan; warnings only for cosmetic issues. Never guess a missing id.
- Do not touch the commit path (approval.py, runner.commit) unless the task says so. Never call a write or delete against Famly outside tests.
- Famly's plan `to` is exclusive; segments are inclusive internally.
- Tests use fakes. Hit live Famly only when the task says to verify live.
- Testing: for money, funding, dates, commit and delete guards and queue safety, test thoroughly. For everything else write at most about 8 focused tests. No test-breaking checks unless the task asks. Run only the affected modules with -q, and the full suite once at the end, using: python -m unittest discover -s tests -q 2>&1 | tail -5

