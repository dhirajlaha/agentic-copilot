# PRD: Agentic Dev/Test Copilot

The product requirements this project is actually built to satisfy — and the gaps in the technical spec that this surfaces.

## Problem & Purpose

Most portfolio projects demo well but solve nothing real — that's the exact credibility gap this project closes. Given a real, scoped engineering ticket sourced from an actual public GitHub issue, an agent pipeline should independently propose a fix, generate tests for it, run those tests, and report pass/fail — end to end, with measurable results against real code, not synthetic problems. Shipped as a lightweight multi-tenant SaaS (GitHub OAuth, connect-your-own-repo) rather than a single-user script, so it can also generate real usage numbers to show in interviews.

Secondary purpose: the same pipeline is the hands-on vehicle for the agentic orchestration, RAG, and LLM-serving-optimization learning in the main tab's curriculum.

## Scope

**In scope**:

- 10-20 curated, well-scoped issues from 1-2 real public repos (a mid-size Python or TypeScript library — small enough that the agent's context window and your eval effort both stay tractable) — used for the internal correctness benchmark; the live product accepts any repo a user connects
- Automated fix generation, test generation, and sandboxed test execution for those issues
- A benchmark comparing a self-hosted quantized model (vLLM + PagedAttention) against an API-based model on the same issues
- A dashboard showing live agent traces and the eval results, gated behind GitHub OAuth sign-in, with a public landing/pricing page for discovery

**Out of scope**:

- Auto-submitting pull requests to third-party repos (see Risks)
- Arbitrary, large, or poorly-specified issues and monorepos — this targets issues an agent can plausibly solve, not open-ended SWE-bench-scale generality
- Multi-user auth, billing, or production-grade reliability — this is a demo/portfolio system, not a shipped product

## Success Criteria

- **Correctness**: measured against closed issues that already have a merged fix PR — that merged diff is your ground truth. Report the % of curated issues where the agent's generated fix passes the same (or an equivalent) test suite as the real fix. Open, unresolved issues have no verified answer and can't be graded this way — keep those for a qualitative demo only, not the headline number.
- **Inference optimization**: latency, throughput, and cost per run, self-hosted quantized model vs. API-based model, on the same set of issues.
- **Interview-readiness**: can you state, with numbers, what % of real issues it solved and explain why the rest failed — that story matters more than a 100% pass rate.

## Non-Goals

- Not a general autonomous software engineer — targets scoped, well-described bugs/small features, not architecture-level changes
- Not a replacement for, or reproduction of, confidential work from your current employer
- Not intended to post AI-generated PRs to third-party maintainers without consent — see Risks. Opening a PR back to a user's own connected repo, with their explicit approval, is a core product feature, not an exception to this

## Risks & Open Decisions (the real gaps)

1. **Evaluation ground truth**: open GitHub issues have no verified "correct" answer. Use closed issues with a merged fix PR as your labeled set so correctness is actually gradable — the technical spec didn't say this explicitly until now.
2. **Sandbox security**: the Test Executor runs LLM-generated code. A bare Python `subprocess` is not a safe boundary once this is publicly reachable — run generated code in an ephemeral, network-disabled container (`--network none`, read-only mounts, strict CPU/memory/pid limits), not a host subprocess, by the time it's on EKS.
3. **Cost/token budget caps**: nothing in the current spec caps total LLM spend per run or per day. Add a hard budget (max tokens or max $ per run, plus a daily ceiling) before this is reachable by anyone but you.
4. **Ethics of acting on real repos**: cloning a repo and running tests locally is fine; auto-opening PRs against someone else's project without review is a known source of low-quality "AI slop" complaints in open source. Decision: this project reads issues and proposes fixes locally/in a fork — it does not open unsolicited PRs on third-party repos.
5. **Repo/issue curation effort**: picking 10-20 well-scoped issues with verifiable fixes is real work, not a throwaway step — budget part of week 4 specifically for this, separate from building the ingestion pipeline itself.

None of these block starting week 1. #1, #4 and #5 matter from week 4 onward (RAG/eval); #2 matters before week 10 (cloud deployment); #3 matters once anything is publicly reachable.
