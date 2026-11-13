
Offline seminar demo: Function Calling + MCP-style tool interaction for 5G diagnostics.

No API key, internet connection, or third-party package is required.
Run:
    python demo_function_calling_offline.py
    python demo_function_calling_offline.py --interactive

This is a deterministic simulation of the agent's tool-selection step:
- A small rule-based planner stands in for the LLM.
- A local tool registry stands in for tools exposed by an MCP server.
- The dispatcher validates tool names and arguments before execution.
- Tool results are returned to the planner, which decides the next step.

It demonstrates the architecture and control flow, not a live LLM or a real MCP
network connection. The UE and AMF data are mock data for the seminar.
"""
import argparse
import json
import re
from typing import Any, Dict


# Tool schemas: the contract that a model/client can inspect before calling.
TOOL_SCHEMAS = {
    "get_ue_status": {
        "description": "Get the 5G registration state for a UE ID.",
        "parameters": {
            "ue_id": {"type": "integer", "minimum": 1, "maximum": 999}
        },
        "required": ["ue_id"],
    },
    "get_logs": {
        "description": "Get recent diagnostic logs for a 5G core component.",
        "parameters": {
            "component": {"type": "string", "enum": ["AMF", "SMF", "UPF"]}
        },
        "required": ["component"],
    },
    "register_ue": {
        "description": "Authenticate a UE with the AMF and register it.",
        "parameters": {
            "ue_id": {"type": "integer", "minimum": 1, "maximum": 999}
        },
        "required": ["ue_id"],
    },
}

# Mock network state. UE 2 is offline because of an AMF authentication failure.
UE_STATE = {
    1: {"ue_id": 1, "REGISTERED": True},
    2: {"ue_id": 2, "REGISTERED": False},
    3: {"ue_id": 3, "REGISTERED": True},
}
AMF_AUTHENTICATED = {1: True, 2: False, 3: True}
CORE_LOGS = {
    "AMF": {"component": "AMF", "cause": "authentication failure"},
    "SMF": {"component": "SMF", "status": "no recent errors"},
    "UPF": {"component": "UPF", "status": "operational"},
}


def get_ue_status(ue_id: int) -> Dict[str, Any]:
    """Local mock implementation of the UE status tool."""
    return UE_STATE.get(ue_id, {"ue_id": ue_id, "error": "UE not found"})


def get_logs(component: str) -> Dict[str, Any]:
    """Local mock implementation of the core-log tool."""
    return CORE_LOGS[component]


def register_ue(ue_id: int) -> Dict[str, Any]:
    """Authenticate a UE with the AMF and update its registration state."""
    if ue_id not in UE_STATE:
        return {"ue_id": ue_id, "error": "UE not found"}

    # This offline demo models a successful AMF authentication attempt.
    AMF_AUTHENTICATED[ue_id] = True
    UE_STATE[ue_id]["REGISTERED"] = True
    return {
        "ue_id": ue_id,
        "authenticated_with_amf": True,
        "REGISTERED": True,
    }


IMPLEMENTATIONS = {
    "get_ue_status": get_ue_status,
    "get_logs": get_logs,
    "register_ue": register_ue,
}


def validate_call(tool_name: str, arguments: Dict[str, Any]):
    """Return an error message, or None if the proposed call is valid."""
    if tool_name not in TOOL_SCHEMAS:
        return f"Unknown tool: {tool_name}"
    if not isinstance(arguments, dict):
        return "Arguments must be a JSON object."

    schema = TOOL_SCHEMAS[tool_name]
    missing = [key for key in schema["required"] if key not in arguments]
    if missing:
        return "Missing required parameter(s): " + ", ".join(missing)

    if tool_name == "get_ue_status":
        value = arguments.get("ue_id")
        # bool is a subclass of int in Python, so reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, int):
            return "ue_id must be an integer."
        if not 1 <= value <= 999:
            return "ue_id must be between 1 and 999."

    if tool_name == "get_logs":
        value = arguments.get("component")
        if not isinstance(value, str):
            return "component must be a string."
        if value not in ("AMF", "SMF", "UPF"):
            return "component must be one of: AMF, SMF, UPF."

    if tool_name == "register_ue":
        value = arguments.get("ue_id")
        if isinstance(value, bool) or not isinstance(value, int):
            return "ue_id must be an integer."
        if not 1 <= value <= 999:
            return "ue_id must be between 1 and 999."

    return None


def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Allowlist + validation + safe execution + structured error result."""
    error = validate_call(tool_name, arguments)
    if error:
        return {"error": error}
    try:
        return IMPLEMENTATIONS[tool_name](**arguments)
    except Exception as exc:
        # In production, log details securely; return a safe message to the model.
        return {"error": f"Tool execution failed: {type(exc).__name__}: {exc}"}


