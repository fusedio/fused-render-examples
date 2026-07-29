"""Install the paramiko dependency the VPS Manager daemon needs.

The built-in runPython executor does not read PEP 723 metadata, so the SSH
daemon's interpreter (the one running fused-render) must have paramiko
available. This installs it into that interpreter with pip.

    python install.py

Then open examples/vps_manager/template.html in fused-render.
"""
import subprocess
import sys


def main():
    try:
        import paramiko  # noqa: F401
        print(f"paramiko already installed for {sys.executable}")
        return
    except ImportError:
        pass
    print(f"installing paramiko into {sys.executable} …")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko>=3"])
    print("done — open examples/vps_manager/template.html in fused-render")


if __name__ == "__main__":
    main()
