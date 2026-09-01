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
    # 1. Create Subnet in Test-Spoke-01
    vpcs = client.get(f"{base_url}/networking/v4.3/config/vpcs", headers=headers).json().get("data", [])
    spoke_obj = next(v for v in vpcs if v["name"] == "Test-Spoke-01")
    spoke_id = spoke_obj["extId"]

    # Get route table
    rts = client.get(f"{base_url}/networking/v4.3/config/route-tables", headers=headers).json().get("data", [])
    spoke_rt = next(rt for rt in rts if rt.get("vpcReference") == spoke_id)
    spoke_rt_id = spoke_rt["extId"]

    # Subnets
    subs = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=headers).json().get("data", [])
    transit_erp_sub = next(s for s in subs if s.get("name") == "Transit-ERP-01")
    transit_erp_sub_id = transit_erp_sub["extId"]

    # Create Spoke Subnet
    sub_payload = {
        "name": "Test-Spoke-Sub-01",
        "subnetType": "OVERLAY",
        "vpcReference": spoke_id,
        "ipConfig": [
            {
                "ipv4": {
                    "ipSubnet": {"ip": {"value": "1.1.1.0", "prefixLength": 32}, "prefixLength": 24},
                    "defaultGatewayIp": {"value": "1.1.1.1", "prefixLength": 32},
                    "dhcpServerAddress": {"value": "1.1.1.1", "prefixLength": 32},
                    "poolList": [{"startIp": {"value": "1.1.1.160", "prefixLength": 32}, "endIp": {"value": "1.1.1.253", "prefixLength": 32}}]
                }
            }
        ]
    }
    r_sub = client.post(f"{base_url}/networking/v4.3/config/subnets", json=sub_payload, headers=headers)
    print("Create Subnet:", r_sub.status_code)
    if r_sub.status_code in (200, 201, 202):
        t_id = r_sub.json().get("data", {}).get("extId")
        if t_id:
            for _ in range(30):
                r_t = client.get(f"{base_url}/prism/v4.3/config/tasks/{t_id}", headers=headers)
                ts = r_t.json().get("data", {}).get("status")
                if ts in ("SUCCEEDED", "FAILED"):
                    print("Subnet Task:", ts)
                    break
                time.sleep(2)

    subs_new = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=headers).json().get("data", [])
    spoke_sub = next(s for s in subs_new if s.get("name") == "Test-Spoke-Sub-01")
    spoke_sub_id = spoke_sub["extId"]

    # Try Combination 1: nexthopType: IP_ADDRESS with nexthopReference = spoke_sub_id
    print("\nTesting Combo 1: IP_ADDRESS with nexthopReference = spoke_sub_id...")
    route_payload_1 = {
        "name": "Default-Route-Combo1",
        "routeType": "STATIC",
        "destination": {"ipv4": {"ip": {"value": "0.0.0.0", "prefixLength": 32}, "prefixLength": 0}},
        "nexthop": {
            "nexthopType": "IP_ADDRESS",
            "nexthopReference": spoke_sub_id,
            "nexthopIpAddress": {"ipv4": {"value": "1.1.1.254", "prefixLength": 32}}
        },
        "isActive": True
    }
    r1 = client.post(f"{base_url}/networking/v4.3/config/route-tables/{spoke_rt_id}/routes", json=route_payload_1, headers=headers)
    print("Combo 1 status:", r1.status_code, r1.text)
    if r1.status_code in (200, 201, 202):
        t_id = r1.json().get("data", {}).get("extId")
        if t_id:
            for _ in range(30):
                r_t = client.get(f"{base_url}/prism/v4.3/config/tasks/{t_id}", headers=headers)
                ts = r_t.json().get("data", {}).get("status")
                print("Combo 1 Task status:", ts)
                if ts in ("SUCCEEDED", "FAILED"):
                    if ts == "FAILED":
                        print("Error messages:", r_t.json().get("data", {}).get("errorMessages"))
                    break
                time.sleep(2)

    # Try Combination 2: nexthopType: LOCAL_SUBNET with non-default destination (e.g. 10.0.0.0/8 or 0.0.0.0/1) or test default route with transit_erp_sub_id
    print("\nTesting Combo 2: IP_ADDRESS with nexthopReference = transit_erp_sub_id...")
    route_payload_2 = {
        "name": "Default-Route-Combo2",
        "routeType": "STATIC",
        "destination": {"ipv4": {"ip": {"value": "0.0.0.0", "prefixLength": 32}, "prefixLength": 0}},
        "nexthop": {
            "nexthopType": "IP_ADDRESS",
            "nexthopReference": transit_erp_sub_id,
            "nexthopIpAddress": {"ipv4": {"value": "10.10.10.1", "prefixLength": 32}}
        },
        "isActive": True
    }
    r2 = client.post(f"{base_url}/networking/v4.3/config/route-tables/{spoke_rt_id}/routes", json=route_payload_2, headers=headers)
    print("Combo 2 status:", r2.status_code, r2.text)
    if r2.status_code in (200, 201, 202):
        t_id = r2.json().get("data", {}).get("extId")
        if t_id:
            for _ in range(30):
                r_t = client.get(f"{base_url}/prism/v4.3/config/tasks/{t_id}", headers=headers)
                ts = r_t.json().get("data", {}).get("status")
                print("Combo 2 Task status:", ts)
                if ts in ("SUCCEEDED", "FAILED"):
                    if ts == "FAILED":
                        print("Error messages:", r_t.json().get("data", {}).get("errorMessages"))
                    break
                time.sleep(2)
