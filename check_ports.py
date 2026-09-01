import httpx
import json
from dotenv import load_dotenv
load_dotenv(override=True)
from config import Config

auth = (Config.PC_USERNAME, Config.PC_PASSWORD)
base_url = f"https://{Config.PC_HOST}:{Config.PC_PORT}/api"

with httpx.Client(verify=False, timeout=15.0, auth=auth) as client:
    # 1. Check VNICs on this subnet
    r = client.get(f"{base_url}/networking/v4.3/config/subnets/1f1232f6-e0c5-40dd-b218-9efd45927dc4/vnics")
    print("VNICs on Transit-NonERP-01:")
    print(r.text)

    # 2. Check VMs
    r_vms = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms")
    vms = r_vms.json().get("data", [])
    print("\nVMs on Cluster:")
    for vm in vms:
        nics = vm.get("nics", [])
        print(f"VM: {vm.get('name')} ({vm.get('extId')})")
        for nic in nics:
            sub_ref = nic.get("networkInfo", {}).get("subnet", {}).get("extId")
            print(f"  NIC: {nic.get('extId')} | Subnet: {sub_ref}")
