# TigerTest Intelligence-First US Tech Selector

TigerTest is a US technology research system for two themes: the semiconductor cycle and AI infrastructure. Each run freezes up to 16 high-liquidity securities for deep research, checks the original evidence for each name, and records paper plans, watch conditions, and evidence gaps.

The system uses the [Caishen trading kernel](docs/CAISHEN_README.md) and [WorkLoom governance workspace](governance/README.md). See the [TigerTest architecture](docs/TIGERTEST-ARCHITECTURE.md) for the research universe and evidence requirements.

**This system currently runs in paper trading only. It is not investment advice and promises no returns.**

## Architecture: Trading Kernel + Governance Shell

```
┌──────────── WorkLoom IM Governance Shell (governance/, TypeScript) ───────────┐
│ Five-element event store (hash-chain audit) · Fence engine (white-paper       │
│ thresholds → block-level baseline) · Quest orchestration · night-shift watch  │
│ · Approval cards · Organizational memory · site (accountability UI)           │
│ ┌────────── Caishen Trading Kernel (repo root, Python, fully preserved) ────┐ │
│ │ 21-step pipeline · six-layer decision stack (L0 scan / L1 MRS / L2 SHS /  │ │
│ │ L2b ICS / L3 TSS / L4 risk) · SearchHub 6 sources · 7-ring quote fallback │ │
│ │ · zero-baseline discipline · journal → WFA → DSR · paper-trading engine   │ │
│ │ · self-contained HTML daily report · review loop (Zhuge team: daily       │ │
│ │ attribution / weekly checkup / monthly WFA proposal with approval flow)   │ │
│ │ · engineering red lines (LLM irreversibility / passthrough logging /      │ │
│ │ step roll-call)                                                           │ │
│ └───────────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────────┘
```

- **Trading kernel**: the repo root is the Caishen system; [original README here](docs/CAISHEN_README.md). The decision stack, thresholds, and risk formulas use `trading_system/config.py` as the single source of truth.
- **Governance shell**: `governance/` is the WorkLoom IM base — governance only, never decisions. It turns every trading action into an accountable event and turns white-paper red lines into a second, machine-level lock. Integration design: [docs/GOVERNANCE.md](docs/GOVERNANCE.md).
- **Industry role bundle**: `governance/bundles/trading/` — AI fund-manager team presets, three-layer fence packs, and investment skills (a stub pointer remains at `bundles/trading/`).

## Quick Start (Trading Kernel)

For dialogue-only operation through Codex or Qoder, start with the
[operator guide](docs/TIGER-OPERATOR.md) and [client setup](docs/AGENT-CLIENTS.md).
The outer agent prepares fixed CLI/MCP requests, preserves the original task
identity through reconnects, and reports completion only after independent
receipt verification. Keep one trusted state root for a continuing paper account
and profile; a new daily task changes its logical idempotency key.

```bash
bash scripts/setup.sh full
# backend-only: bash scripts/setup.sh backend
python3 -m pytest tests/ -q                              # trading-kernel test suite
python3 main.py --demo --universe tech --top 16 --picks 5 # offline two-theme demo
python3 main.py --universe full --top 30 --picks 8        # historical full-universe comparison

# S6 review loop & approval flow
python3 main.py --review-list                              # list WFA parameter proposals
python3 main.py --review-approve <id>                      # approve → effective next day, disclosed
python3 main.py --review-reject <id> --reason "..."        # reject (reason required)
```

## Controlled Research and Evidence Acquisition

New controlled AI research selects `us-tech-intelligence-paper-v4` through the
TigerTest CLI/MCP workflow. It freezes a two-theme research frontier, collects
original documents and attachments from registered sources, checks each
security’s eligibility, and binds the evidence in an immutable bundle.
Missing evidence remains a gap with null semantic scores. All five semantic
stages still require the admitted DeepSeek model; optional discovery providers
do not replace that text model or numeric risk gates. Historical profiles and
their original receipts retain their original verification rules.

