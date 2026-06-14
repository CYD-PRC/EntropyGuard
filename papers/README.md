# EntropyGuard — Publications

[![Zenodo PRE-GHR v3.0](https://zenodo.org/badge/DOI/10.5281/zenodo.20685899.svg)](https://doi.org/10.5281/zenodo.20685899)
[![Zenodo EESCF v2.0](https://zenodo.org/badge/DOI/10.5281/zenodo.20687118.svg)](https://doi.org/10.5281/zenodo.20687118)
[![Zenodo UHACD v1.0](https://zenodo.org/badge/DOI/10.5281/zenodo.20687498.svg)](https://doi.org/10.5281/zenodo.20687498)

This directory contains the LaTeX source code for three related papers on
human–AI authority dynamics and shared autonomy systems. All papers are
published on [Zenodo](https://zenodo.org/) under CC-BY-4.0 open access.

---

## Paper 1: PRE-GHR v3.0

**Title:** PRE-GHR v3.0: A Partially Observable Stochastic Control Framework
for Modeling Progressive Human–AI Authority Degradation in Autonomous Systems

**DOI:** [10.5281/zenodo.20685899](https://doi.org/10.5281/zenodo.20685899)

**Abstract:**
A formal framework for modeling long-term human–AI control dynamics as a
partially observable stochastic control system. Models human control as a
latent state variable evolving under autonomy pressure, external tool
expansion, and interaction noise. Introduces a discrete Gear-based
abstraction layer (1–5) with regime-switching structure.

**Citation:**
```bibtex
@misc{preghr2026,
  author       = {Anonymous},
  title        = {{PRE-GHR v3.0: A Partially Observable Stochastic Control
                   Framework for Modeling Progressive Human–AI Authority
                   Degradation in Autonomous Systems}},
  year         = {2026},
  doi          = {10.5281/zenodo.20685899},
  publisher    = {Zenodo},
  url          = {https://doi.org/10.5281/zenodo.20685899}
}
```

**Source:** `pre-ghr-v3.tex`

---

## Paper 2: EESCF v2.0

**Title:** EESCF v2.0: A Stochastic Control Framework for Delay-Constrained
Human–AI Shared Autonomy in Extreme Space Environments

**DOI:** [10.5281/zenodo.20687118](https://doi.org/10.5281/zenodo.20687118)

**Abstract:**
A stochastic control framework for human–AI shared autonomy under extreme
space environments with high communication latency and intermittent
connectivity. Formalizes autonomy allocation as a time-varying decision
process over a discrete permission space (P0–P5), with regime-switching for
dynamic authority redistribution. Validated via Artemis and Starship mission
simulations.

**Citation:**
```bibtex
@misc{eescf2026,
  author       = {Anonymous},
  title        = {{EESCF v2.0: A Stochastic Control Framework for
                   Delay-Constrained Human–AI Shared Autonomy in Extreme
                   Space Environments}},
  year         = {2026},
  doi          = {10.5281/zenodo.20687118},
  publisher    = {Zenodo},
  url          = {https://doi.org/10.5281/zenodo.20687118}
}
```

**Source:** `eescf-paper.tex`

---

## Paper 3: UHACD v1.0

**Title:** UHACD v1.0: A Unified Stochastic Control Framework for Human–AI
Authority Drift and Shared Autonomy Systems

**DOI:** [10.5281/zenodo.20687498](https://doi.org/10.5281/zenodo.20687498)

**Abstract:**
A unified stochastic control framework integrating continuous control
degradation (PRE-GHR) with discrete permission allocation (EESCF).
Introduces explicit bidirectional coupling functions $\phi$ and $g$,
establishes Gear–Permission isomorphism, and formalizes the system as a
Dec-POMDP. Reveals emergent dynamics including control lag after permission
changes. Simulation: 91.3% autonomous completion rate, 94.7% regime
transition detection accuracy.

**Citation:**
```bibtex
@misc{uhacd2026,
  author       = {Anonymous},
  title        = {{UHACD v1.0: A Unified Stochastic Control Framework for
                   Human–AI Authority Drift and Shared Autonomy Systems}},
  year         = {2026},
  doi          = {10.5281/zenodo.20687498},
  publisher    = {Zenodo},
  url          = {https://doi.org/10.5281/zenodo.20687498}
}
```

**Source:** `uhacd-paper.tex`

---

## Relationship

```
PRE-GHR v3.0          EESCF v2.0
(Continuous drift)    (Discrete permission)
       \                  /
        \                /
         UHACD v1.0
    (Unified framework)
         |
    φ(C_t) ↔ g(P_t)
    Dec-POMDP formulation
```

- **PRE-GHR** models *how* control degrades over time (Gear 1–5)
- **EESCF** models *what* authority level to grant under delay (P0–P5)
- **UHACD** unifies both via bidirectional coupling $\phi$ and $g$

All three papers share the same underlying EntropyRuntime system architecture
and Gear-based permission model implemented in this repository.
