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
        print(f"Name: {v.get('name')} | extId: {v.get('extId')} | vpcType: {v.get('vpcType')}")
        print(f"  externalSubnets: {v.get('externalSubnets')}")
        print(f"  externallyRoutablePrefixes: {v.get('externallyRoutablePrefixes')}")
        print(f"  commonDhcpOptions: {v.get('commonDhcpOptions')}")

    r_subnets = client.get(f"{base_url}/networking/v4.3/config/subnets")
    subnets = r_subnets.json().get("data", [])
    print("\n=== SUBNETS ===")
    for s in subnets:
        print(f"Name: {s.get('name')} | extId: {s.get('extId')} | subnetType: {s.get('subnetType')} | vlanId: {s.get('vlanId')} | isExternal: {s.get('isExternal')} | vpcRef: {s.get('vpcReference')}")
        print(f"  clusterRef: {s.get('clusterReference')}")
        print(f"  dhcpOptions: {s.get('dhcpOptions')}")
        print(f"  ipConfig: {s.get('ipConfig')}")
