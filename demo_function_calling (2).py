"""
Offline seminar demo: Function Calling + MCP-style tool interaction for 5G diagnostics.

No API key, internet connection, or third-party package is required (Python 3 only).
Run:
    python demo_function_calling_offline.py                  # main trace: "Why is UE 2 offline?"
    python demo_function_calling_offline.py --validation     # + rejected calls, then type your own
    python demo_function_calling_offline.py --interactive    # menu + your own questions

This is a deterministic simulation of the agent's tool-selection step:
- A small rule-based planner stands in for the LLM.
- A local tool registry stands in for tools exposed by an MCP server.
- The dispatcher validates tool names and arguments before execution.
- Tool results are returned to the planner, which decides the next step.

It demonstrates the architecture and control flow, not a live LLM or a real MCP
network connection. The UE, AMF, SMF, UPF and gNB data are mock data for the seminar.

Key distinction:
- An agent/LLM reasons about the user's request, selects tools, and interprets results.
- MCP exposes tools and their schemas so an agent can discover and call them.
- The MCP client/host validates calls, executes them through the server, and returns results.
"""
import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional

MAX_STEPS = 5  # safety limit for the agent loop

# ---------------------------------------------------------------------------
# Colours (work in Windows Terminal / PowerShell / Linux). --no-color disables.
# ---------------------------------------------------------------------------
if os.name == "nt":
    os.system("")  # enables ANSI colours in the classic Windows console
COLOR = True


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def head(text: str) -> str:   return c(text, "1;36")  # bold cyan
def call(text: str) -> str:   return c(text, "33")    # yellow
def ok(text: str) -> str:     return c(text, "32")    # green
def err(text: str) -> str:    return c(text, "31")    # red
def bold(text: str) -> str:   return c(text, "1")


# ---------------------------------------------------------------------------
# TOOL SCHEMAS: the contract that a model/client can inspect before calling.
# ---------------------------------------------------------------------------
TOOL_SCHEMAS = {
    "list_ues": {
        "description": "List all UEs in the network with a short health summary "
                       "(registered?, signal, IP address). Use for questions about many devices.",
        "kind": "read",
        "parameters": {},
        "required": [],
    },
    "get_ue_status": {
        "description": "Get the 5G registration state, cell, signal strength and IP address for one UE.",
        "kind": "read",
        "parameters": {
            "ue_id": {"type": "integer", "minimum": 1, "maximum": 999}
        },
        "required": ["ue_id"],
    },
    "get_logs": {
        "description": "Get recent diagnostic logs for a 5G component: AMF = registration/authentication, "
                       "GNB = radio/base station, SMF = data sessions/IP, UPF = user data traffic. "
                       "Optional ue_id filter.",
        "kind": "read",
        "parameters": {
            "component": {"type": "string", "enum": ["AMF", "SMF", "UPF", "GNB"]},
            "ue_id": {"type": "integer", "minimum": 1, "maximum": 999},
        },
        "required": ["component"],
    },
    "register_ue": {
        "description": "Re-authenticate a UE with the AMF and register it (ACTION: changes network state).",
        "kind": "action",
        "parameters": {
            "ue_id": {"type": "integer", "minimum": 1, "maximum": 999}
        },
        "required": ["ue_id"],
    },
}

# ---------------------------------------------------------------------------
# MOCK 5G NETWORK: 8 UEs. UE 1 is healthy; UEs 2-8 each have a DIFFERENT problem,
# written in the log of the network part where it happens.
#   AMF = registration + authentication     gNB = base station / radio
#   SMF = data session + IP address         UPF = forwards the user data
# ---------------------------------------------------------------------------
UE_STATE = {
    1: {"ue_id": 1, "type": "Smartphone",  "REGISTERED": True,  "cell": "gNB-1 / Cell 1", "signal_dbm": -78,  "ip": "10.45.0.2"},
    2: {"ue_id": 2, "type": "Smartphone",  "REGISTERED": False, "cell": "gNB-1 / Cell 1", "signal_dbm": -81,  "ip": None},
    3: {"ue_id": 3, "type": "IoT sensor",  "REGISTERED": True,  "cell": "gNB-2 / Cell 4", "signal_dbm": -112, "ip": "10.45.0.4"},
    4: {"ue_id": 4, "type": "Tablet",      "REGISTERED": False, "cell": None,             "signal_dbm": None, "ip": None},
    5: {"ue_id": 5, "type": "Laptop modem","REGISTERED": True,  "cell": "gNB-1 / Cell 2", "signal_dbm": -84,  "ip": None},
    6: {"ue_id": 6, "type": "Smartwatch",  "REGISTERED": False, "cell": "gNB-2 / Cell 3", "signal_dbm": -75,  "ip": None},
    7: {"ue_id": 7, "type": "Smartphone",  "REGISTERED": False, "cell": "gNB-1 / Cell 1", "signal_dbm": -83,  "ip": None},
    8: {"ue_id": 8, "type": "Smartphone",  "REGISTERED": True,  "cell": "gNB-1 / Cell 2", "signal_dbm": -79,  "ip": "10.45.0.8"},
}

