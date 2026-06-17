"""`python -m aats.dms` — the aats-dms service process entrypoint (T-322).

Kept as a one-line shim so the compose unit's `CMD ["python", "-m", "aats.dms"]`
(docker/Dockerfile.dms) starts the watchdog loop in `aats.risk.dms_main`.
"""

from aats.risk.dms_main import main

if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
