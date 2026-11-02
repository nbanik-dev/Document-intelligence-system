# Disclosure of AI-assisted development

- **Code authoring:** the source code, tests, synthetic-dataset generator, documentation and this disclosure were
  generated with **Claude (Anthropic)** in a single interactive session. The human candidate directed the work.
- **Verification performed in the session:** the full automated test suite was executed (see `docs/TEST_RESULTS.txt`),
  the evaluation harness was executed against a generated labelled dataset (`docs/EVALUATION.md`), the large-PDF
  benchmark was executed (`docs/BENCHMARK_LARGE.md`) and the frontend production build was executed.
- **Not verified in the session (be transparent in the interview):** the Docker/PostgreSQL deployment path was not
  executed (no Docker or PostgreSQL server was available), the live LLM path was not exercised against the real
  API (no key; it is covered by mocked-client tests), and no real customer documents were evaluated.
- **Runtime AI:** optional LLM extraction (Anthropic API). Without a key the system runs fully on the rule-based
  extractor + OCR. Prompts are in `docs/PROMPTS.md`.
- **Candidate responsibility:** the candidate must read, run and be able to explain every module before submission.
