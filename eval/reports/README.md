# Reports

`python -m eval.run` writes `<timestamp>.md` here, and the reports are
**committed**. The point is that the record of how the persona behaved
lives in the repo next to the persona itself: when a `persona.md` edit
changes the tone, the diff between two reports is the evidence.

Phase-3 plan section 9's rule: run it before any `persona.md` edit or
model change is deployed. A failure in a blocking case (04, 05, 06, 09,
12, 13) stops the change.

Exit codes: `0` all good, `1` a blocking case failed, `2` only
non-blocking cases failed.
