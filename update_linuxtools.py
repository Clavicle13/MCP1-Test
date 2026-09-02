import time
import uuid
import httpx
import json
from dotenv import load_dotenv
load_dotenv(override=True)
from config import Config

auth = (Config.PC_USERNAME, Config.PC_PASSWORD)
base_url = f"https://{Config.PC_HOST}:{Config.PC_PORT}/api"

TARGET_VCPU = 8
TARGET_MEMORY_BYTES = 12 * 1024 * 1024 * 1024       # 12 GiB = 12,884,901,888 bytes
TARGET_DISK_BYTES = 110 * 1024 * 1024 * 1024        # 110 GiB = 118,111,600,640 bytes

def get_headers(etag=None):
    h = {
        "NTNX-Request-Id": str(uuid.uuid4()),
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    if etag:
        h["If-Match"] = etag
    return h

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
        time.sleep(2)
    raise TimeoutError(f"Task {desc} timed out after {timeout_secs}s")


def update_linuxtools():
    print("=" * 80)
    print("  UPDATING LINUXTOOLS VM: 8 vCPUs, 12 GiB RAM, 110 GiB BOOT DISK")
    print("=" * 80)

    with httpx.Client(verify=False, timeout=30.0, auth=auth) as client:
        # 1. Discover LinuxTools VM
        r_vms = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms", headers=get_headers()).json().get("data", [])
        vm = next((v for v in r_vms if v.get("name") == "LinuxTools"), None)
        if not vm:
            raise RuntimeError("LinuxTools VM not found in Prism Central!")
        
        vm_id = vm["extId"]
        print(f"Found LinuxTools VM ExtID: {vm_id}")
        print(f"Current Specs: vCPU={vm.get('numSockets')} sockets x {vm.get('numCoresPerSocket')} cores, Memory={vm.get('memorySizeBytes') / (1024**3):.1f} GiB, PowerState={vm.get('powerState')}")

        # Check if we should gracefully power off if needed
        initial_power = vm.get("powerState")
        if initial_power == "ON":
            print("\n[Step 1] Powering off LinuxTools VM for hardware reconfiguration...")
            r_single = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}", headers=get_headers())
            etag = r_single.headers.get("ETag") or r_single.json().get("data", {}).get("$reserved", {}).get("ETag")
            res_pwr = client.post(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}/$actions/power-off", headers=get_headers(etag))
            if res_pwr.status_code in (200, 202):
                t_pwr = res_pwr.json().get("data", {}).get("extId")
                if t_pwr:
                    wait_for_task(client, t_pwr, "Power Off LinuxTools")
                print("  [OK] VM successfully powered off.")

        # 2. Update Boot Disk to 110 GiB
        print(f"\n[Step 2] Updating Boot Disk size to 110 GiB ({TARGET_DISK_BYTES} bytes)...")
        r_single = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}", headers=get_headers())
        vm_data = r_single.json().get("data", {})
        disks = vm_data.get("disks", [])
        boot_disk = disks[0] if disks else None
        
        if boot_disk:
            disk_id = boot_disk["extId"]
            r_d_single = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}/disks/{disk_id}", headers=get_headers())
            disk_etag = r_d_single.headers.get("ETag") or r_d_single.json().get("data", {}).get("$reserved", {}).get("ETag")
            disk_data = r_d_single.json().get("data", {})
            disk_data["backingInfo"]["diskSizeBytes"] = TARGET_DISK_BYTES
            
            res_disk = client.put(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}/disks/{disk_id}", json=disk_data, headers=get_headers(disk_etag))
            if res_disk.status_code in (200, 202):
                t_disk = res_disk.json().get("data", {}).get("extId")
                if t_disk:
                    wait_for_task(client, t_disk, "Resize Boot Disk to 110 GiB")
                print("  [OK] Boot disk resized to 110 GiB successfully.")
            else:
                print(f"  [Warning] Disk resize response: {res_disk.status_code} - {res_disk.text}")

        # 3. Update CPU & Memory (8 vCPUs, 12 GiB RAM)
        print(f"\n[Step 3] Updating CPU (8 vCPUs) and Memory (12 GiB RAM)...")
        r_single = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}", headers=get_headers())
        etag = r_single.headers.get("ETag") or r_single.json().get("data", {}).get("$reserved", {}).get("ETag")
        vm_data = r_single.json().get("data", {})
        
        vm_data["numSockets"] = 8
        vm_data["numCoresPerSocket"] = 1
        vm_data["memorySizeBytes"] = TARGET_MEMORY_BYTES
        
        res_vm = client.put(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}", json=vm_data, headers=get_headers(etag))
        if res_vm.status_code in (200, 202):
            t_vm = res_vm.json().get("data", {}).get("extId")
            if t_vm:
                wait_for_task(client, t_vm, "Update CPU & Memory")
            print("  [OK] CPU and Memory updated successfully.")
        else:
            print(f"  [Warning] VM update response: {res_vm.status_code} - {res_vm.text}")

        # 4. Power VM back ON
        print("\n[Step 4] Powering LinuxTools VM back ON...")
        r_single = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}", headers=get_headers())
        etag = r_single.headers.get("ETag") or r_single.json().get("data", {}).get("$reserved", {}).get("ETag")
        res_on = client.post(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}/$actions/power-on", headers=get_headers(etag))
        if res_on.status_code in (200, 202):
            t_on = res_on.json().get("data", {}).get("extId")
            if t_on:
                wait_for_task(client, t_on, "Power On LinuxTools")
            print("  [OK] VM powered back ON.")

        # 5. Final Verification
        print("\n" + "=" * 80)
        print("  FINAL SPECIFICATION VERIFICATION")
        print("=" * 80)
        r_final = client.get(f"{base_url}/vmm/v4.2/ahv/config/vms/{vm_id}", headers=get_headers()).json().get("data", {})
        final_sockets = r_final.get("numSockets")
        final_cores = r_final.get("numCoresPerSocket")
        final_mem_gb = r_final.get("memorySizeBytes", 0) / (1024**3)
        final_disks = r_final.get("disks", [])
        final_disk_gb = final_disks[0].get("backingInfo", {}).get("diskSizeBytes", 0) / (1024**3) if final_disks else 0
        final_pwr = r_final.get("powerState")
        
        print(f"VM Name:        {r_final.get('name')}")
        print(f"vCPUs:          {final_sockets * final_cores} ({final_sockets} sockets x {final_cores} core)")
        print(f"Memory:         {final_mem_gb:.1f} GiB ({r_final.get('memorySizeBytes')} bytes)")
        print(f"Boot Disk:      {final_disk_gb:.1f} GiB ({final_disks[0].get('backingInfo', {}).get('diskSizeBytes')} bytes)")
        print(f"Power State:    {final_pwr}")
        print("=" * 80)

if __name__ == "__main__":
    update_linuxtools()
