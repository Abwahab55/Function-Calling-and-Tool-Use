"""Seminar demo: Function Calling & Tool Use (5G Network Diagnostics).

Setup:  pip install anthropic
        export ANTHROPIC_API_KEY=your_key      (Windows: set ANTHROPIC_API_KEY=your_key)
Run:    python demo_function_calling.py

Lifecycle demonstrated:
1. Tool Schema & Parameters  -> Defining structured JSON schemas for the LLM
2. Tool Selection            -> Model chooses the right tool(s) based on user intent
3. Validation                -> App checks tool name, required fields, and types
4. Tool Results              -> App executes the function and returns structured JSON
5. Error Handling            -> Graceful handling of unknown tools, bad args, or runtime errors
"""
import json
import anthropic

MODEL = "claude-sonnet-4-5"
client = anthropic.Anthropic()

# 1. TOOL SCHEMA & PARAMETERS
TOOLS = [
    {
        "name": "get_ue_status",
        "description": "Retrieve the registration and connection status of a User Equipment (UE) by its numeric ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ue_id": {
                    "type": "integer",
                    "description": "Numeric identifier of the UE (e.g., 1, 2)."
                }
            },
            "required": ["ue_id"]
        }
    },
    {
        "name": "get_logs",
        "description": "Fetch recent diagnostic logs from a 5G Core network function (e.g., 'AMF', 'SMF', 'UPF').",
        "input_schema": {
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "enum": ["AMF", "SMF", "UPF", "AUSF"],
                    "description": "5G core network component name."
                }
            },
            "required": ["component"]
        }
    }
]


# Mock implementations representing 5G Core / RAN telemetry APIs
def get_ue_status(ue_id: int) -> dict:
    ue_db = {
        1: {"ue_id": 1, "REGISTERED": True, "cell_id": "gNB-01"},
        2: {"ue_id": 2, "REGISTERED": False, "last_seen": "14:22:10Z"},
    }
    if ue_id not in ue_db:
        raise ValueError(f"UE {ue_id} not found in subscriber database.")
    return ue_db[ue_id]


def get_logs(component: str) -> dict:
    logs_db = {
        "AMF": [
            "14:22:11Z [WARN] UE-2 Registration Request received",
            "14:22:12Z [ERROR] UE-2 authentication failure (5G-AKA MAC mismatch)"
        ],
        "SMF": ["14:20:00Z [INFO] PDU session establishment nominal"],
        "UPF": ["14:21:05Z [INFO] N3 tunnel active"],
        "AUSF": ["14:22:12Z [ERROR] SUCI authentication vector verification failed for UE-2"]
    }
    if component not in logs_db:
        raise ValueError(f"Unknown component '{component}'.")
    return {"component": component, "logs": logs_db[component]}


IMPL = {
    "get_ue_status": get_ue_status,
    "get_logs": get_logs,
}


# 3. PARAMETER & SCHEMA VALIDATION
def validate_and_execute(tool_name: str, tool_input: dict) -> dict:
    """Validate tool existence, required parameters, and types before execution."""
    if tool_name not in IMPL:
        return {"error": f"Unknown tool: '{tool_name}'"}

    if not isinstance(tool_input, dict):
        return {"error": "Invalid parameters: expected a JSON object."}

    if tool_name == "get_ue_status":
        ue_id = tool_input.get("ue_id")
        if type(ue_id) is not int or ue_id <= 0:
            return {"error": "Validation error: 'ue_id' must be a positive integer."}

    elif tool_name == "get_logs":
        component = tool_input.get("component")
        if not isinstance(component, str) or component not in {"AMF", "SMF", "UPF", "AUSF"}:
            return {"error": "Validation error: 'component' must be one of AMF, SMF, UPF, AUSF."}

    # 5. ERROR HANDLING DURING EXECUTION
    try:
        return IMPL[tool_name](**tool_input)
    except Exception as exc:
        return {"error": f"Execution failed: {str(exc)}"}


def run(prompt: str):
    messages = [{"role": "user", "content": prompt}]
    print(f"USER: {prompt}\n")

    while True:
        # 2. TOOL SELECTION (Model decides whether to call a tool or answer)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=800,
            tools=TOOLS,
            messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            final_text = "".join(b.text for b in resp.content if b.type == "text")
            print(f"\nASSISTANT: {final_text}")
            return

        # 4. TOOL RESULTS
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            print(f"TOOL CALL  {block.name}({json.dumps(block.input)})")
            out = validate_and_execute(block.name, block.input)
            print(f"RESULT     {json.dumps(out)}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(out)
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    run("Why is UE 2 offline?")
