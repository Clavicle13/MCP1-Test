import httpx
import json
import uuid
import time
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
    r_fips = client.get(f"{base_url}/networking/v4.3/config/floating-ips", headers=headers)
    fips = r_fips.json().get("data", [])
    print(f"Floating IPs found: {len(fips)}")
    for f in fips:
        ext_id = f.get("extId")
        print(f"Floating IP: {f.get('name')} ({ext_id}) | IP: {f.get('floatingIp')} | Subnet: {f.get('externalSubnetReference')}")
        print(f"Deleting Floating IP {ext_id}...")
        r_del = client.delete(f"{base_url}/networking/v4.3/config/floating-ips/{ext_id}", headers=headers)
        print("Delete status:", r_del.status_code)
        if r_del.status_code in (200, 202):
            task_id = r_del.json().get("data", {}).get("extId")
            if task_id:
                for _ in range(30):
                    r_task = client.get(f"{base_url}/prism/v4.3/config/tasks/{task_id}", headers=headers)
                    t_status = r_task.json().get("data", {}).get("status")
                    print("Task status:", t_status)
                    if t_status in ("SUCCEEDED", "FAILED"):
                        break
                    time.sleep(2)
