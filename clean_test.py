import httpx
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
    subs = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=headers).json().get("data", [])
    for s in subs:
        if s.get("name") == "Test-Spoke-Sub-01":
            ext_id = s.get("extId")
            print(f"Deleting Test-Spoke-Sub-01 ({ext_id})...")
            r = client.delete(f"{base_url}/networking/v4.3/config/subnets/{ext_id}", headers=headers)
            if r.status_code in (200, 202):
                t_id = r.json().get("data", {}).get("extId")
                if t_id:
                    for _ in range(30):
                        r_t = client.get(f"{base_url}/prism/v4.3/config/tasks/{t_id}", headers=headers)
                        ts = r_t.json().get("data", {}).get("status")
                        if ts in ("SUCCEEDED", "FAILED"):
                            print("Subnet Task:", ts)
                            break
                        time.sleep(2)

    vpcs = client.get(f"{base_url}/networking/v4.3/config/vpcs", headers=headers).json().get("data", [])
    for v in vpcs:
        if v.get("name") == "Test-Spoke-01":
            ext_id = v.get("extId")
            print(f"Deleting Test-Spoke-01 ({ext_id})...")
            r = client.delete(f"{base_url}/networking/v4.3/config/vpcs/{ext_id}", headers=headers)
            if r.status_code in (200, 202):
                t_id = r.json().get("data", {}).get("extId")
                if t_id:
                    for _ in range(30):
                        r_t = client.get(f"{base_url}/prism/v4.3/config/tasks/{t_id}", headers=headers)
                        ts = r_t.json().get("data", {}).get("status")
                        if ts in ("SUCCEEDED", "FAILED"):
                            print("VPC Task:", ts)
                            break
                        time.sleep(2)
