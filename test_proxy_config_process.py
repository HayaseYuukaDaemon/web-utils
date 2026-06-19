from app import processCNAProxy
from pathlib import Path
import subprocess

CNA_PROFILE_PATH = Path("CNA.yaml")
TEMP_CHECK_FILE = Path('__temp_check__.yaml')

def test_processCNAProxy():
    proceeded_content = processCNAProxy(CNA_PROFILE_PATH.read_text())
    TEMP_CHECK_FILE.write_text(proceeded_content)
    process = subprocess.run([".\\mihomo.exe", '-f', f'.\\{TEMP_CHECK_FILE}', '-t'])
    if process.returncode != 0:
        print("Error occurred while testing the proxy configuration.")
    else:
        print("Proxy configuration is valid.")
    TEMP_CHECK_FILE.unlink()
    
if __name__ == "__main__":
    test_processCNAProxy()
  
