import httpx
import json
from dotenv import load_dotenv
load_dotenv(override=True)
from config import Config

auth = (Config.PC_USERNAME, Config.PC_PASSWORD)
base_url = f"https://{Config.PC_HOST}:{Config.PC_PORT}/api"

with httpx.Client(verify=False, timeout=15.0, auth=auth) as client:
    r = client.get(f"{base_url}/networking/v4.3/config/subnets/5a5ae755-c982-40fb-b67e-8ad5429b9288")
    print(json.dumps(r.json(), indent=2))
