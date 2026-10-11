"""python -m sandbox [--port 8001] [--chaos realistic|off|hostile]"""
import argparse
import os

import uvicorn

parser = argparse.ArgumentParser(prog="sandbox")
parser.add_argument("--port", type=int, default=8001)
parser.add_argument("--chaos", default=os.environ.get("SANDBOX_CHAOS", "realistic"))
args = parser.parse_args()
os.environ["SANDBOX_CHAOS"] = args.chaos
uvicorn.run("sandbox.app:app", host="127.0.0.1", port=args.port, log_level="warning")
