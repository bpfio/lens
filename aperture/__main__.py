"""CLI entry point for Aperture.

Usage:
    python -m aperture
    python -m aperture --port 3000
    python -m aperture --socket /var/run/aperture.sock
"""

import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="Aperture — AI Protocol Translator")
    parser.add_argument(
        "--host",
        default=os.environ.get("APERTURE_HOST", "0.0.0.0"),
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("APERTURE_PORT", "8080")),
        help="Bind port (default: 8080)",
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("APERTURE_UNIX_SOCKET", ""),
        help="Unix socket path (overrides host:port)",
    )
    args = parser.parse_args()

    def _load_env_file(filepath: str):
        if not os.path.isfile(filepath):
            return
        try:
            from dotenv import load_dotenv
            load_dotenv(filepath)
            return
        except ImportError:
            pass
        # Fallback simple parser if python-dotenv is not installed
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

    # Check root dir, cwd, and package dir
    root_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    cwd_env = os.path.join(os.getcwd(), ".env")
    pkg_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    for p in (root_env, cwd_env, pkg_env):
        if os.path.isfile(p):
            _load_env_file(p)
            break

    from aiohttp import web
    from .index import create_app

    app = create_app()

    if args.socket:
        print(f"Aperture starting on unix://{args.socket}", file=sys.stderr)
        web.run_app(app, path=args.socket)
    else:
        print(f"Aperture starting on http://{args.host}:{args.port}", file=sys.stderr)
        web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
