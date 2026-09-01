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
    vpcs = client.get(f"{base_url}/networking/v4.3/config/vpcs", headers=headers).json().get("data", [])
    spoke_obj = next((v for v in vpcs if v["name"] == "Spoke-VPC-1-01"), None)
    if not spoke_obj:
        print("Spoke-VPC-1-01 not found")
        exit(0)
    spoke_id = spoke_obj["extId"]

    rts = client.get(f"{base_url}/networking/v4.3/config/route-tables", headers=headers).json().get("data", [])
    spoke_rt = next(rt for rt in rts if rt.get("vpcReference") == spoke_id)
    spoke_rt_id = spoke_rt["extId"]

    subs = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=headers).json().get("data", [])
    transit_erp_sub = next(s for s in subs if s.get("name") == "Transit-ERP-01")
    transit_erp_sub_id = transit_erp_sub["extId"]
    spoke_sub = next(s for s in subs if s.get("name") == "Spoke-ERP-1-01")
    spoke_sub_id = spoke_sub["extId"]

    # Test 1: Destination 0.0.0.0/1 with IP_ADDRESS next hop
    print("Testing 0.0.0.0/1...")
    p1 = {
        "name": "Route-0-1",
        "routeType": "STATIC",
        "destination": {"ipv4": {"ip": {"value": "0.0.0.0", "prefixLength": 32}, "prefixLength": 1}},
        "nexthop": {
            "nexthopType": "IP_ADDRESS",
            "nexthopReference": transit_erp_sub_id,
            "nexthopIpAddress": {"ipv4": {"value": "10.10.10.1", "prefixLength": 32}}
        },
        "isActive": True
    }
    r = client.post(f"{base_url}/networking/v4.3/config/route-tables/{spoke_rt_id}/routes", json=p1, headers=headers)
    print("0.0.0.0/1 status:", r.status_code)
    if r.status_code in (200, 201, 202):
        t_id = r.json().get("data", {}).get("extId")
        if t_id:
            for _ in range(15):
                ts = client.get(f"{base_url}/prism/v4.3/config/tasks/{t_id}", headers=headers).json().get("data", {}).get("status")
                print("Task 0.0.0.0/1:", ts)
                if ts in ("SUCCEEDED", "FAILED"):
                    break
                time.sleep(2)

    # Test 2: Destination 10.0.0.0/8 with IP_ADDRESS next hop
    print("Testing 10.0.0.0/8...")
    p2 = {
        "name": "Route-10-8",
        "routeType": "STATIC",
        "destination": {"ipv4": {"ip": {"value": "10.0.0.0", "prefixLength": 32}, "prefixLength": 8}},
        "nexthop": {
            "nexthopType": "IP_ADDRESS",
            "nexthopReference": transit_erp_sub_id,
            "nexthopIpAddress": {"ipv4": {"value": "10.10.10.1", "prefixLength": 32}}
        },
        "isActive": True
    }
    r = client.post(f"{base_url}/networking/v4.3/config/route-tables/{spoke_rt_id}/routes", json=p2, headers=headers)
    print("10.0.0.0/8 status:", r.status_code)
    if r.status_code in (200, 201, 202):
        t_id = r.json().get("data", {}).get("extId")
        if t_id:
            for _ in range(15):
                ts = client.get(f"{base_url}/prism/v4.3/config/tasks/{t_id}", headers=headers).json().get("data", {}).get("status")
                print("Task 10.0.0.0/8:", ts)
                if ts in ("SUCCEEDED", "FAILED"):
                    break
                time.sleep(2)