CORE_LOGS = {
    "AMF": [
        {"ue_id": 1, "level": "INFO",  "msg": "Registration accepted"},
        {"ue_id": 2, "level": "ERROR", "msg": "Authentication failure (SIM key mismatch)"},
        {"ue_id": 3, "level": "INFO",  "msg": "Registration accepted"},
        {"ue_id": 5, "level": "INFO",  "msg": "Registration accepted"},
        {"ue_id": 6, "level": "ERROR", "msg": "Registration rejected: subscription expired"},
        {"ue_id": 7, "level": "ERROR", "msg": "Registration rejected: roaming not allowed (foreign SIM, PLMN 262-01)"},
        {"ue_id": 8, "level": "INFO",  "msg": "Registration accepted"},
    ],
    "GNB": [
        {"ue_id": 3, "level": "WARN",  "msg": "Weak signal (-112 dBm) on Cell 4"},
        {"ue_id": 4, "level": "ERROR", "msg": "Radio link failure, signal lost"},
    ],
    "SMF": [
        {"ue_id": 1, "level": "INFO",  "msg": "PDU session established, IP 10.45.0.2"},
        {"ue_id": 3, "level": "INFO",  "msg": "PDU session established, IP 10.45.0.4"},
        {"ue_id": 5, "level": "ERROR", "msg": "PDU session rejected: IP address pool exhausted"},
        {"ue_id": 8, "level": "INFO",  "msg": "PDU session established, IP 10.45.0.8"},
    ],
    "UPF": [
        {"ue_id": 8, "level": "WARN",  "msg": "High packet loss (12%) on user data path"},
    ],
}

# Why register_ue cannot simply fix some UEs (used for realistic error handling)
CANNOT_REGISTER = {
    4: "UE has no radio connection (no cell); cannot register.",
    6: "Subscription expired; renew it in the subscriber database first.",
    7: "Roaming not allowed for this foreign SIM; registration refused.",
}

FIXES = [  # keyword in log message -> suggested fix
    ("Authentication", "Check the SIM key / subscriber data, then re-register the UE."),
    ("subscription expired", "Renew the subscription in the subscriber database (UDM)."),
    ("roaming not allowed", "Check the roaming agreement / allowed networks for this SIM."),
    ("Radio link failure", "Check radio coverage; the device lost the base station."),
    ("Weak signal", "Improve coverage on the cell or move the device closer to the base station."),
    ("IP address pool", "Enlarge the IP address pool in the SMF configuration."),
    ("packet loss", "Check the load on the UPF / transport link."),
]


def fix_for(msg: str) -> str:
    return next((f for k, f in FIXES if k.lower() in msg.lower()), "Investigate further.")


# ---------------------------------------------------------------------------
# TOOL IMPLEMENTATIONS (in a real system: calls to the 5G core via an MCP server)
# ---------------------------------------------------------------------------
def list_ues() -> Dict[str, Any]:
    return {"ues": [{"ue_id": u["ue_id"], "type": u["type"], "REGISTERED": u["REGISTERED"],
                     "signal_dbm": u["signal_dbm"], "ip": u["ip"]} for u in UE_STATE.values()]}


def get_ue_status(ue_id: int) -> Dict[str, Any]:
    if ue_id not in UE_STATE:
        return {"ue_id": ue_id, "error": f"UE {ue_id} not found. Known UEs: {sorted(UE_STATE)}"}
    return dict(UE_STATE[ue_id])


