import httpx
import json
import uuid
from dotenv import load_dotenv
load_dotenv(override=True)
from config import Config

auth = (Config.PC_USERNAME, Config.PC_PASSWORD)
base_url = f"https://{Config.PC_HOST}:{Config.PC_PORT}/api"

headers = {
    "NTNX-Request-Id": str(uuid.uuid4()),
    "If-Match": "*",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

with httpx.Client(verify=False, timeout=15.0, auth=auth) as client:
    rts = client.get(f"{base_url}/networking/v4.3/config/route-tables", headers=headers).json().get("data", [])
    for rt in rts:
        rt_id = rt.get("extId")
        r = client.get(f"{base_url}/networking/v4.3/config/route-tables/{rt_id}/routes", headers=headers)
        print(f"RT {rt_id} routes:")
        print(json.dumps(r.json().get("data"), indent=2))
