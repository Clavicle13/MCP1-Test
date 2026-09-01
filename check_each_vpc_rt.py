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

    for v in vpcs:
        v_name = v.get("name")
        v_id = v.get("extId")
        # find matching RT
        matching_rt = next((rt for rt in rts if rt.get("vpcReference") == v_id), None)
        if matching_rt:
            rt_id = matching_rt.get("extId")
            routes = client.get(f"{base_url}/networking/v4.3/config/route-tables/{rt_id}/routes", headers=headers).json().get("data", [])
            print(f"VPC: {v_name} (RT: {rt_id}) has {len(routes)} routes:")
            has_default = False
            for r in routes:
                if isinstance(r, dict):
                    dest = r.get("destination", {}).get("ipv4", {})
                    dest_cidr = f"{dest.get('ip', {}).get('value')}/{dest.get('prefixLength')}"
                    nh = r.get("nexthop", {})
                    nh_type = nh.get("nexthopType", "N/A")
                    nh_name = nh.get("nexthopName") or nh.get("nexthopReference") or "N/A"
                    r_type = r.get("routeType", "N/A")
                    print(f"  -> Dest: {dest_cidr:<15} | Type: {r_type:<10} | NextHop: {nh_type} ({nh_name})")
                    if dest_cidr == "0.0.0.0/0":
                        has_default = True
            if not has_default:
                print(f"  -> Adding Default Route to {v_name}...")
                payload = {
                    "name": f"Default-Route-{v_name}",
                    "routeType": "STATIC",
                    "destination": {"ipv4": {"ip": {"value": "0.0.0.0", "prefixLength": 32}, "prefixLength": 0}},
                    "nexthop": {"nexthopType": "EXTERNAL_SUBNET", "nexthopReference": vlan_id},
                    "isActive": True
                }
                res = client.post(f"{base_url}/networking/v4.3/config/route-tables/{rt_id}/routes", json=payload, headers=headers)
                print(f"     Add result: {res.status_code}")
