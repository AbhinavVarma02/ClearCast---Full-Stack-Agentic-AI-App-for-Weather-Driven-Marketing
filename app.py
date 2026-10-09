"""ClearCast entrypoint: start the full local stack (orchestrator, gateway, UI).

Equivalent to ``python -m deploy.launcher``. The Docker image (Hugging Face
Space) runs the same launcher.
"""

from deploy.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