def choose_next_call(user_prompt: str, previous_results=None):
    """
    Tiny rule-based stand-in for an LLM planner.
    It selects the next tool based on the request and the previous tool result.
    """
    previous_results = previous_results or []
    if not previous_results:
        match = re.search(r"\bUE\s*(\d+)\b", user_prompt, re.IGNORECASE)
        if not match:
            return None
        if re.search(r"\b(register|authenticate|attach)\w*\b", user_prompt, re.IGNORECASE):
            return ("register_ue", {"ue_id": int(match.group(1))})
        return ("get_ue_status", {"ue_id": int(match.group(1))})

    last = previous_results[-1]
    if last.get("tool") == "get_ue_status":
        result = last["result"]
        if result.get("REGISTERED") is False:
            return ("get_logs", {"component": "AMF"})
    return None


def run_diagnostic(prompt: str):
    print("=" * 68)
    print("USER:", prompt)
    print("=" * 68)
    results = []

    # Agent loop: choose -> validate/execute -> observe result -> choose again.
    for step in range(1, 5):
        proposed = choose_next_call(prompt, results)
        if proposed is None:
            break

        tool_name, arguments = proposed
        print(f"\nAGENT / LLM — TOOL SELECTION {step}")
        print("  Proposed call:", f"{tool_name}({json.dumps(arguments)})")
        print("  Tool schema:  ", json.dumps(TOOL_SCHEMAS[tool_name]))

        print("HOST / MCP CLIENT — VALIDATION")
        error = validate_call(tool_name, arguments)
        if error:
            print("  REJECTED:", error)
            result = {"error": error}
        else:
            print("  Validated: yes")
            print("MCP SERVER / LOCAL TOOL — EXECUTION")
            result = execute_tool(tool_name, arguments)

        print("TOOL RESULT:", json.dumps(result))
        results.append({"tool": tool_name, "result": result})

    print("\nAGENT — FINAL RESPONSE")
    if not results:
        print("  I could not identify a UE number. Try: Why is UE 2 offline?")
        return
    status = results[0]["result"]
    if results[0]["tool"] == "register_ue":
        if "error" in status:
            print("  UE registration failed:", status["error"])
        else:
            print(f"  AMF authentication successful for UE {status['ue_id']}.")
            print(f"  UE {status['ue_id']} is now registered.")
    elif "error" in status:
        print("  UE lookup failed:", status["error"])
    elif status.get("REGISTERED") is True:
        print(f"  UE {status['ue_id']} is registered.")
    else:
        print(f"  UE {status['ue_id']} is not registered.")
        if len(results) > 1:
            log_result = results[1]["result"]
            if log_result.get("error"):
                print("  I could not retrieve the AMF logs:", log_result["error"])
            else:
                print("  Likely cause from AMF logs:", log_result.get("cause", "not specified"))
                print("  Diagnosis: authentication failure in the AMF.")
    print("\nNOTE: All network responses above are mock data; no 5G core is contacted.")
    print("=" * 68)


def interactive():
    print("Offline 5G tool-calling demo. Type 'quit' to exit.")
    while True:
        prompt = input("\nAsk a diagnostic question: ").strip()
        if prompt.lower() in {"quit", "exit", "q"}:
            print("Demo ended.")
            break
        if prompt:
            run_diagnostic(prompt)


def show_validation_examples():
    print("\nVALIDATION CHECKS (demonstrating rejected calls)")
    tests = [
        ("get_ue_status", {"ue_id": "2"}),
        ("get_ue_status", {"ue_id": 0}),
        ("get_logs", {"component": "AUSF"}),
        ("delete_subscriber", {"ue_id": 2}),
    ]
    for name, args in tests:
        print(f"  {name}({json.dumps(args)}) -> {json.dumps(execute_tool(name, args))}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="API-key-free function-calling demo")
    parser.add_argument("--interactive", action="store_true",
                        help="enter your own diagnostic questions")
    parser.add_argument("--validation", action="store_true",
                        help="also show invalid calls being rejected")
    args = parser.parse_args()

    if args.interactive:
        interactive()
    else:
        run_diagnostic("Why is UE 2 offline?")
        if args.validation:
            show_validation_examples()
        print("\nTry interactive mode with: python demo_function_calling_offline.py --interactive")
