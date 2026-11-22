"""Configure both firmware projects for normal dual-antenna testing."""

import sys

from calibration import configure_mode


if __name__ == "__main__":
    try:
        configure_mode("normal")
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Mode switch stopped. {error}", file=sys.stderr)
        sys.exit(1)
