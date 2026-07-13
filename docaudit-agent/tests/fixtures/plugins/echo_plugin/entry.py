"""A minimal valid plugin entry point for integration testing."""

import json
import sys


def main():
    """Simple JSON-RPC server that responds to health and echo tool calls."""
    # Send register notification first
    register_msg = {
        "method": "plugin.register",
        "params": {
            "capabilities": [
                {"type": "tool", "name": "echo", "display_name": "Echo"},
            ]
        },
    }
    sys.stdout.write(json.dumps(register_msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()

    # Process requests
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "id" not in msg and "method" not in msg:
            continue

        method = msg.get("method", "")

        # Handle notifications (no id field)
        if "id" not in msg:
            if method == "plugin.shutdown":
                break
            continue

        req_id = msg["id"]

        if method == "plugin.health":
            sys.stdout.write(json.dumps({
                "id": req_id, "result": {"status": "ok", "dependencies": {}}
            }, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        elif method == "plugin.shutdown":
            sys.stdout.write(json.dumps({"id": req_id, "result": "ok"}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            break
        elif method == "tool.execute":
            params = msg.get("params", {})
            sys.stdout.write(json.dumps({
                "id": req_id,
                "result": {
                    "success": True,
                    "data": {"echo": params.get("args", {})},
                },
            }, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        else:
            sys.stdout.write(json.dumps({
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