The independent `node scripts/tiger-intelligence.mjs sync|monitor|status|evidence`
entry defaults to read-only access and has bounded, trusted startup options. It
does not call a text model, connect an account or place an order. The offline
benchmark ships 100 explicitly synthetic contract fixtures across five strata;
these are not human-labelled financial gold or measurements of real providers.
See [the acquisition guide](docs/INTELLIGENCE-ACQUISITION.md) and
[the Agent guide](docs/AGENT-CLIENTS.md) for requests, credentials, limits,
failure states and production-validation boundaries.

## The Ten Iron Rules of the White Paper (highest discipline; no upgrade may violate them)

1. **Edge is process, not prediction**: permission first (MRS) → mainline (SHS) → chain prosperity (ICS) → entry timing (TSS)
2. **Deliberately unearned money**: no earnings gambles; no forcing trades when MRS*<4; honest failure when the data foundation is unsound
3. **Hard entry logic**: standard long requires MRS*≥6 and SHS≥7.5 and TSS_final≥7.2; light-probe channel sizes ×0.30–0.40; no new positions when MRS*<4.0
4. **Mechanized risk control**: 1R = 0.8% of equity; single name ≤20%; structural/time stops; profit protection at ≥2R; Kill Switch
5. **Data engineering**: multi-source fallback chain never falls back to synthetic data; zero-baseline discipline restarts every run from scratch
6. **Engineering red lines**: 21-step roll-call — a missing step is an incident; LLM steps are irreversible (no rule-based fallback); exceptions passthrough with logging
7. **Iteration honesty**: WFA + DSR correction; insignificant results keep default parameters
8. **Public paper-trading verification**: fully AI-managed, gains and losses reported truthfully, no cosmetic framing
9. **AI division of labor**: semantics to LLMs, numbers to rules, gates must be deterministic
10. **Consistency**: same input, same output — the premise of backtesting, review, and iteration

## Document Map

| Document | Description |
|---|---|
| [docs/CAISHEN_README.md](docs/CAISHEN_README.md) | Full documentation of the Caishen trading kernel (v6.3) |
| [reports/AI短线美股交易白皮书_20260730.pdf](reports/AI短线美股交易白皮书_20260730.pdf) | Trading philosophy white paper (highest discipline, Chinese) |
| [docs/GOVERNANCE.md](docs/GOVERNANCE.md) | WorkLoom governance shell integration design |
| [docs/PROJECT_INTRO.md](docs/PROJECT_INTRO.md) / [docs/PROJECT_INTRO_EN.md](docs/PROJECT_INTRO_EN.md) | Project introduction (Chinese / English) |
| [research/UPGRADE_PLAN_v3.md](research/UPGRADE_PLAN_v3.md) | Upgrade plan v3 (Caishen-centric edition, confirmed) |
| [research/01_benchmark.html](research/01_benchmark.html) | Research 1: deep dive on industry benchmarks (Bridgewater AIA and 8 others) |
| [research/02_frontline.html](research/02_frontline.html) | Research 2: frontline investment-team workflows and know-how |
| [docs/AGENT_CENSUS.md](docs/AGENT_CENSUS.md) | Agent census (20 agents/modules classified) |
| [docs/DATA_HYGIENE.md](docs/DATA_HYGIENE.md) | Data hygiene discipline |
| [docs/INTELLIGENCE-ACQUISITION.md](docs/INTELLIGENCE-ACQUISITION.md) | Registered-source acquisition, evidence bundles and offline evaluation |
| [docs/PUBLIC_VERIFICATION.md](docs/PUBLIC_VERIFICATION.md) | Public paper-trading verification charter |

## Compliance Statement

This system is a technical research and simulation project: **paper trading only, no real orders**; it is not investment advice; free market-data sources carry delays (annotated in reports). Live trading would require separate integration with a licensed broker and compliance with each market's regulations (US SEC 15c3-5, CN program-trading filing, HK SFC).
