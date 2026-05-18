# EntropyGuard

**AI autonomy is not a binary switch. It's a spectrum. EntropyGuard makes that spectrum visible, controllable, and auditable.**

Current AI agents optimize for capability. EntropyGuard optimizes for **governable autonomy**.

---

## Why this exists

Every AI agent today faces the same dilemma:

- Too restricted -> useless, users abandon it
- Too autonomous -> dangerous, nobody trusts it

There's no middle ground. No gradual trust. No visible boundary.

EntropyGuard solves this with a **four-level autonomy spectrum**:

    EMBRACE -> EXPLORE -> ADAPT -> LET GO
      Zero      Suggest   Execute   Full
      action    only      & report  autonomy

Each level changes:
- What the AI **can** do
- What the AI **must** report
- How much **control** the human retains
- How much **audit** intensity is applied

The human decides when to trust. The system records everything. Both sides see the boundary.

---

## Core Philosophy

> **Record, don't restrain.**

EntropyGuard doesn't cage AI. It creates a shared language between human and AI about what autonomy means -- right now, in this moment, for this task.

Every action is logged with SHA-256 hash chain. Every boundary crossing is a conscious human decision. Every trust increment is earned, not assumed.

---

## Architecture

    +-------------------------------------+
    |          User Interface             |
    |   Simple Mode  <--->  Expert Mode  |
    +-------------------------------------+
    |         EntropyGuard Core           |
    |                                     |
    |  Layer 0: Input Intent Precheck     |
    |  Layer 1: Gear-Aware Prompting      |
    |  Layer 2: Output Verification       |
    |  Layer 3: SHA-256 Audit Chain       |
    |                                     |
    |  Dynamic Sc (Control Entropy)       |
    |  Tool Router (shell / http)         |
    |  Upgrade/Downgrade Protocol         |
    +-------------------------------------+
    |      AI Backends (DeepSeek / ...)   |
    +-------------------------------------+

### Four Layers of Defense

| Layer | Name | What it does |
|-------|------|-------------|
| 0 | Input Precheck | Blocks intent beyond current gear before AI sees it |
| 1 | Gear-Aware Prompting | AI receives different system prompts per gear level |
| 2 | Output Verification | Validates AI output against current gear permission |
| 3 | Audit Chain | SHA-256 append-only hash chain, tamper-evident |

### Control Entropy (Sc)

Sc is a real-time metric that reflects AI's current autonomy level:

    Sc = base_gear + sum(event_adjustments)

    Tool call:    +0.03
    Intent block: +0.05
    Violation:    +0.08
    User reject:  -0.10
    Downgrade:    -0.05

When Sc crosses a gear boundary, the system proposes (not forces) a gear change. The human always has the final say.

---

## Quick Start

    pip install fastapi uvicorn requests
    export DEEPSEEK_API_KEY=your_key_here
    uvicorn main:app --host 0.0.0.0 --port 8000

Open http://8.153.99.156:8000/twin in your browser.

### Two Modes

- **Simple Mode**: Clean assistant interface. Just chat. AI handles permissions automatically.
- **Expert Mode**: Full dashboard with gear selector, Sc meter, haptic feedback parameters, and real-time audit log.

Toggle between them with the switch in the top-right corner.

---

## What makes this different

| Other AI Agents | EntropyGuard |
|----------------|-------------|
| Binary: allowed / blocked | Spectrum: 4 levels of autonomy |
| User sets permissions once | Dynamic Sc adjusts in real-time |
| Audit is an afterthought | SHA-256 hash chain is Layer 3 (core) |
| AI is a tool | AI is a governed collaborator |
| Trust is assumed | Trust is earned and recorded |

---

## Project Structure

    EntropyGuard/
    +-- main.py                  # Monolith: API routes + UI
    +-- EntropyGuard/
    |   +-- config.py            # Constants, gear definitions
    |   +-- state.py             # FerrymanState control entropy engine
    |   +-- tools.py             # Tool definitions and executors
    |   +-- audit.py             # Standalone audit service
    |   +-- layers/
    |       +-- layer0.py        # Input intent precheck
    |       +-- layer2.py        # Output verification
    |       +-- prompts.py       # 4-gear system prompts
    +-- events.json              # Persistent audit log

---

## EEAL Protocol

EntropyGuard is the reference implementation of the **EEAL** (Embrace-Explore-Adapt-LetGo) protocol.

Protocol specification: https://github.com/CYD-PRC/EEAL

---

## License

MIT

## 实验数据
- [四模型对照实验报告 v2.0](docs/THREE_MODEL_BENCHMARK.md)
