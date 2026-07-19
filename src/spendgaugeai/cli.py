"""cli.py — `spendgaugeai serve` entry point. argparse, not a CLI framework
dependency — one command doesn't need one (docs/DESIGN.md §9)."""
import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="spendgaugeai", description="AI FinOps for self-hosted developers")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="Run the SpendGaugeAI server")
    serve.add_argument("--host", default=None, help="Default: 0.0.0.0, or $HOST")
    serve.add_argument("--port", type=int, default=None, help="Default: 8000, or $PORT")
    serve.add_argument("--db-path", default=None, help="Default: ./data/spendgaugeai.db, or $SPENDGAUGEAI_DB_PATH")

    args = parser.parse_args()

    if args.command != "serve":
        parser.print_help()
        raise SystemExit(1)

    from dotenv import load_dotenv
    load_dotenv()

    if args.db_path:
        os.environ["SPENDGAUGEAI_DB_PATH"] = args.db_path

    host = args.host or os.environ.get("HOST", "0.0.0.0")
    port = args.port or int(os.environ.get("PORT", 8000))

    import uvicorn
    uvicorn.run("spendgaugeai.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
