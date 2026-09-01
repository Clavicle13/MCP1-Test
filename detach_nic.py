import httpx
import uuid
import time
from dotenv import load_dotenv
load_dotenv(override=True)
from config import Config

auth = (Config.PC_USERNAME, Config.PC_PASSWORD)
base_url = f"https://{Config.PC_HOST}:{Config.PC_PORT}/api"

with httpx.Client(verify=False, timeout=15.0, auth=auth) as client:
    vm_id = "c768a894-25be-4204-97e4-36a5e88e28eb"
    nic_id = "85694406-678d-42a8-966f-fa236af89846"
    
    # 1. Get VM and ETag
    r_vm = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}")
    etag = r_vm.headers.get("ETag") or r_vm.json().get("data", {}).get("$reserved", {}).get("ETag")
    print(f"VM ETag: {etag}")

    headers = {
        "NTNX-Request-Id": str(uuid.uuid4()),
        "If-Match": etag,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    print(f"Deleting NIC {nic_id} from VM {vm_id}...")
    r = client.delete(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}/nics/{nic_id}", headers=headers)
    print("Status:", r.status_code)
    print("Response:", r.text)
    if r.status_code in (200, 202):
        task_id = r.json().get("data", {}).get("extId")
        if task_id:
            for _ in range(30):
                r_task = client.get(f"{base_url}/prism/v4.3/config/tasks/{task_id}", headers={"NTNX-Request-Id": str(uuid.uuid4())})
                t_status = r_task.json().get("data", {}).get("status")
                print("Task status:", t_status)
                if t_status in ("SUCCEEDED", "FAILED"):
                    break
                time.sleep(2)
