import httpx
import json
from dotenv import load_dotenv
load_dotenv(override=True)
from config import Config

auth = (Config.PC_USERNAME, Config.PC_PASSWORD)
base_url = f"https://{Config.PC_HOST}:{Config.PC_PORT}/api"

with httpx.Client(verify=False, timeout=15.0, auth=auth) as client:
    r_vm = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/c768a894-25be-4204-97e4-36a5e88e28eb")
    print(json.dumps(r_vm.json(), indent=2))
