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

with httpx.Client(verify=False, timeout=20.0, auth=auth) as client:
    rts = client.get(f"{base_url}/networking/v4.3/config/route-tables", headers=headers).json().get("data", [])
    time.sleep(1)
    vpcs = client.get(f"{base_url}/networking/v4.3/config/vpcs", headers=headers).json().get("data", [])
    time.sleep(1)
    subnets = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=headers).json().get("data", [])
    time.sleep(1)

    print("=" * 100)
    print("FINAL VERIFICATION SUMMARY REPORT")
    print("=" * 100)

    print("\n--- 1. VPCS & DNS SERVER ENTRIES ---")
    print(f"{'VPC Name':<20} | {'VPC Type':<10} | {'DNS Servers':<20} | {'ExtID'}")
    print("-" * 100)
    for v in sorted(vpcs, key=lambda x: x.get('name', '')):
        dns_servers = [d.get("ipv4", {}).get("value") for d in v.get("commonDhcpOptions", {}).get("domainNameServers", [])] if v.get("commonDhcpOptions") else []
        print(f"{v.get('name'):<20} | {v.get('vpcType'):<10} | {str(dns_servers):<20} | {v.get('extId')}")

    print("\n--- 2. SUBNETS ---")
    print(f"{'Subnet Name':<25} | {'Type':<8} | {'CIDR':<18} | {'DNS Servers':<15} | {'ExtID'}")
    print("-" * 100)
    for s in sorted(subnets, key=lambda x: x.get('name', '')):
        dns_servers = [d.get("ipv4", {}).get("value") for d in s.get("dhcpOptions", {}).get("domainNameServers", [])] if s.get("dhcpOptions") else []
        ip_sub = s.get("ipConfig", [{}])[0].get("ipv4", {}).get("ipSubnet", {})
        cidr = f"{ip_sub.get('ip', {}).get('value')}/{ip_sub.get('prefixLength')}" if ip_sub else "N/A"
        print(f"{s.get('name'):<25} | {s.get('subnetType'):<8} | {cidr:<18} | {str(dns_servers):<15} | {s.get('extId')}")

    print("\n--- 3. ROUTE TABLES & ROUTES (INCLUDING 0.0.0.0/0) ---")
    print(f"{'VPC Name':<20} | {'Destination':<15} | {'Route Type':<12} | {'Next Hop Type':<18} | {'Next Hop Target'}")
    print("-" * 100)
    for rt in sorted(rts, key=lambda x: x.get('extId', '')):
        vpc_id = rt.get("vpcReference")
        vpc_obj = next((v for v in vpcs if v["extId"] == vpc_id), None)
        vpc_name = vpc_obj.get("name") if vpc_obj else "Unknown"
        rt_id = rt.get("extId")
        
        # Retry with backoff if rate limited
        routes_data = []
        for attempt in range(5):
            time.sleep(1.5)
            r_resp = client.get(f"{base_url}/networking/v4.3/config/route-tables/{rt_id}/routes", headers={"NTNX-Request-Id": str(uuid.uuid4())})
            if r_resp.status_code == 200:
                routes_data = r_resp.json().get("data", [])
                break
            time.sleep(2)

        if isinstance(routes_data, list):
            for r in routes_data:
                if isinstance(r, dict):
                    dest = r.get("destination", {}).get("ipv4", {})
                    dest_cidr = f"{dest.get('ip', {}).get('value')}/{dest.get('prefixLength')}" if dest else "N/A"
                    nh = r.get("nexthop", {})
                    nh_type = nh.get("nexthopType", "N/A")
                    nh_name = nh.get("nexthopName") or nh.get("nexthopReference") or "N/A"
                    r_type = r.get("routeType", "N/A")
                    print(f"{vpc_name:<20} | {dest_cidr:<15} | {r_type:<12} | {nh_type:<18} | {nh_name}")

    print("=" * 100)
