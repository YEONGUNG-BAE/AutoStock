# Dev-only synthetic fixture builders

이 디렉터리의 script는 **production collector / orchestration entrypoint가 아니다.**

| Script | Output | Purpose |
|---|---|---|
| `build_synthetic_paper_loop_input.py` | `runtime/synthetic/paper_loop_input.<scenario>.SYNTH.json` | Layer B `run_paper_once.py` smoke |
| `build_synthetic_paper_review_input.py` | `runtime/synthetic/paper_review_input.<scenario>.SYNTH.json` | Phase 16 `build_paper_review_report.py` smoke |

공통 규칙:

- deterministic output (fixed timestamp / fixed ids)
- 모든 synthetic id는 `SYNTH-` prefix
- generated JSON은 **commit하지 않는다**
- LLM/Ollama/KIS/PaperBroker/PaperLoopRunner/ledger store 호출 없음

Daily paper pilot workflow: [docs/PAPER_PILOT_WORKFLOW.md](../../docs/PAPER_PILOT_WORKFLOW.md)
