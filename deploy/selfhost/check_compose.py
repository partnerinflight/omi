"""Fail if the merged selfhost compose config loses its hardening.

    docker compose -f deploy/selfhost/ci-stub.yml -f deploy/selfhost/secondbrain.yml --profile apps \\
        config --format json | python3 deploy/selfhost/check_compose.py
"""
import json
import sys

config = json.load(sys.stdin)
s = config["services"]["secondbrain"]
labels = s.get("labels", {})
labels = labels if isinstance(labels, dict) else dict(l.split("=", 1) for l in labels)
router = labels.get("traefik.http.routers.secondbrain-rtr.middlewares", "")
checks = {
    "no published ports": not s.get("ports"),
    "non-root user": s.get("user") == "65532:65532",
    "read-only root": s.get("read_only") is True,
    "all capabilities dropped": s.get("cap_drop") == ["ALL"],
    "no-new-privileges": "no-new-privileges:true" in s.get("security_opt", []),
    "memory limit": bool(s.get("mem_limit")),
    "cpu limit": float(s.get("cpus") or 0) > 0,
    "pids limit": bool(s.get("pids_limit")),
    "forwarded IPs not wildcard": s["environment"].get("SB_FORWARDED_ALLOW_IPS") not in (None, "*"),
    "rate limit on router": "secondbrain-rl" in router.split(","),
    "body limit on router": "secondbrain-body" in router.split(","),
    "body limit configured": labels.get("traefik.http.middlewares.secondbrain-body.buffering.maxRequestBodyBytes") == "8000000",
}
failed = [name for name, ok in checks.items() if not ok]
print("\n".join(f"{'ok  ' if ok else 'FAIL'} {name}" for name, ok in checks.items()))
sys.exit(1 if failed else 0)
