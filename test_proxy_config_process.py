from app import processCNAProxy
from pathlib import Path
import subprocess
import os

if os.name == 'nt':
    LOCAL_MIHOMO_PATH = Path('.\\mihomo.exe')
else:
    LOCAL_MIHOMO_PATH = Path('./mihomo')

CNA_PROFILE_PATH = Path("CNA.yaml")
TEMP_CHECK_FILE = Path('patched_CNA.yaml')

def test_processCNAProxy():
    proceeded_content = processCNAProxy(CNA_PROFILE_PATH.read_text())
    TEMP_CHECK_FILE.write_text(proceeded_content)
    process = subprocess.run(['./mihomo', '-f', str(TEMP_CHECK_FILE), '-t'])
    if process.returncode != 0:
        print("Error occurred while testing the proxy configuration.")
    else:
        print("Proxy configuration is valid.")
    
if __name__ == "__main__":
    test_processCNAProxy()
  