def get_logs(component: str, ue_id: Optional[int] = None) -> Dict[str, Any]:
    if ue_id is not None and ue_id not in UE_STATE:
        return {"ue_id": ue_id, "error": f"UE {ue_id} not found. Known UEs: {sorted(UE_STATE)}"}
    lines = CORE_LOGS[component]
    if ue_id is not None:
        lines = [l for l in lines if l["ue_id"] == ue_id]
    return {"component": component, "lines": lines}


def register_ue(ue_id: int) -> Dict[str, Any]:
    if ue_id not in UE_STATE:
        return {"ue_id": ue_id, "error": f"UE {ue_id} not found. Known UEs: {sorted(UE_STATE)}"}
    if ue_id in CANNOT_REGISTER:
        return {"ue_id": ue_id, "error": CANNOT_REGISTER[ue_id]}
    if UE_STATE[ue_id]["REGISTERED"]:
        return {"ue_id": ue_id, "REGISTERED": True, "note": "UE was already registered."}
    # The demo assumes the operator has now corrected UE 2's SIM key.
    UE_STATE[ue_id]["REGISTERED"] = True
    UE_STATE[ue_id]["ip"] = f"10.45.0.{10 + ue_id}"  # new, unused IP address
    CORE_LOGS["AMF"] = [l for l in CORE_LOGS["AMF"] if l["ue_id"] != ue_id]
    CORE_LOGS["AMF"].append({"ue_id": ue_id, "level": "INFO", "msg": "Re-authentication successful, registration accepted"})
    return {"ue_id": ue_id, "authenticated_with_amf": True, "REGISTERED": True}


IMPLEMENTATIONS = {
    "list_ues": list_ues,
    "get_ue_status": get_ue_status,
    "get_logs": get_logs,
    "register_ue": register_ue,
}


