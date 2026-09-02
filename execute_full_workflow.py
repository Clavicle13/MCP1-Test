import asyncio
import time
import uuid
import httpx
import json
from dotenv import load_dotenv
load_dotenv(override=True)
from config import Config

auth = (Config.PC_USERNAME, Config.PC_PASSWORD)
base_url = f"https://{Config.PC_HOST}:{Config.PC_PORT}/api"

def get_headers():
    return {
        "NTNX-Request-Id": str(uuid.uuid4()),
        "If-Match": "*",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

def wait_for_task(client: httpx.Client, task_ext_id: str, desc: str, timeout_secs: int = 180):
    print(f"  [Task] Waiting for task {task_ext_id} ({desc})...")
    start = time.time()
    while time.time() - start < timeout_secs:
        r = client.get(f"{base_url}/prism/v4.3/config/tasks/{task_ext_id}", headers=get_headers())
        if r.status_code == 200:
            task_data = r.json().get("data", {})
            status = task_data.get("status")
            if status in ("SUCCEEDED", "SUCCESS"):
                print(f"  [OK] Task {desc} SUCCEEDED.")
                return task_data
            elif status in ("FAILED", "CANCELED", "ERROR"):
                err = task_data.get("errorMessages", [])
                raise RuntimeError(f"Task {desc} FAILED with status {status}: {err}")
        time.sleep(3)
    raise TimeoutError(f"Task {desc} timed out after {timeout_secs}s")


def run_full_workflow():
    print("=" * 80)
    print("  NUTANIX PRISM CENTRAL: END-TO-END TEARDOWN & CLEAN RE-CREATION")
    print("=" * 80)

    with httpx.Client(verify=False, timeout=30.0, auth=auth) as client:
        # Dynamic Cluster and Virtual Switch discovery
        r_cls = client.get(f"{base_url}/clustermgmt/v4.0.b2/config/clusters", headers=get_headers()).json().get("data", [])
        cluster_ext_id = r_cls[0].get("extId") if r_cls else "00065a75-6072-0ba0-0000-0000000297f5"
        print(f" -> Discovered Cluster ExtID: {cluster_ext_id}")

        r_vsw = client.get(f"{base_url}/networking/v4.3/config/virtual-switches", headers=get_headers()).json().get("data", [])
        vswitch_ext_id = r_vsw[0].get("extId") if r_vsw else "82ae0b0c-c6e8-47c5-a64c-7d7301babfab"
        print(f" -> Discovered Virtual Switch ExtID: {vswitch_ext_id}")
        # =====================================================================
        # CAPTURE PHASE: Capture DNS Server and Subnet details from Primary Subnet
        # =====================================================================
        print("\n" + "=" * 50)
        print(">>> SUBNET INFO CAPTURE PHASE")
        print("=" * 50)
        r_all_subnets = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=get_headers()).json().get("data", [])
        primary_subnet = next((s for s in r_all_subnets if s.get("name") == "primary-DM3-POC081"), None)
        
        captured_dns_servers = []
        if primary_subnet and primary_subnet.get("dhcpOptions"):
            dns_list = primary_subnet.get("dhcpOptions", {}).get("domainNameServers", [])
            for d in dns_list:
                val = d.get("ipv4", {}).get("value")
                if val:
                    captured_dns_servers.append(val)
        
        if not captured_dns_servers:
            captured_dns_servers = ["10.55.81.6"]
        
        primary_dns_ip = captured_dns_servers[0]
        print(f" -> Dynamically Captured DNS Server(s) from primary subnet: {captured_dns_servers}")
        print(f" -> Using DNS Server IP: {primary_dns_ip} for all VPCs and Subnets.")

        # =====================================================================
        # PHASE 1: TEARDOWN
        # =====================================================================
        print("\n" + "=" * 50)
        print(">>> PHASE 1: TEARDOWN PREVIOUSLY CREATED CONSTRUCTS")
        print("=" * 50)

        # 1.0 Delete any Floating IPs
        r_fips = client.get(f"{base_url}/networking/v4.3/config/floating-ips", headers=get_headers()).json().get("data", [])
        for f in r_fips:
            f_id = f.get("extId")
            print(f"Deleting Floating IP {f_id}...")
            res = client.delete(f"{base_url}/networking/v4.3/config/floating-ips/{f_id}", headers=get_headers())
            if res.status_code in (200, 202):
                task_id = res.json().get("data", {}).get("extId")
                if task_id:
                    wait_for_task(client, task_id, "Delete Floating IP")

        # 1.0.0 Delete existing Windows VM if present
        try:
            r_all_vms_pre = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms", headers=get_headers()).json().get("data", [])
            win_vm_old = next((v for v in r_all_vms_pre if v.get("name") == Config.WINDOWS_VM_NAME), None)
            if win_vm_old:
                win_id = win_vm_old.get("extId")
                print(f"Deleting existing Windows VM '{Config.WINDOWS_VM_NAME}' ({win_id})...")
                r_win_single = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{win_id}", headers=get_headers())
                win_del_etag = r_win_single.headers.get("ETag") or r_win_single.json().get("data", {}).get("$reserved", {}).get("ETag")
                del_win_headers = get_headers()
                if win_del_etag:
                    del_win_headers["If-Match"] = win_del_etag
                res_del_win = client.delete(f"{base_url}/vmm/v4.2/ahv/config/vms/{win_id}", headers=del_win_headers)
                if res_del_win.status_code in (200, 202):
                    t_win_del = res_del_win.json().get("data", {}).get("extId")
                    if t_win_del:
                        wait_for_task(client, t_win_del, f"Delete Windows VM '{Config.WINDOWS_VM_NAME}'")
        except Exception as exc:
            print(f"  [Warning] Windows VM teardown check: {exc}")

        # 1.1 Discover live VPCs and Subnets
        r_vpcs = client.get(f"{base_url}/networking/v4.3/config/vpcs", headers=get_headers()).json().get("data", [])
        r_subnets = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=get_headers()).json().get("data", [])

        spoke_subnet_names = ["Spoke-ERP-1-01", "Spoke-ERP-2-01", "Spoke-ERP-3-01", "Test-Spoke-Sub-01"]
        transit_subnet_names = ["Transit-ERP-01", "Transit-NonERP-01"]
        spoke_vpc_names = ["Spoke-VPC-1-01", "Spoke-VPC-2-01", "Spoke-VPC-3-01", "Test-Spoke-01"]
        transit_vpc_names = ["Transit-VPC-01"]
        vlan_subnet_names = ["secondary-DM3-POC081"]

        # Step 1.0.1: Detach VM NICs attached to Transit or Spoke subnets before deleting subnets
        print("\n[1.0.1] Detaching VM NICs attached to Transit / Spoke subnets...")
        target_subnet_ids = {s.get("extId") for s in r_subnets if s.get("name") in (spoke_subnet_names + transit_subnet_names)}
        try:
            r_all_vms = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms", headers=get_headers()).json().get("data", [])
            for vm in r_all_vms:
                vm_id = vm.get("extId")
                r_vm_nics = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}/nics", headers=get_headers()).json().get("data", [])
                for nic in r_vm_nics:
                    net_info = nic.get("networkInfo", {}) or nic.get("nicNetworkInfo", {})
                    sub_ref = net_info.get("subnet", {}).get("extId")
                    if sub_ref in target_subnet_ids:
                        nic_id = nic.get("extId")
                        print(f"Detaching NIC {nic_id} from VM '{vm.get('name')}' ({vm_id})...")
                        r_vm_single = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}", headers=get_headers())
                        v_etag = r_vm_single.headers.get("ETag") or r_vm_single.json().get("data", {}).get("$reserved", {}).get("ETag")
                        del_nic_headers = get_headers()
                        if v_etag:
                            del_nic_headers["If-Match"] = v_etag
                        res_del = client.delete(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}/nics/{nic_id}", headers=del_nic_headers)
                        if res_del.status_code in (200, 202):
                            t_id = res_del.json().get("data", {}).get("extId")
                            if t_id:
                                wait_for_task(client, t_id, f"Detach NIC from {vm.get('name')}")
        except Exception as exc:
            print(f"  [Warning] NIC detachment error: {exc}")

        # Step 1.1: Delete Spoke Subnets
        print("\n[1.1] Deleting Spoke Subnets...")
        for s in r_subnets:
            if s.get("name") in spoke_subnet_names:
                ext_id = s.get("extId")
                name = s.get("name")
                print(f"Deleting subnet '{name}' ({ext_id})...")
                res = client.delete(f"{base_url}/networking/v4.3/config/subnets/{ext_id}", headers=get_headers())
                if res.status_code in (200, 202):
                    task_id = res.json().get("data", {}).get("extId")
                    if task_id:
                        wait_for_task(client, task_id, f"Delete Subnet {name}")
                elif res.status_code == 404:
                    print(f"  Subnet {name} already deleted.")
                else:
                    print(f"  Warning deleting {name}: {res.status_code} - {res.text}")

        # Step 1.2: Delete Spoke VPCs
        print("\n[1.2] Deleting Spoke VPCs...")
        for v in r_vpcs:
            if v.get("name") in spoke_vpc_names:
                ext_id = v.get("extId")
                name = v.get("name")
                print(f"Deleting VPC '{name}' ({ext_id})...")
                res = client.delete(f"{base_url}/networking/v4.3/config/vpcs/{ext_id}", headers=get_headers())
                if res.status_code in (200, 202):
                    task_id = res.json().get("data", {}).get("extId")
                    if task_id:
                        wait_for_task(client, task_id, f"Delete VPC {name}")
                elif res.status_code == 404:
                    print(f"  VPC {name} already deleted.")
                else:
                    print(f"  Warning deleting {name}: {res.status_code} - {res.text}")

        # Step 1.3: Delete Transit Subnets
        print("\n[1.3] Deleting Transit Subnets...")
        for s in r_subnets:
            if s.get("name") in transit_subnet_names:
                ext_id = s.get("extId")
                name = s.get("name")
                print(f"Deleting subnet '{name}' ({ext_id})...")
                res = client.delete(f"{base_url}/networking/v4.3/config/subnets/{ext_id}", headers=get_headers())
                if res.status_code in (200, 202):
                    task_id = res.json().get("data", {}).get("extId")
                    if task_id:
                        wait_for_task(client, task_id, f"Delete Subnet {name}")
                elif res.status_code == 404:
                    print(f"  Subnet {name} already deleted.")
                else:
                    print(f"  Warning deleting {name}: {res.status_code} - {res.text}")

        # Step 1.4: Delete Transit VPC
        print("\n[1.4] Deleting Transit VPC...")
        for v in r_vpcs:
            if v.get("name") in transit_vpc_names:
                ext_id = v.get("extId")
                name = v.get("name")
                print(f"Deleting VPC '{name}' ({ext_id})...")
                res = client.delete(f"{base_url}/networking/v4.3/config/vpcs/{ext_id}", headers=get_headers())
                if res.status_code in (200, 202):
                    task_id = res.json().get("data", {}).get("extId")
                    if task_id:
                        wait_for_task(client, task_id, f"Delete VPC {name}")
                elif res.status_code == 404:
                    print(f"  VPC {name} already deleted.")
                else:
                    print(f"  Warning deleting {name}: {res.status_code} - {res.text}")

        # Step 1.5: Delete External VLAN Subnet
        print("\n[1.5] Deleting External VLAN Subnet...")
        for s in r_subnets:
            if s.get("name") in vlan_subnet_names:
                ext_id = s.get("extId")
                name = s.get("name")
                print(f"Deleting external VLAN subnet '{name}' ({ext_id})...")
                res = client.delete(f"{base_url}/networking/v4.3/config/subnets/{ext_id}", headers=get_headers())
                if res.status_code in (200, 202):
                    task_id = res.json().get("data", {}).get("extId")
                    if task_id:
                        wait_for_task(client, task_id, f"Delete Subnet {name}")
                elif res.status_code == 404:
                    print(f"  Subnet {name} already deleted.")
                else:
                    print(f"  Warning deleting {name}: {res.status_code} - {res.text}")

        print("\n[OK] PHASE 1 COMPLETE: All target networking constructs torn down.")

        # =====================================================================
        # PHASE 2: CLEAN RE-CREATION
        # =====================================================================
        print("\n" + "=" * 50)
        print(">>> PHASE 2: CLEAN RE-CREATION FROM SCRATCH")
        print("=" * 50)

        # Step 2.1: Create External VLAN Subnet
        print("\n[2.1] Creating External VLAN Subnet ('secondary-DM3-POC081')...")
        vlan_subnet_payload = {
            "name": "secondary-DM3-POC081",
            "subnetType": "VLAN",
            "isExternal": True,
            "networkId": 0,
            "clusterReference": cluster_ext_id,
            "virtualSwitchReference": vswitch_ext_id,
            "ipConfig": [
                {
                    "ipv4": {
                        "ipSubnet": {
                            "ip": {"value": "10.55.81.128", "prefixLength": 32},
                            "prefixLength": 25
                        },
                        "defaultGatewayIp": {"value": "10.55.81.129", "prefixLength": 32},
                        "poolList": [
                            {
                                "startIp": {"value": "10.55.81.160", "prefixLength": 32},
                                "endIp": {"value": "10.55.81.253", "prefixLength": 32}
                            }
                        ]
                    }
                }
            ]
        }
        res = client.post(f"{base_url}/networking/v4.3/config/subnets", json=vlan_subnet_payload, headers=get_headers())
        if res.status_code not in (200, 201, 202):
            raise RuntimeError(f"Failed to create external VLAN subnet: {res.status_code} - {res.text}")
        vlan_task_id = res.json().get("data", {}).get("extId")
        vlan_task_res = wait_for_task(client, vlan_task_id, "Create External VLAN Subnet")
        
        # Get the new VLAN Subnet ExtID
        r_subs = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=get_headers()).json().get("data", [])
        vlan_subnet_ext_id = next(s["extId"] for s in r_subs if s.get("name") == "secondary-DM3-POC081")
        print(f" -> External VLAN Subnet created with ExtID: {vlan_subnet_ext_id}")

        # Step 2.2: Create Transit VPC with DNS Server and External Subnet attachment
        print(f"\n[2.2] Creating Transit VPC ('Transit-VPC-01') with DNS Server ({primary_dns_ip}) and ERP...")
        transit_vpc_payload = {
            "name": "Transit-VPC-01",
            "vpcType": "REGULAR",
            "commonDhcpOptions": {
                "domainNameServers": [
                    {
                        "ipv4": {
                            "value": primary_dns_ip,
                            "prefixLength": 32
                        }
                    }
                ]
            },
            "externalSubnets": [
                {
                    "subnetReference": vlan_subnet_ext_id,
                    "activeGatewayCount": 2
                }
            ],
            "externallyRoutablePrefixes": [
                {
                    "ipv4": {
                        "ip": {"value": "10.10.10.0", "prefixLength": 32},
                        "prefixLength": 24
                    }
                },
                {
                    "ipv4": {
                        "ip": {"value": "1.1.1.0", "prefixLength": 32},
                        "prefixLength": 24
                    }
                },
                {
                    "ipv4": {
                        "ip": {"value": "2.2.2.0", "prefixLength": 32},
                        "prefixLength": 24
                    }
                },
                {
                    "ipv4": {
                        "ip": {"value": "3.3.3.0", "prefixLength": 32},
                        "prefixLength": 24
                    }
                }
            ]
        }
        res = client.post(f"{base_url}/networking/v4.3/config/vpcs", json=transit_vpc_payload, headers=get_headers())
        if res.status_code not in (200, 201, 202):
            raise RuntimeError(f"Failed to create Transit VPC: {res.status_code} - {res.text}")
        transit_task_id = res.json().get("data", {}).get("extId")
        wait_for_task(client, transit_task_id, "Create Transit VPC")

        # Get Transit VPC ExtID
        r_vpcs_new = client.get(f"{base_url}/networking/v4.3/config/vpcs", headers=get_headers()).json().get("data", [])
        transit_vpc_ext_id = next(v["extId"] for v in r_vpcs_new if v.get("name") == "Transit-VPC-01")
        print(f" -> Transit VPC created with ExtID: {transit_vpc_ext_id}")

        # Step 2.3: Create Transit VPC Subnets (Transit-ERP-01 and Transit-NonERP-01)
        print(f"\n[2.3] Creating Transit VPC Subnets with DNS Server ({primary_dns_ip})...")
        transit_erp_subnet_payload = {
            "name": "Transit-ERP-01",
            "subnetType": "OVERLAY",
            "vpcReference": transit_vpc_ext_id,
            "dhcpOptions": {
                "domainNameServers": [
                    {"ipv4": {"value": primary_dns_ip, "prefixLength": 32}}
                ]
            },
            "ipConfig": [
                {
                    "ipv4": {
                        "ipSubnet": {
                            "ip": {"value": "10.10.10.0", "prefixLength": 32},
                            "prefixLength": 24
                        },
                        "defaultGatewayIp": {"value": "10.10.10.1", "prefixLength": 32},
                        "dhcpServerAddress": {"value": "10.10.10.1", "prefixLength": 32},
                        "poolList": [
                            {
                                "startIp": {"value": "10.10.10.160", "prefixLength": 32},
                                "endIp": {"value": "10.10.10.253", "prefixLength": 32}
                            }
                        ]
                    }
                }
            ]
        }
        res = client.post(f"{base_url}/networking/v4.3/config/subnets", json=transit_erp_subnet_payload, headers=get_headers())
        if res.status_code not in (200, 201, 202):
            raise RuntimeError(f"Failed to create Transit-ERP-01 subnet: {res.status_code} - {res.text}")
        t_erp_task_id = res.json().get("data", {}).get("extId")
        wait_for_task(client, t_erp_task_id, "Create Transit-ERP-01 Subnet")

        transit_nonerp_subnet_payload = {
            "name": "Transit-NonERP-01",
            "subnetType": "OVERLAY",
            "vpcReference": transit_vpc_ext_id,
            "dhcpOptions": {
                "domainNameServers": [
                    {"ipv4": {"value": primary_dns_ip, "prefixLength": 32}}
                ]
            },
            "ipConfig": [
                {
                    "ipv4": {
                        "ipSubnet": {
                            "ip": {"value": "20.20.20.0", "prefixLength": 32},
                            "prefixLength": 24
                        },
                        "defaultGatewayIp": {"value": "20.20.20.1", "prefixLength": 32},
                        "dhcpServerAddress": {"value": "20.20.20.1", "prefixLength": 32},
                        "poolList": [
                            {
                                "startIp": {"value": "20.20.20.160", "prefixLength": 32},
                                "endIp": {"value": "20.20.20.253", "prefixLength": 32}
                            }
                        ]
                    }
                }
            ]
        }
        res = client.post(f"{base_url}/networking/v4.3/config/subnets", json=transit_nonerp_subnet_payload, headers=get_headers())
        if res.status_code not in (200, 201, 202):
            raise RuntimeError(f"Failed to create Transit-NonERP-01 subnet: {res.status_code} - {res.text}")
        t_nonerp_task_id = res.json().get("data", {}).get("extId")
        wait_for_task(client, t_nonerp_task_id, "Create Transit-NonERP-01 Subnet")

        # Discover newly created Transit-NonERP-01 Subnet ExtID
        r_subnets_after_transit = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=get_headers()).json().get("data", [])
        transit_nonerp_subnet_obj = next((s for s in r_subnets_after_transit if s.get("name") == "Transit-NonERP-01"), None)
        transit_nonerp_ext_id = transit_nonerp_subnet_obj.get("extId") if transit_nonerp_subnet_obj else None

        # Step 2.3.1: Attach Linux Bastion VM to Transit-NonERP-01 Subnet
        print(f"\n[2.3.1] Attaching Linux Bastion VM ('{Config.BASTION_VM_NAME}') to Transit-NonERP-01...")
        r_vms = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms", headers=get_headers()).json().get("data", [])
        bastion_vm = next((vm for vm in r_vms if vm.get("name") == Config.BASTION_VM_NAME), None)
        if not bastion_vm:
            print(f"  [Warning] Bastion VM '{Config.BASTION_VM_NAME}' not found in cluster inventory.")
        elif not transit_nonerp_ext_id:
            print("  [Warning] Transit-NonERP-01 subnet ExtID not found.")
        else:
            bastion_vm_id = bastion_vm.get("extId")

            # Detach any existing basic/stale NICs from Bastion VM before attaching to VPC subnet
            r_bastion_nics = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{bastion_vm_id}/nics", headers=get_headers()).json().get("data", [])
            for old_nic in r_bastion_nics:
                old_nic_id = old_nic.get("extId")
                print(f"  Detaching existing NIC {old_nic_id} from Bastion VM before attaching to VPC subnet...")
                r_vm_s = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{bastion_vm_id}", headers=get_headers())
                v_etag = r_vm_s.headers.get("ETag") or r_vm_s.json().get("data", {}).get("$reserved", {}).get("ETag")
                d_headers = get_headers()
                if v_etag:
                    d_headers["If-Match"] = v_etag
                res_d = client.delete(f"{base_url}/vmm/v4.2/ahv/config/vms/{bastion_vm_id}/nics/{old_nic_id}", headers=d_headers)
                if res_d.status_code in (200, 202):
                    t_d = res_d.json().get("data", {}).get("extId")
                    if t_d:
                        wait_for_task(client, t_d, f"Detach old NIC {old_nic_id}")

            r_vm_single = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{bastion_vm_id}", headers=get_headers())
            vm_etag = r_vm_single.headers.get("ETag") or r_vm_single.json().get("data", {}).get("$reserved", {}).get("ETag")
            nic_attach_headers = get_headers()
            if vm_etag:
                nic_attach_headers["If-Match"] = vm_etag

            attach_nic_payload = {
                "backingInfo": {
                    "isConnected": True
                },
                "networkInfo": {
                    "nicType": "NORMAL_NIC",
                    "subnet": {
                        "extId": transit_nonerp_ext_id
                    },
                    "vlanMode": "ACCESS",
                    "ipv4Config": {
                        "ipAddress": {
                            "value": Config.BASTION_VM_IP,
                            "prefixLength": 32
                        }
                    }
                }
            }
            res_attach = client.post(f"{base_url}/vmm/v4.2/ahv/config/vms/{bastion_vm_id}/nics", json=attach_nic_payload, headers=nic_attach_headers)
            if res_attach.status_code in (200, 201, 202):
                t_nic_id = res_attach.json().get("data", {}).get("extId")
                if t_nic_id:
                    wait_for_task(client, t_nic_id, f"Attach Bastion VM '{Config.BASTION_VM_NAME}' to Transit-NonERP-01")
                print(f"  [OK] Successfully attached Bastion VM '{Config.BASTION_VM_NAME}' to Transit-NonERP-01.")
            else:
                print(f"  [Warning] Failed to attach NIC to Bastion VM: {res_attach.status_code} - {res_attach.text}")

        # Step 2.3.2: Create Windows VM on Transit-NonERP-01 Subnet
        print(f"\n[2.3.2] Creating Windows VM ('{Config.WINDOWS_VM_NAME}') on Transit-NonERP-01...")
        try:
            # Query Image for Windows 2022
            r_imgs = client.get(f"{base_url}/vmm/v4.0.b1/content/images", headers=get_headers()).json().get("data", []) or client.get(f"{base_url}/vmm/v4.2/content/images", headers=get_headers()).json().get("data", [])
            win_img = next((img for img in r_imgs if "windows" in img.get("name", "").lower() and "2022" in img.get("name", "").lower()), None)
            win_img_id = win_img.get("extId") if win_img else None
            print(f" -> Found Windows Image: {win_img.get('name') if win_img else 'Not Found'} ({win_img_id})")

            win_vm_payload = {
                "name": Config.WINDOWS_VM_NAME,
                "description": "Windows Server 2022 VM created by Nutanix LangGraph Agent",
                "cluster": {"extId": cluster_ext_id},
                "numSockets": 1,
                "numCoresPerSocket": Config.WINDOWS_VM_VCPU,
                "memorySizeBytes": Config.WINDOWS_VM_MEMORY_GB * 1024 * 1024 * 1024,
            }

            create_vm_headers = {
                "NTNX-Request-Id": str(uuid.uuid4()),
                "Accept": "application/json",
                "Content-Type": "application/json"
            }
            res_win_vm = client.post(f"{base_url}/vmm/v4.2/ahv/config/vms", json=win_vm_payload, headers=create_vm_headers)
            if res_win_vm.status_code in (200, 201, 202):
                t_win_id = res_win_vm.json().get("data", {}).get("extId")
                if t_win_id:
                    wait_for_task(client, t_win_id, f"Create Windows VM '{Config.WINDOWS_VM_NAME}'")
                print(f"  [OK] Successfully created base VM '{Config.WINDOWS_VM_NAME}'.")

                # Retrieve newly created VM ExtID
                r_vms_after = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms", headers=get_headers()).json().get("data", [])
                created_win_vm = next((v for v in r_vms_after if v.get("name") == Config.WINDOWS_VM_NAME), None)
                if created_win_vm:
                    win_vm_id = created_win_vm.get("extId")

                    # 1. Attach 110 GB Boot Disk cloned from Windows Image
                    if win_img_id:
                        r_single_win = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{win_vm_id}", headers=get_headers())
                        win_etag = r_single_win.headers.get("ETag") or r_single_win.json().get("data", {}).get("$reserved", {}).get("ETag")
                        disk_headers = get_headers()
                        if win_etag:
                            disk_headers["If-Match"] = win_etag

                        obj_type = "$objectType"
                        disk_payload = {
                            "diskAddress": {"busType": "SCSI", "index": 0},
                            "backingInfo": {
                                obj_type: "vmm.v4.ahv.config.VmDisk",
                                "diskSizeBytes": Config.WINDOWS_VM_DISK_GB * 1024 * 1024 * 1024,
                                "dataSource": {
                                    obj_type: "vmm.v4.ahv.config.DataSource",
                                    "reference": {
                                        obj_type: "vmm.v4.ahv.config.ImageReference",
                                        "imageExtId": win_img_id
                                    }
                                }
                            }
                        }
                        res_disk = client.post(f"{base_url}/vmm/v4.2/ahv/config/vms/{win_vm_id}/disks", json=disk_payload, headers=disk_headers)
                        if res_disk.status_code in (200, 201, 202):
                            t_disk_id = res_disk.json().get("data", {}).get("extId")
                            if t_disk_id:
                                wait_for_task(client, t_disk_id, f"Attach {Config.WINDOWS_VM_DISK_GB}GB Boot Disk to '{Config.WINDOWS_VM_NAME}'")
                            print(f"  [OK] Successfully attached {Config.WINDOWS_VM_DISK_GB}GB boot disk cloned from image.")
                        else:
                            print(f"  [Warning] Disk attachment failed: {res_disk.status_code} - {res_disk.text}")

                    # 2. Attach NIC to Transit-NonERP-01 subnet with static IP
                    if transit_nonerp_ext_id:
                        r_single_win = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{win_vm_id}", headers=get_headers())
                        win_etag = r_single_win.headers.get("ETag") or r_single_win.json().get("data", {}).get("$reserved", {}).get("ETag")
                        nic_headers = get_headers()
                        if win_etag:
                            nic_headers["If-Match"] = win_etag

                        nic_win_payload = {
                            "backingInfo": {"isConnected": True},
                            "networkInfo": {
                                "nicType": "NORMAL_NIC",
                                "subnet": {"extId": transit_nonerp_ext_id},
                                "vlanMode": "ACCESS",
                                "ipv4Config": {
                                    "ipAddress": {
                                        "value": Config.WINDOWS_VM_IP,
                                        "prefixLength": 32
                                    }
                                }
                            }
                        }
                        res_win_nic = client.post(f"{base_url}/vmm/v4.2/ahv/config/vms/{win_vm_id}/nics", json=nic_win_payload, headers=nic_headers)
                        if res_win_nic.status_code in (200, 201, 202):
                            t_win_nic_id = res_win_nic.json().get("data", {}).get("extId")
                            if t_win_nic_id:
                                wait_for_task(client, t_win_nic_id, f"Attach NIC ({Config.WINDOWS_VM_IP}) to '{Config.WINDOWS_VM_NAME}'")
                            print(f"  [OK] Successfully attached NIC ({Config.WINDOWS_VM_IP}) on Transit-NonERP-01.")
                        else:
                            print(f"  [Warning] NIC attachment failed: {res_win_nic.status_code} - {res_win_nic.text}")
            else:
                print(f"  [Warning] Failed to create Windows VM: {res_win_vm.status_code} - {res_win_vm.text}")
        except Exception as exc:
            print(f"  [Warning] Windows VM creation error: {exc}")

        # Step 2.4: Configure Default Static Route (0.0.0.0/0) on Transit VPC
        print("\n[2.4] Configuring Default Static Route (0.0.0.0/0) on Transit VPC Route Table...")
        # Get Transit VPC Route Table
        r_rts = client.get(f"{base_url}/networking/v4.3/config/route-tables", headers=get_headers()).json().get("data", [])
        transit_rt = next(rt for rt in r_rts if rt.get("vpcReference") == transit_vpc_ext_id)
        transit_rt_ext_id = transit_rt["extId"]
        print(f" -> Transit VPC Route Table ExtID: {transit_rt_ext_id}")

        transit_route_payload = {
            "name": "Default-Route-External",
            "routeType": "STATIC",
            "destination": {
                "ipv4": {
                    "ip": {"value": "0.0.0.0", "prefixLength": 32},
                    "prefixLength": 0
                }
            },
            "nexthop": {
                "nexthopType": "EXTERNAL_SUBNET",
                "nexthopReference": vlan_subnet_ext_id
            },
            "isActive": True
        }
        res = client.post(f"{base_url}/networking/v4.3/config/route-tables/{transit_rt_ext_id}/routes", json=transit_route_payload, headers=get_headers())
        if res.status_code in (200, 201, 202):
            t_route_task_id = res.json().get("data", {}).get("extId")
            if t_route_task_id:
                wait_for_task(client, t_route_task_id, "Create Transit Default Route")
            print("  [OK] Default static route (0.0.0.0/0 -> External Subnet) created on Transit VPC.")
        else:
            print(f"  Route creation response: {res.status_code} - {res.text}")

        # Step 2.5: Create Spoke VPCs and Subnets + Default Static Routes
        spokes_config = [
            {"name": "Spoke-VPC-1-01", "subnet_name": "Spoke-ERP-1-01", "cidr": "1.1.1.0", "gw": "1.1.1.1", "p_start": "1.1.1.160", "p_end": "1.1.1.253"},
            {"name": "Spoke-VPC-2-01", "subnet_name": "Spoke-ERP-2-01", "cidr": "2.2.2.0", "gw": "2.2.2.1", "p_start": "2.2.2.160", "p_end": "2.2.2.253"},
            {"name": "Spoke-VPC-3-01", "subnet_name": "Spoke-ERP-3-01", "cidr": "3.3.3.0", "gw": "3.3.3.1", "p_start": "3.3.3.160", "p_end": "3.3.3.253"},
        ]

        for spoke in spokes_config:
            spoke_vpc_name = spoke["name"]
            subnet_name = spoke["subnet_name"]
            cidr = spoke["cidr"]
            gw = spoke["gw"]
            p_start = spoke["p_start"]
            p_end = spoke["p_end"]

            print(f"\n[2.5] Creating {spoke_vpc_name} with DNS Server ({primary_dns_ip}), External Subnet attachment, and ERP prefix...")
            spoke_vpc_payload = {
                "name": spoke_vpc_name,
                "vpcType": "REGULAR",
                "commonDhcpOptions": {
                    "domainNameServers": [
                        {"ipv4": {"value": primary_dns_ip, "prefixLength": 32}}
                    ]
                },
                "externalSubnets": [
                    {
                        "subnetReference": vlan_subnet_ext_id,
                        "activeGatewayCount": 2
                    }
                ],
                "externallyRoutablePrefixes": [
                    {
                        "ipv4": {
                            "ip": {"value": cidr, "prefixLength": 32},
                            "prefixLength": 24
                        }
                    }
                ]
            }
            res = client.post(f"{base_url}/networking/v4.3/config/vpcs", json=spoke_vpc_payload, headers=get_headers())
            if res.status_code not in (200, 201, 202):
                raise RuntimeError(f"Failed to create {spoke_vpc_name}: {res.status_code} - {res.text}")
            s_task_id = res.json().get("data", {}).get("extId")
            wait_for_task(client, s_task_id, f"Create {spoke_vpc_name}")

            # Get Spoke VPC ExtID
            r_vpcs_latest = client.get(f"{base_url}/networking/v4.3/config/vpcs", headers=get_headers()).json().get("data", [])
            spoke_vpc_ext_id = next(v["extId"] for v in r_vpcs_latest if v.get("name") == spoke_vpc_name)
            print(f" -> {spoke_vpc_name} created with ExtID: {spoke_vpc_ext_id}")

            # Create Subnet inside Spoke VPC
            print(f"Creating subnet '{subnet_name}' ({cidr}/24) inside {spoke_vpc_name} with DNS ({primary_dns_ip})...")
            spoke_subnet_payload = {
                "name": subnet_name,
                "subnetType": "OVERLAY",
                "vpcReference": spoke_vpc_ext_id,
                "dhcpOptions": {
                    "domainNameServers": [
                        {"ipv4": {"value": primary_dns_ip, "prefixLength": 32}}
                    ]
                },
                "ipConfig": [
                    {
                        "ipv4": {
                            "ipSubnet": {
                                "ip": {"value": cidr, "prefixLength": 32},
                                "prefixLength": 24
                            },
                            "defaultGatewayIp": {"value": gw, "prefixLength": 32},
                            "dhcpServerAddress": {"value": gw, "prefixLength": 32},
                            "poolList": [
                                {
                                    "startIp": {"value": p_start, "prefixLength": 32},
                                    "endIp": {"value": p_end, "prefixLength": 32}
                                }
                            ]
                        }
                    }
                ]
            }
            res = client.post(f"{base_url}/networking/v4.3/config/subnets", json=spoke_subnet_payload, headers=get_headers())
            if res.status_code not in (200, 201, 202):
                raise RuntimeError(f"Failed to create {subnet_name}: {res.status_code} - {res.text}")
            s_sub_task_id = res.json().get("data", {}).get("extId")
            wait_for_task(client, s_sub_task_id, f"Create Subnet {subnet_name}")

            # Configure Default Route on Spoke VPC Route Table pointing to External Subnet
            print(f"Configuring Default Route (0.0.0.0/0 -> External Subnet) on {spoke_vpc_name} Route Table...")
            r_rts_spoke = client.get(f"{base_url}/networking/v4.3/config/route-tables", headers=get_headers()).json().get("data", [])
            spoke_rt = next(rt for rt in r_rts_spoke if rt.get("vpcReference") == spoke_vpc_ext_id)
            spoke_rt_ext_id = spoke_rt["extId"]

            spoke_route_payload = {
                "name": f"Default-Route-{spoke_vpc_name}",
                "routeType": "STATIC",
                "destination": {
                    "ipv4": {
                        "ip": {"value": "0.0.0.0", "prefixLength": 32},
                        "prefixLength": 0
                    }
                },
                "nexthop": {
                    "nexthopType": "EXTERNAL_SUBNET",
                    "nexthopReference": vlan_subnet_ext_id
                },
                "isActive": True
            }
            res = client.post(f"{base_url}/networking/v4.3/config/route-tables/{spoke_rt_ext_id}/routes", json=spoke_route_payload, headers=get_headers())
            if res.status_code in (200, 201, 202):
                s_route_task_id = res.json().get("data", {}).get("extId")
                if s_route_task_id:
                    wait_for_task(client, s_route_task_id, f"Create {spoke_vpc_name} Default Route")
                print(f"  [OK] Default static route (0.0.0.0/0 -> External Subnet) created on {spoke_vpc_name}.")
            else:
                print(f"  Route creation response for {spoke_vpc_name}: {res.status_code} - {res.text}")

        # Step 2.6: Assign Floating IPs from External Subnet to LinuxTools and Windows2022-VM
        print("\n[2.6] Assigning Floating IPs to Linux Bastion & Windows VMs from External Subnet...")
        # 1. Floating IP for LinuxTools VM
        r_all_vms_now = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms", headers=get_headers()).json().get("data", [])
        bastion_vm_now = next((v for v in r_all_vms_now if v.get("name") == Config.BASTION_VM_NAME), None)
        if bastion_vm_now:
            b_id = bastion_vm_now.get("extId")
            r_bnics = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{b_id}/nics", headers=get_headers()).json().get("data", [])
            if r_bnics:
                b_nic_id = r_bnics[0].get("extId")
                fip_linux_payload = {
                    "name": "FIP-LinuxTools",
                    "externalSubnetReference": vlan_subnet_ext_id,
                    "association": {
                        "$objectType": "networking.v4.config.VmNicAssociation",
                        "vmNicReference": b_nic_id
                    }
                }
                res_fip_l = client.post(f"{base_url}/networking/v4.3/config/floating-ips", json=fip_linux_payload, headers=get_headers())
                if res_fip_l.status_code in (200, 201, 202):
                    t_fip_l = res_fip_l.json().get("data", {}).get("extId")
                    if t_fip_l:
                        wait_for_task(client, t_fip_l, "Allocate Floating IP for LinuxTools")
                    print("  [OK] Successfully assigned Floating IP to LinuxTools.")
                else:
                    print(f"  [Warning] Floating IP for LinuxTools response: {res_fip_l.status_code} - {res_fip_l.text}")

        # 2. Floating IP for Windows2022-VM
        created_win_vm_now = next((v for v in r_all_vms_now if v.get("name") == Config.WINDOWS_VM_NAME), None)
        if created_win_vm_now:
            w_id = created_win_vm_now.get("extId")
            r_wnics = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{w_id}/nics", headers=get_headers()).json().get("data", [])
            if r_wnics:
                w_nic_id = r_wnics[0].get("extId")
                fip_win_payload = {
                    "name": "FIP-Windows2022-VM",
                    "externalSubnetReference": vlan_subnet_ext_id,
                    "association": {
                        "$objectType": "networking.v4.config.VmNicAssociation",
                        "vmNicReference": w_nic_id
                    }
                }
                res_fip_w = client.post(f"{base_url}/networking/v4.3/config/floating-ips", json=fip_win_payload, headers=get_headers())
                if res_fip_w.status_code in (200, 201, 202):
                    t_fip_w = res_fip_w.json().get("data", {}).get("extId")
                    if t_fip_w:
                        wait_for_task(client, t_fip_w, "Allocate Floating IP for Windows VM")
                    print("  [OK] Successfully assigned Floating IP to Windows2022-VM.")
                else:
                    print(f"  [Warning] Floating IP for Windows VM response: {res_fip_w.status_code} - {res_fip_w.text}")

        # =====================================================================
        # PHASE 3: VERIFICATION
        # =====================================================================
        print("\n" + "=" * 50)
        print(">>> PHASE 3: FINAL VERIFICATION & INVENTORY")
        print("=" * 50)

        final_vpcs = client.get(f"{base_url}/networking/v4.3/config/vpcs", headers=get_headers()).json().get("data", [])
        final_subnets = client.get(f"{base_url}/networking/v4.3/config/subnets", headers=get_headers()).json().get("data", [])
        final_rts = client.get(f"{base_url}/networking/v4.3/config/route-tables", headers=get_headers()).json().get("data", [])

        print("\n--- FINAL VPCS ---")
        for v in final_vpcs:
            dns_servers = [d.get("ipv4", {}).get("value") for d in v.get("commonDhcpOptions", {}).get("domainNameServers", [])] if v.get("commonDhcpOptions") else []
            print(f"VPC: {v.get('name'):<20} | ExtID: {v.get('extId')} | DNS: {dns_servers}")

        print("\n--- FINAL SUBNETS ---")
        for s in final_subnets:
            dns_s = [d.get("ipv4", {}).get("value") for d in s.get("dhcpOptions", {}).get("domainNameServers", [])] if s.get("dhcpOptions") else []
            dns_str = str(dns_s)
            print(f"Subnet: {s.get('name'):<25} | Type: {s.get('subnetType'):<10} | DNS: {dns_str:<15} | ExtID: {s.get('extId')}")

        print("\n--- FINAL ROUTE TABLES & ROUTES ---")
        for rt in final_rts:
            vpc_id = rt.get("vpcReference")
            vpc_obj = next((v for v in final_vpcs if v["extId"] == vpc_id), None)
            vpc_name = vpc_obj.get("name") if vpc_obj else "Unknown"
            print(f"\nRoute Table for '{vpc_name}' ({rt.get('extId')}):")
            r_routes = client.get(f"{base_url}/networking/v4.3/config/route-tables/{rt.get('extId')}/routes", headers=get_headers())
            routes = r_routes.json().get("data", [])
            for r in routes:
                if isinstance(r, dict):
                    dest = r.get("destination", {}).get("ipv4", {})
                    dest_cidr = f"{dest.get('ip', {}).get('value')}/{dest.get('prefixLength')}"
                    nh = r.get("nexthop", {})
                    nh_type = nh.get("nexthopType")
                    nh_name = nh.get("nexthopName") or nh.get("nexthopReference")
                    r_type = r.get("routeType", "N/A")
                    print(f"  -> Dest: {dest_cidr:<18} | Type: {r_type:<8} | NextHop: {nh_type} ({nh_name})")

        print("\n--- BASTION VM STATUS ---")
        bastion_vm_final = next((vm for vm in client.get(f"{base_url}/vmm/v4.2/ahv/config/vms", headers=get_headers()).json().get("data", []) if vm.get("name") == Config.BASTION_VM_NAME), None)
        if bastion_vm_final:
            b_id = bastion_vm_final.get("extId")
            b_nics = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{b_id}/nics", headers=get_headers()).json().get("data", [])
            for n in b_nics:
                net_info = n.get("networkInfo", {}) or n.get("nicNetworkInfo", {})
                sub_id = net_info.get("subnet", {}).get("extId", "N/A")
                sub_name = next((s.get("name") for s in final_subnets if s.get("extId") == sub_id), "Unknown")
                ip_addr = net_info.get("ipv4Config", {}).get("ipAddress", {}).get("value", "N/A")
                mac = n.get("backingInfo", {}).get("macAddress", "N/A")
                print(f"VM: {bastion_vm_final.get('name')} | Subnet: {sub_name} ({sub_id}) | IP: {ip_addr} | MAC: {mac}")
        else:
            print(f"Bastion VM '{Config.BASTION_VM_NAME}' not found.")

        print("\n--- WINDOWS VM STATUS ---")
        win_vm_final = next((vm for vm in client.get(f"{base_url}/vmm/v4.2/ahv/config/vms", headers=get_headers()).json().get("data", []) if vm.get("name") == Config.WINDOWS_VM_NAME), None)
        if win_vm_final:
            w_id = win_vm_final.get("extId")
            w_nics = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{w_id}/nics", headers=get_headers()).json().get("data", [])
            for n in w_nics:
                net_info = n.get("networkInfo", {}) or n.get("nicNetworkInfo", {})
                sub_id = net_info.get("subnet", {}).get("extId", "N/A")
                sub_name = next((s.get("name") for s in final_subnets if s.get("extId") == sub_id), "Unknown")
                ip_addr = net_info.get("ipv4Config", {}).get("ipAddress", {}).get("value", "N/A")
                mac = n.get("backingInfo", {}).get("macAddress", "N/A")
                print(f"VM: {win_vm_final.get('name')} | Subnet: {sub_name} ({sub_id}) | IP: {ip_addr} | MAC: {mac}")
        else:
            print(f"Windows VM '{Config.WINDOWS_VM_NAME}' not found.")

        print("\n" + "=" * 80)
        print("  END-TO-END TEARDOWN & RE-CREATION COMPLETED SUCCESSFULLY!")
        print("=" * 80)

if __name__ == "__main__":
    run_full_workflow()
