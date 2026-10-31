# AegisSkeleton // Project Milestones & Roadmap
> Kiroween 2026 Hackathon (Skeleton Crew Category)

## 📌 Milestone 1: Baseline Compliance (COMPLETED)
- [x] **Repository Structure**: Public GitHub repo with `.kiro/` directory, MIT license, and verified commit history.
- [x] **Dual Derived Applications**:
  - `apps/grim-reconciler`: High-throughput FinTech transaction ledger with 7-day DynamoDB TTL.
  - `apps/phantom-guard`: Cloud Sentry for detecting and neutralizing unbudgeted persistent resources.
- [x] **Agent Hook**: `Aegis Smite` pre-commit hook active, blocking client-side mock persistence (`localStorage`).
- [x] **Local Execution Verified**: `reconciler.py` successfully executed locally on Python 3.9/ARM64, verifying single-table DynamoDB payloads.
- [x] **Live Public Prototype**: S3 static website hosting active at `http://aegis-skeleton-ui-1793421864.s3-website-us-east-1.amazonaws.com`.

---

## 🎯 Milestone 2: Cloud E2E & FinOps Hardening (IN PROGRESS)
- [ ] **CloudFormation IAM Unblock**: Resolve EC2 role permissions to allow direct SAM deployment or configure GitHub Actions deployment runner.
- [ ] **Live REST Endpoint**: Expose API Gateway HTTP POST endpoint for `/reconcile` and `/smite`.
- [ ] **Real Telemetry Wiring**: Frontend `fetch()` requests hitting real Lambda and writing to DynamoDB with verifiable `200 OK` network calls.
- [ ] **Deep MCP Toolchain**: Implement stdio FinOps MCP server (`mcp/finops_sentry.py`) for AI agent zero-idle auditing.

---

## 🎨 Milestone 3: Elite JRPG UI & Battle Motion (SCHEDULED)
- [ ] **Sprite & Motion Overhaul**: Replace placeholder geometric blocks with authentic pixel-art sprite sheets (Pumpkin Paladin & Cloud Zombies).
- [ ] **Tactile Feedback**: 8-bit sound effects, screen shake physics, and dynamic damage numbers.
- [ ] **Bento Density Balance**: Embed real-time telemetry tables alongside the JRPG battle canvas.

---

## 🏆 Milestone 4: Final Submission & Video Demo (FINAL PHASE)
- [ ] **2:45 Showcase Video**: Record 4-scene video adhering to the storyboard (Problem -> Kiro Specs -> Live Cloud E2E -> Submission).
- [ ] **Devpost Writeup**: Complete structured documentation covering Architecture, FinOps impact, and Spec-driven workflows.