# ---------------------------------------------------------------------------
# VALIDATION: generic checks driven by the schema (allowlist, required, type, range, enum)
# ---------------------------------------------------------------------------
def validate_call(tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
    """Return an error message, or None if the proposed call is valid."""
    if tool_name not in TOOL_SCHEMAS:
        return f"Unknown tool: {tool_name}"
    if not isinstance(arguments, dict):
        return "Arguments must be a JSON object."

    schema = TOOL_SCHEMAS[tool_name]
    missing = [key for key in schema["required"] if key not in arguments]
    if missing:
        return "Missing required parameter(s): " + ", ".join(missing)
    unknown = [key for key in arguments if key not in schema["parameters"]]
    if unknown:
        return "Unknown parameter(s): " + ", ".join(unknown)

    for key, value in arguments.items():
        rule = schema["parameters"][key]
        if rule["type"] == "integer":
            # bool is a subclass of int in Python, so reject it explicitly.
            if isinstance(value, bool) or not isinstance(value, int):
                return f"{key} must be an integer."
            if not rule["minimum"] <= value <= rule["maximum"]:
                return f"{key} must be between {rule['minimum']} and {rule['maximum']}."
        if rule["type"] == "string":
            if not isinstance(value, str):
                return f"{key} must be a string."
            if "enum" in rule and value not in rule["enum"]:
                return f"{key} must be one of: {', '.join(rule['enum'])}."
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


# ---------------------------------------------------------------------------
# PLANNER: tiny rule-based stand-in for the LLM (tool selection)
# ---------------------------------------------------------------------------
OVERVIEW = re.compile(r"\b(which|any|all|list|overview|summary)\b.*\b(devices?|ues?|phones?|problems?)\b", re.IGNORECASE)
SLOW = re.compile(r"\b(slow|speed|lag|packet)", re.IGNORECASE)


def choose_next_call(user_prompt: str, previous_results: List[Dict[str, Any]]):
    """Select the next tool based on the request and the previous tool results."""
    # ---- first step: what does the user want? ----
    if not previous_results:
        if OVERVIEW.search(user_prompt):
            return ("list_ues", {})                       # question about many devices
        match = re.search(r"\bUE\s*(-?\d+)\b", user_prompt, re.IGNORECASE)
        if not match:
            return None                                   # e.g. "Hello" -> no tool needed
        ue_id = int(match.group(1))
        # Only a command like "Register UE 2" triggers the ACTION tool (not "Is UE 1 registered?")
        if re.match(r"\s*(please\s+)?(re-?)?(register|authenticate|attach)\b", user_prompt, re.IGNORECASE):
            return ("register_ue", {"ue_id": ue_id})
        return ("get_ue_status", {"ue_id": ue_id})

    first = previous_results[0]
    if "error" in first["result"]:
        return None                                       # nothing more to investigate

    # ---- overview: check the logs of every component that shows a symptom ----
    if first["tool"] == "list_ues":
        ues = first["result"]["ues"]
        needed = []
        if any(not u["REGISTERED"] and u["signal_dbm"] is not None for u in ues): needed.append("AMF")
        if any(u["signal_dbm"] is None or u["signal_dbm"] < -100 for u in ues):  needed.append("GNB")
        if any(u["REGISTERED"] and u["ip"] is None for u in ues):                needed.append("SMF")
        needed.append("UPF")                              # traffic problems are invisible in the list
        done = [r["args"].get("component") for r in previous_results[1:]]
        todo = [comp for comp in needed if comp not in done]
        return ("get_logs", {"component": todo[0]}) if todo else None

    # ---- single UE: read the clues from get_ue_status and pick the right logs ----
    if first["tool"] != "get_ue_status" or len(previous_results) > 1:
        return None
    r = first["result"]
    ue_id = r["ue_id"]
    if r["cell"] is None:
        return ("get_logs", {"component": "GNB", "ue_id": ue_id})   # no radio -> base station
    if not r["REGISTERED"]:
        return ("get_logs", {"component": "AMF", "ue_id": ue_id})   # radio OK -> registration
    if r["signal_dbm"] < -100:
        return ("get_logs", {"component": "GNB", "ue_id": ue_id})   # weak signal -> radio
    if r["ip"] is None:
        return ("get_logs", {"component": "SMF", "ue_id": ue_id})   # registered but no IP -> session
    if SLOW.search(user_prompt):
        return ("get_logs", {"component": "UPF", "ue_id": ue_id})   # user says slow -> data path
    return None                                                     # healthy UE


def final_response(prompt: str, results: List[Dict[str, Any]]) -> str:
    """Turn the tool results into an answer (what an LLM would write)."""
    if not results:
        return ("I can diagnose UEs (devices) in the 5G network. Try: 'Why is UE 2 offline?', "
                "'Which devices have a problem?' or 'Register UE 2'. (No tool was needed for this question.)")
    first = results[0]
    r = first["result"]
    if "error" in r:
        return f"I could not complete the request: {r['error']}"
    if first["tool"] == "register_ue":
        if r.get("note"):
            return f"UE {r['ue_id']}: {r['note']}"
        return f"AMF authentication successful. UE {r['ue_id']} is now registered."

    problems = [(l, res["args"]["component"]) for res in results[1:]
                for l in res["result"].get("lines", []) if l["level"] in ("ERROR", "WARN")]

    if first["tool"] == "list_ues":
        by_ue: Dict[int, List[str]] = {}
        for l, comp in problems:
            by_ue.setdefault(l["ue_id"], []).append(f"{comp}: {l['msg']}")
        healthy = [u["ue_id"] for u in r["ues"] if u["ue_id"] not in by_ue]
        lines = [f"{len(by_ue)} of {len(r['ues'])} devices have a problem:"]
        for ue in sorted(by_ue):
            lines.append(f"    UE {ue}: " + "; ".join(by_ue[ue]))
        lines.append(f"    Healthy: UE {', UE '.join(map(str, healthy))}" if healthy else "    No healthy devices.")
        return "\n".join(lines)

    ue = r["ue_id"]
    if not problems:
        return f"UE {ue} is registered on {r['cell']} with good signal ({r['signal_dbm']} dBm) and IP {r['ip']}. It is working normally."
    l, comp = problems[0]
    state = "is registered but has a problem" if r["REGISTERED"] else "is offline (not registered)"
    return f"UE {ue} {state}. Cause from {comp} logs: {l['msg']}. Suggested fix: {fix_for(l['msg'])}"


# ---------------------------------------------------------------------------
# AGENT LOOP + OUTPUT
# ---------------------------------------------------------------------------
def show_architecture_overview():
    print(head("MCP AND AGENTS"))
    print("  Agent/LLM: reasons about the request and selects the next tool.")
    print("  MCP: exposes discoverable tools, schemas, parameters, and results.")
    print("  MCP client/host: validates arguments, dispatches the call, and handles errors.")
    print("  Interaction: agent -> tool selection -> validation -> tool execution")
    print("              -> tool result -> agent interpretation -> next action.")
    print(head("\nDISCOVERED TOOLS (like MCP tools/list)"))
    for name, s in TOOL_SCHEMAS.items():
        params = ", ".join(f"{p}{'*' if p in s['required'] else ''}: {r['type']}" for p, r in s["parameters"].items()) or "-"
        print(f"  {bold(f'{name:<15}')} ({params})  [{s['kind']}]")
    print("  * = required parameter")


def run_diagnostic(prompt: str):
    print("\n" + "=" * 72)
    print(bold("USER: " + prompt))
    print("=" * 72)
    results: List[Dict[str, Any]] = []

    # Agent loop: choose -> validate/execute -> observe result -> choose again.
    step = 0
    while True:
        proposed = choose_next_call(prompt, results)
        if proposed is None:
            break
        if step == MAX_STEPS:
            print(err(f"\nStopped: safety limit of {MAX_STEPS} tool calls reached."))
            break
        step += 1

        tool_name, arguments = proposed
        print(head(f"\nSTEP {step}  AGENT / LLM - TOOL SELECTION"))
        print("  Proposed call:", call(f"{tool_name}({json.dumps(arguments)})"))
        if tool_name in TOOL_SCHEMAS:
            schema = {k: v for k, v in TOOL_SCHEMAS[tool_name].items() if k in ("parameters", "required")}
            print("  Tool schema:  ", json.dumps(schema))
        if TOOL_SCHEMAS.get(tool_name, {}).get("kind") == "action":
            print("  " + err("ACTION tool: changes network state (in production: needs operator approval)"))

        print(head("  HOST / MCP CLIENT - VALIDATION"))
        error = validate_call(tool_name, arguments)
        if error:
            print("  " + err("REJECTED: " + error))
            result = {"error": error}
        else:
            print("  " + ok("Validated: yes"))
            print(head("  MCP SERVER / TOOL - EXECUTION"))
            result = execute_tool(tool_name, arguments)

        colour = err if "error" in result else ok
        print("  TOOL RESULT:", colour(json.dumps(result)))
        results.append({"tool": tool_name, "args": arguments, "result": result})

    print(head("\nAGENT - FINAL RESPONSE"))
    print("  " + bold(final_response(prompt, results)))
    print("\nNOTE: mock data; a rule-based planner stands in for the LLM; no 5G core is contacted.")
    print("=" * 72)


EXAMPLES = [
    "Why is UE 2 offline?",
    "Is UE 1 registered?",
    "Why does UE 3 have a bad connection?",
    "Why is UE 4 offline?",
    "UE 5 is registered but has no internet. Why?",
    "Why is UE 6 offline?",
    "Why is UE 7 offline?",
    "Why is the internet slow on UE 8?",
    "Which devices have a problem?",
    "Why is UE 99 offline?",
    "Hello, what can you do?",
    "Register UE 2",
    "Register UE 6",
]


def interactive():
    print(head("\nOffline 5G tool-calling demo.") + " Type a number, your own question, or 'q' to quit.")
    while True:
        print(head("\nExample questions:"))
        for i, q in enumerate(EXAMPLES, 1):
            print(f"  {i:>2}. {q}")
        prompt = input("\nAsk a diagnostic question: ").strip()
        if prompt.lower() in {"quit", "exit", "q"}:
            print("Demo ended.")
            break
        if prompt.isdigit() and 1 <= int(prompt) <= len(EXAMPLES):
            prompt = EXAMPLES[int(prompt) - 1]
        if prompt:
            run_diagnostic(prompt)


def show_validation_examples():
    print(head("\nVALIDATION CHECKS (demonstrating rejected calls)"))
    tests = [
        ("get_ue_status", {"ue_id": 1}),
        ("get_ue_status", {"ue_id": "2"}),
        ("get_ue_status", {"ue_id": 0}),
        ("get_ue_status", {}),
        ("get_logs", {"component": "AUSF"}),
        ("get_ue_status", {"ue_id": 99}),
        ("delete_subscriber", {"ue_id": 2}),
    ]
    for name, args in tests:
        res = execute_tool(name, args)
        colour = err if "error" in res else ok
        print(f"  {call(name + '(' + json.dumps(args) + ')'):<55} -> {colour(json.dumps(res))}")


def parse_tool_call(text: str):
    """Turn  get_ue_status ue_id=2   (or  get_ue_status {"ue_id": 2})  into (tool, arguments).
    Values: 2 -> number, "2" -> text, AMF -> text, true/false -> boolean."""
    text = text.strip()
    # Shortcut: only a UE id  ->  get_ue_status   (2 = number, "2" = text, abc = text)
    if re.fullmatch(r'-?\d+|"[^"]*"|[A-Za-z]+', text) and text not in TOOL_SCHEMAS:
        text = f"get_ue_status ue_id={text}"
        print("  (shortcut: " + text + ")")
    # Same format as shown on screen:  get_logs({"component": "AUSF"})
    m = re.fullmatch(r"(\w+)\s*\((.*)\)", text, re.DOTALL)
    if m:
        inner = m.group(2).strip()
        return m.group(1), (json.loads(inner) if inner else {})
    parts = text.split(None, 1)
    tool = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    if rest.startswith("{"):
        return tool, json.loads(rest)                      # JSON style
    args: Dict[str, Any] = {}
    for item in re.findall(r'(\w+)\s*=\s*("[^"]*"|\S+)', rest):
        key, raw = item
        if raw.startswith('"') and raw.endswith('"'):
            value: Any = raw[1:-1]                         # "2" -> text
        elif re.fullmatch(r"-?\d+", raw):
            value = int(raw)                               # 2 -> number
        elif raw.lower() in ("true", "false"):
            value = raw.lower() == "true"
        else:
            value = raw                                    # AMF -> text
        args[key] = value
    if rest and not args:
        raise ValueError("could not read the parameters. Use name=value, e.g. ue_id=2")
    return tool, args


def manual_tool_calls():
    """Let the user type tool calls directly, to test validation live."""
    print(head("\nTRY IT YOURSELF: type a tool call (or 'q' to quit)"))
    print("  Format:  tool_name parameter=value     (or just a UE id: 2, \"2\", 0, 99)")
    print("  Examples:")
    print("    get_ue_status ue_id=2            (correct)")
    print("    get_ue_status ue_id=\"2\"          (text instead of a number)")
    print("    get_ue_status ue_id=0            (out of range)")
    print("    get_ue_status                    (missing parameter)")
    print("    get_logs component=AMF ue_id=2   (correct)")
    print("    get_logs component=AUSF          (not allowed)")
    print("    delete_subscriber ue_id=2        (unknown tool)")
    print("    get_logs({\"component\": \"AUSF\"})  (same format as on screen also works)")
    while True:
        text = input("\nTool call: ").strip()
        if text.lower() in {"q", "quit", "exit"}:
            print("Done.")
            break
        if not text:
            continue
        try:
            tool_name, arguments = parse_tool_call(text)
        except json.JSONDecodeError:
            print("  " + err('Could not read this input: inside { } every text needs quotes, e.g. {"component": "AUSF"}'))
            continue
        except Exception as exc:
            print("  " + err(f"Could not read this input: {exc}"))
            continue
        print("  Proposed call:", call(f"{tool_name}({json.dumps(arguments)})"))
        print(head("  HOST / MCP CLIENT - VALIDATION"))
        error = validate_call(tool_name, arguments)
        if error:
            print("  " + err("REJECTED: " + error))
            print("  " + err("The tool was NOT executed."))
            continue
        print("  " + ok("Validated: yes"))
        print(head("  MCP SERVER / TOOL - EXECUTION"))
        result = execute_tool(tool_name, arguments)
        colour = err if "error" in result else ok
        print("  TOOL RESULT:", colour(json.dumps(result)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="API-key-free function-calling demo")
    parser.add_argument("--interactive", action="store_true", help="menu + your own diagnostic questions")
    parser.add_argument("--validation", action="store_true", help="show rejected calls, then type your own tool calls")
    parser.add_argument("--no-color", action="store_true", help="plain output without colours")
    args = parser.parse_args()
    COLOR = not args.no_color

    show_architecture_overview()
    if args.interactive:
        interactive()
    else:
        run_diagnostic("Why is UE 2 offline?")
        if args.validation:
            show_validation_examples()
            manual_tool_calls()
        print("\nTry interactive mode with: python demo_function_calling_offline.py --interactive")
