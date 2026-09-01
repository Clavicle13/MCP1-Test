import httpx
import json
from dotenv import load_dotenv
load_dotenv(override=True)
from config import Config

auth = (Config.PC_USERNAME, Config.PC_PASSWORD)
base_url = f"https://{Config.PC_HOST}:{Config.PC_PORT}/api"

with httpx.Client(verify=False, timeout=15.0, auth=auth) as client:
    r_vpcs = client.get(f"{base_url}/networking/v4.3/config/vpcs")
    vpcs = r_vpcs.json().get("data", [])
    print("=== VPCS ===")
    for v in vpcs:
        print(f"VPC Name: {v.get('name')}, extId: {v.get('extId')}, type: {v.get('vpcType')}")
        print(f"  extSubnets: {v.get('externalSubnets')}")
        print(f"  ERP: {v.get('externallyRoutablePrefixes')}")
        print(f"  DHCP: {v.get('commonDhcpOptions')}")
