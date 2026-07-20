# Conductor recorded-stream evaluations

These fixtures replay production artifact metadata and events through the real
Conductor. They are offline: only the closed-list classifier answer is read
from a fixture; routing, guards, capability checks, termination, persistence,
recovery, and delegation minting are production code.

To record a fixture from a real (non-secret) run:

1. Copy only `artifact_published` records needed for the scenario, preserving
   their order, and redact bodies and project/session names.
2. For each record, copy the corresponding `meta.json` fields that affect
   decisions (`id`, `type`, `status`, `content_hash`, `lineage`, `supersedes`,
   `depth`, `hop_count`, and `source`). Put these objects in `stream.jsonl`.
3. Copy the applicable routing rules and goal manifest. Replace any classifier
   output with the selected member of the closed candidate set in
   `classifier_answers.json`; never include a prompt or model transcript.
4. Run `python3 -m unittest tests.conductor_evals.test_evals`. Inspect the
   normalized decision list and write it to `expected.json`. Then deliberately
   change one decision once and confirm the mutation test reports the fixture,
   step, expected/actual values, and ledger tail.

Do not copy credentials, model output, absolute paths, costs, or timestamps.
The checked-in goldens intentionally exclude wall-clock fields and temporary
paths so consecutive runs are byte-identical.
