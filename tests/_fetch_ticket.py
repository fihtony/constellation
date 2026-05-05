"""Quick utility to fetch and print the Jira ticket configured in tests/.env."""
import base64
import json
import os
from urllib.request import Request, urlopen

env = {}
env_path = os.path.join(os.path.dirname(__file__), ".env")
with open(env_path) as fh:
    for raw in fh:
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()

token = env.get("TEST_JIRA_TOKEN", "")
email = env.get("TEST_JIRA_EMAIL", "")
creds = base64.b64encode(f"{email}:{token}".encode()).decode()

ticket_url = env.get("TEST_JIRA_TICKET_URL", "").strip()
if not ticket_url:
    raise SystemExit("Set TEST_JIRA_TICKET_URL in tests/.env before running this script.")

key = ticket_url.rstrip("/").split("/")[-1]
base_url = "/".join(ticket_url.split("/")[:3])
url = f"{base_url}/rest/api/3/issue/{key}"
req = Request(url, headers={"Authorization": f"Basic {creds}", "Accept": "application/json"})
with urlopen(req, timeout=15) as r:
    d = json.load(r)

fields = d["fields"]
print("Summary:", fields.get("summary"))
print("Status:", fields["status"]["name"])
print("Assignee:", (fields.get("assignee") or {}).get("displayName", "unassigned"))
print("Components:", [c["name"] for c in (fields.get("components") or [])])
print("Labels:", fields.get("labels"))


def extract_text(node):
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return "".join(extract_text(c) for c in node.get("content", []))
    if isinstance(node, list):
        return "".join(extract_text(x) for x in node)
    return ""


desc = fields.get("description") or {}
text = extract_text(desc)
print("Description (%d chars):" % len(text))
print(text[:3000])

# Check custom fields for links
for k, v in fields.items():
    if "repo" in k.lower() or "github" in k.lower() or "url" in k.lower():
        if v:
            print(f"Field {k}: {v}")
