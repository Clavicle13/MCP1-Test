import httpx
import json
from dotenv import load_dotenv
load_dotenv(override=True)
from config import Config

auth = (Config.PC_USERNAME, Config.PC_PASSWORD)
base_url = f"https://{Config.PC_HOST}:{Config.PC_PORT}/api"

with httpx.Client(verify=False, timeout=15.0, auth=auth) as client:
    r_vpcs = client.get(f"{base_url}/networking/v4.3/config/vpcs")
    print("ALL VPCS:")
    print(json.dumps(r_vpcs.json(), indent=2))

    r_subnets = client.get(f"{base_url}/networking/v4.3/config/subnets")
    print("\nALL SUBNETS:")
    print(json.dumps(r_subnets.json(), indent=2))
