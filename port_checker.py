"""Network port utility and process diagnostic tool for Race Engineer.

Identifies which process currently owns a conflicting UDP or TCP port, enabling
clear user messages like: 'Port 20777 is in use by F1_25.exe (PID 12345)'.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import NamedTuple, Optional

log = logging.getLogger("engineer.port_checker")


class PortOwner(NamedTuple):
    name: Optional[str]
    pid: Optional[int]


def find_port_owner(port: int, proto: str = "udp") -> PortOwner:
    """Identifies the process name and PID holding the specified port.

    Args:
        port: Port number (e.g. 20777 or 8765)
        proto: Protocol: "udp" or "tcp"

    Returns:
        PortOwner(name, pid) or PortOwner(None, None) if not determinable.
    """
    proto_lower = proto.lower()

    # 1. Primary method: psutil
    try:
        import psutil
        kinds = ["udp4", "udp6"] if proto_lower == "udp" else ["tcp4", "tcp6"]
        for kind in kinds:
            try:
                conns = psutil.net_connections(kind=kind)
            except Exception:
                conns = []
            for conn in conns:
                if conn.laddr and conn.laddr.port == port:
                    pid = conn.pid
                    if pid:
                        try:
                            proc = psutil.Process(pid)
                            return PortOwner(proc.name(), pid)
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            return PortOwner(f"PID {pid}", pid)
    except Exception as e:
        log.debug("psutil port check failed for port %d: %s", port, e)

    # 2. Platform fallback: Windows netstat -ano
    if sys.platform == "win32":
        try:
            proto_flag = "UDP" if proto_lower == "udp" else "TCP"
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", proto_flag],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 4 and f":{port}" in parts[1]:
                    pid_str = parts[-1]
                    if pid_str.isdigit():
                        pid = int(pid_str)
                        name = _get_process_name_by_pid(pid)
                        return PortOwner(name, pid)
        except Exception as e:
            log.debug("netstat fallback failed: %s", e)

    # 3. Platform fallback: macOS / Linux lsof
    else:
        try:
            cmd = ["lsof", "-t", f"-i{proto_lower.upper()}:{port}"]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2.0).strip()
            if out:
                pid_str = out.split()[0]
                if pid_str.isdigit():
                    pid = int(pid_str)
                    name = _get_process_name_by_pid(pid)
                    return PortOwner(name, pid)
        except Exception as e:
            log.debug("lsof fallback failed: %s", e)

    return PortOwner(None, None)


def _get_process_name_by_pid(pid: int) -> str:
    """Resolves process name from PID."""
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        pass

    if sys.platform != "win32":
        try:
            out = subprocess.check_output(["ps", "-p", str(pid), "-o", "comm="], text=True, timeout=1.0).strip()
            if out:
                return os.path.basename(out)
        except Exception:
            pass
    return f"PID {pid}"


def is_port_in_use(port: int, proto: str = "udp") -> bool:
    """Quick check whether a port can be bound."""
    import socket
    sock_type = socket.SOCK_DGRAM if proto.lower() == "udp" else socket.SOCK_STREAM
    s = socket.socket(socket.AF_INET, sock_type)
    try:
        s.bind(("0.0.0.0", port))
        s.close()
        return False
    except OSError:
        return True


def format_port_conflict_message(port: int, proto: str = "udp") -> str:
    """Formats human-readable port conflict diagnostic."""
    name, pid = find_port_owner(port, proto=proto)
    if name and pid:
        return f"Port {port} is in use by {name} (PID {pid})"
    elif pid:
        return f"Port {port} is in use by PID {pid}"
    else:
        return f"Port {port} is in use by another process"
