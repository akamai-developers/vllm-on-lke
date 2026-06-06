"""Custom Strands tools for the chatbot.

The `@tool` decorator turns a plain function into something the agent can call.
Strands reads the type hints and docstring to build the tool's JSON schema — the
first docstring paragraph becomes the description, and the `Args:` section
documents each parameter. Keep both accurate; the model relies on them to decide
when and how to call the tool.

This module shows hand-written tools alongside the community tools
(`http_request`, `calculator`, `current_time`) that `app.py` also wires in.
"""
import json

from strands import tool


@tool
def web_search(query: str, max_results: int = 4) -> str:
    """Search the web for current, real-time information.

    Use this for anything that needs live or recent data the model can't know
    from training: news, sports scores, prices, weather, "today", "latest", etc.

    Args:
        query: What to search for.
        max_results: How many results to return (keep small — each result adds
            to the model's context; 3-4 is plenty).
    """
    from ddgs import DDGS

    try:
        hits = DDGS().text(query, max_results=max_results)
    except Exception as e:  # network/rate-limit — fail soft so the agent can recover
        return f"Search failed: {e}"
    if not hits:
        return "No results found."
    # Trim each snippet so a single search can't blow the context budget.
    out = []
    for h in hits:
        body = (h.get("body") or "")[:500]
        out.append(f"{h.get('title','')}\n{h.get('href','')}\n{body}")
    return "\n\n".join(out)


@tool
def list_lke_clusters() -> str:
    """List the user's Linode Kubernetes Engine (LKE) clusters on Akamai Cloud.

    Returns each cluster's id, label, region, Kubernetes version, and status.
    Use when asked about the user's clusters, infrastructure, or what's running
    on their account. Read-only: this only ever performs a GET.
    """
    import os

    import requests

    token = os.environ.get("LINODE_TOKEN")
    if not token:
        return "LINODE_TOKEN is not set, so the Linode API can't be queried."
    try:
        r = requests.get(
            "https://api.linode.com/v4/lke/clusters",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        return f"Linode API request failed: {e}"
    rows = [
        {k: c.get(k) for k in ("id", "label", "region", "k8s_version", "status")}
        for c in r.json().get("data", [])
    ]
    return json.dumps(rows, indent=2) if rows else "No LKE clusters found on this account."


@tool
def convert_temperature(value: float, from_unit: str, to_unit: str) -> str:
    """Convert a temperature between Celsius, Fahrenheit, and Kelvin.

    Use this for any temperature conversion instead of doing the arithmetic
    yourself — language models get the constants wrong.

    Args:
        value: The numeric temperature to convert.
        from_unit: Source unit: one of "C", "F", or "K" (case-insensitive).
        to_unit: Target unit: one of "C", "F", or "K" (case-insensitive).
    """
    f, t = from_unit.strip().upper(), to_unit.strip().upper()
    valid = {"C", "F", "K"}
    if f not in valid or t not in valid:
        return f"Error: units must be one of C, F, K (got from={from_unit!r}, to={to_unit!r})."

    # Normalize to Celsius first.
    if f == "C":
        celsius = value
    elif f == "F":
        celsius = (value - 32) * 5 / 9
    else:  # K
        celsius = value - 273.15

    # Celsius -> target.
    if t == "C":
        result = celsius
    elif t == "F":
        result = celsius * 9 / 5 + 32
    else:  # K
        result = celsius + 273.15

    return f"{value}°{f} = {round(result, 2)}°{t}"


# Tools exported to the agent. Add your own @tool functions here and they show
# up in the chatbot automatically.
CUSTOM_TOOLS = [web_search, list_lke_clusters, convert_temperature]
