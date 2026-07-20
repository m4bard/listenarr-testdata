# Testing conventions

Most of the suite tests the ordinary thing: **good input → correct output.** Does the generator
lay out the right folders, embed the right tags, produce a stable manifest, reconcile it. That is
necessary and it is the bulk of the tests.

But this repo is a *conformance harness* — a tool whose entire job is to emit a pass/fail verdict
about someone else's scanner. For a tool like that, the scariest bug is not wrong output, it is a
**wrong verdict**: a broken scan that reports green, or a correct scan scored red. A harness that
lies about its verdict is worse than no harness — it launders a broken branch into a checkmark, or
cries wolf until people stop trusting the green.

So there is a second, smaller category of test here, and it has a different shape.

## Verdict / safety contract tests

Marked with `@pytest.mark.contract`. Run just them:

```bash
python -m pytest -m contract
```

A contract test does not check "correct input → correct output." It **constructs a state where the
tool must reach a specific judgment, then asserts the judgment — including the exit code, not just
the printed table.** It is adversarial against the tool's own verdict: it tries to make the tool
lie, and proves it cannot.

Three sub-contracts, each with living examples in the suite:

1. **Fails when it should (no false PASS).**
   For every gate, feed a *known-broken* state and assert a non-zero exit.
   → `TestStrictExitCode` (0 links + `--strict` exits 1), `TestGate` (a regression fails the diff).

2. **Honors its own thesis (no false FAIL).**
   Encode the repo's headline claims as assertions, so a correct-but-non-obvious scanner passes —
   and pin the *edges*, so the rule doesn't rot into "anything passes."
   → `TestWorkEquivalence` (a link to a work-equivalent ASIN passes; wrong position / unrelated
   ASIN still fail), driven off the corpus's real twin pairs.

3. **Degrades loudly (no false PASS via silence).**
   Every data source must **raise** on a moved schema/endpoint, never return an empty result that
   reads as a clean run. Every machine artifact must serialize a broken run as `fail` /
   `inconclusive`, never a green.
   → `TestSourceRotIsLoud` (a moved schema raises), `TestMachineOutput` (a broken run serializes to
   `fail`; a rotted source to `inconclusive`), `TestDedup` (a work-level failure makes `overall`
   fail even when every per-file link is correct).

## The rule for new work

Every feature that produces or influences a verdict — a new scenario, a new check, a new output
format, a new gate — ships with at least one `contract` test. Before writing it, ask the one
question the category exists to force:

> **How would this thing lie to me, and what test makes that lie impossible?**

If a feature *cannot* emit a verdict (pure generation, formatting, a helper), it does not need a
contract test — the ordinary output tests are enough. The marker is for the judgment surface only.

This convention is the durable form of the discussion in issue #15.
