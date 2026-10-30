"""Install a built bundle into a clean environment and confirm its demo serves."""
import argparse
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import venv
import zipfile


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def fetch(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return response.status, response.read()


def verify(bundle):
    with tempfile.TemporaryDirectory(prefix="home-monitor-release-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(root)
        source = next(root.iterdir())
        environment = root / "environment"
        venv.create(environment, with_pip=True)
        python = environment / "bin/python"
        subprocess.run([python, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=source, check=True)
        port = free_port()
        demo = subprocess.Popen([python, "scripts/demo.py", "--port", str(port)], cwd=source, stdout=subprocess.DEVNULL)
        try:
            for _ in range(120):
                if demo.poll() is not None:
                    raise RuntimeError("The demo stopped during startup")
                try:
                    status, body = fetch(f"http://127.0.0.1:{port}/api/status")
                    break
                except (urllib.error.URLError, ConnectionError, TimeoutError):
                    time.sleep(0.5)
            else:
                raise RuntimeError("The demo did not start")
            page = fetch(f"http://127.0.0.1:{port}/")[1]
            if status != 200 or b'"cameras"' not in body or b"Home Monitor" not in page:
                raise RuntimeError("The demo did not serve the application")
        finally:
            demo.terminate()
            demo.wait(timeout=20)
    print(f"Verified {Path(bundle).name} in a clean environment.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    verify(parser.parse_args().bundle)


if __name__ == "__main__":
    main()
