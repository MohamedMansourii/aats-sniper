# ADR-0011 — Kubernetes Explicitly Out of Scope Until Multi-Node Trigger

**Date:** 2026-06-16
**Status:** ACCEPTED
**Author:** `latency-devops-engineer`
**Scope:** T-250 (scaffold), T-500 (deploy)

---

## Context

The deploy topology is a single Linux host (infrastructure.md §1, `dedicated_geyser` tier).
The system runs as 11 containers under `docker compose up`. The team's charter (CLAUDE.md) includes
a `devops-engineer` generalist pattern and a `latency-devops-engineer` specialist.

A common Kubernetes recommendation exists for containerized systems. This ADR records the explicit
trade-off decision for the AATS deployment.

## Decision

**Kubernetes is EXPLICITLY OUT OF SCOPE until a documented multi-node trigger fires.**

The triggers that would justify a K8s migration:

1. The system is deployed across 2+ physical hosts for HA or cross-region execution.
2. Total service count grows beyond 20 and manual compose management becomes unworkable.
3. A load-balancing requirement (multiple hot-core instances) requires a container scheduler.

None of these apply at T-250 scope.

## Consequences (the trade-off, stated honestly)

| Factor | docker-compose (chosen) | Kubernetes |
|---|---|---|
| RTT overhead | Zero — host-network or bridge | kube-proxy iptables + CNI overlay adds 0.1–0.5ms per hop |
| Control-plane tax | None | etcd + apiserver + scheduler on the same host = memory + CPU tax |
| aats-signer isolation | Unix-socket volume mount, no network exposure | Requires NetworkPolicy + Pod security; still possible but more config surface |
| Redis latency | <0.1ms bridge | <0.1ms with hostNetwork or optimized CNI; bridge adds jitter |
| Operational complexity | `docker compose up/down` | kubectl, helm, RBAC, namespaces, PodDisruptionBudgets |
| Cold-pull build | <10 min on 4-vCPU (NFR-010) | Same, but image pull policy + scheduler adds startup latency |
| Restart policy | `restart: unless-stopped` | Pod restart policy + liveness probes (equivalent) |

**The dominant reason:** the snipe loop's latency budget has no slack for an extra CNI overlay hop
on the Redis read path (SNIPE loop reads pre-staged KV on every candidate event). A kube-proxy
iptables rule adds jitter even on a single-node cluster because packets traverse the kernel netfilter
stack twice. We pay Rust + process isolation + Redis proximity to squeeze microseconds; adding K8s
networking on a single-node deployment is a net latency tax with zero multi-node benefit.

## When to revisit

If the multi-node trigger fires (colo-primary + cloud-secondary for DMS failover, or horizontal
scale of the control-plane API), revisit K8s or Nomad. The service contracts (Redis Streams,
docker-compose service names) are designed to translate cleanly — this is not a lock-in decision.

The architect (`solana-systems-architect`) issues the delta notice when the trigger fires.
