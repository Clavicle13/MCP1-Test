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
    vpcs = client.get(f"{base_url}/networking/v4.3/config/vpcs", headers=headers).json().get("data", [])
    subs = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=headers).json().get("data", [])
    vlan_sub = next(s for s in subs if s.get("name") == "secondary-DM3-POC081")
    vlan_id = vlan_sub["extId"]

    for rt in rts:
        vpc_id = rt.get("vpcReference")
        vpc_obj = next((v for v in vpcs if v["extId"] == vpc_id), None)
        vpc_name = vpc_obj.get("name") if vpc_obj else "Unknown"
        rt_id = rt.get("extId")
        r_routes = client.get(f"{base_url}/networking/v4.3/config/route-tables/{rt_id}/routes", headers=headers).json().get("data", [])
        print(f"VPC: {vpc_name} | RT: {rt_id} | Routes count: {len(r_routes)}")
        if len(r_routes) == 0:
            print(f"  Adding default route to {vpc_name}...")
            payload = {
                "name": f"Default-Route-{vpc_name}",
                "routeType": "STATIC",
                "destination": {"ipv4": {"ip": {"value": "0.0.0.0", "prefixLength": 32}, "prefixLength": 0}},
                "nexthop": {"nexthopType": "EXTERNAL_SUBNET", "nexthopReference": vlan_id},
                "isActive": True
            }
            r_add = client.post(f"{base_url}/networking/v4.3/config/route-tables/{rt_id}/routes", json=payload, headers=headers)
            print("  Add status:", r_add.status_code, r_add.text)
