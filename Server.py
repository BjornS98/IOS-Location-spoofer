import threading
import time
import asyncio
from pymobiledevice3.tunneld.server import TunneldRunner
from pymobiledevice3.remote.tunnel_service import TunnelProtocol
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

# Global runner instance!
terminate_location_thread = False  # Global flag to allow exit
tunneld_runner = None

# --- CONFIGURABLE ---
TUNNEL_HOST = "127.0.0.1"
TUNNEL_PORT = 49151
# --------------------

async def update_location_to_first_tunnel(latitude, longitude):
    global tunneld_runner
    tunnels = {}

    # Build the detailed tunnel dict just like in print_all_tunnels
    for ip, active_tunnel in tunneld_runner._tunneld_core.tunnel_tasks.items():
        if (active_tunnel.udid is None) or (active_tunnel.tunnel is None):
            continue
        if active_tunnel.udid not in tunnels:
            tunnels[active_tunnel.udid] = []
        tunnels[active_tunnel.udid].append({
            'tunnel-address': active_tunnel.tunnel.address,
            'tunnel-port': active_tunnel.tunnel.port,
            'interface': ip
        })
    # Find the first available tunnel:
    if not tunnels:
        print("No tunnels available!")
        return

    first_udid = next(iter(tunnels))
    first_tunnel = tunnels[first_udid][0]
    rsd_host = first_tunnel['tunnel-address']
    rsd_port = first_tunnel['tunnel-port']
    print(f"Using tunnel for UDID={first_udid}: host={rsd_host}, port={rsd_port}")

    try:
        async with RemoteServiceDiscoveryService((rsd_host, rsd_port)) as sp_rsd:
            with DvtSecureSocketProxyService(sp_rsd) as dvt:
                LocationSimulation(dvt).set(latitude, longitude)
                print("Location Set Successfully")

                # Keep the session alive until terminated
                while not terminate_location_thread:
                    await asyncio.sleep(0.5)
    except Exception as e:
        print(f"Exception in location updater: {e}")

def start_tunneld_server():
    global tunneld_runner
    tunneld_runner = TunneldRunner(
        TUNNEL_HOST,
        TUNNEL_PORT,
        protocol=TunnelProtocol.TCP,
        usb_monitor=True,
        wifi_monitor=True,
        usbmux_monitor=True,
        mobdev2_monitor=True
    )
    tunneld_runner._run_app()  # starts the FastAPI server (blocking, so run in thread!)

def get_all_tunnels():
    global tunneld_runner
    if tunneld_runner is None:
        raise RuntimeError("TunneldRunner is not started yet!")

    # --- Detailed tunnel info (address, port, interface), matches FastAPI endpoint
    print("\n--- Detailed Tunnels ---")
    tunnels = {}
    for ip, active_tunnel in tunneld_runner._tunneld_core.tunnel_tasks.items():
        if (active_tunnel.udid is None) or (active_tunnel.tunnel is None):
            continue
        if active_tunnel.udid not in tunnels:
            tunnels[active_tunnel.udid] = []
        tunnels[active_tunnel.udid].append({
            'tunnel-address': active_tunnel.tunnel.address,
            'tunnel-port': active_tunnel.tunnel.port,
            'interface': ip
        })
    for udid, tunnel_list in tunnels.items():
        print(f"UDID: {udid}")
        for tunnel in tunnel_list:
            print(f"  Interface: {tunnel['interface']}, Address: {tunnel['tunnel-address']}, Port: {tunnel['tunnel-port']}")

    # --- Simple {UDID: [IP, ...]} view from get_tunnels_ips()
    print("\n--- UDID -> IPs Map ---")
    udid_ip_map = tunneld_runner._tunneld_core.get_tunnels_ips()
    for udid, ip_list in udid_ip_map.items():
        print(f"UDID: {udid}: {ip_list}")

    # Optionally, return these for programmatic use:
    return tunnels, udid_ip_map

async def update_location_over_tunnel(udid, rsd_host, rsd_port, latitude, longitude):
    """
    Update the device location via tunnel.
    Args:
        udid (str): Device UDID (for logging, not required by RSD connection)
        rsd_host (str): Host/IP from tunnel
        rsd_port (int): Port from tunnel
        latitude (float)
        longitude (float)
    """
    print(f"Connecting to UDID={udid} at {rsd_host}:{rsd_port}")
    try:
        async with RemoteServiceDiscoveryService((rsd_host, rsd_port)) as sp_rsd:
            with DvtSecureSocketProxyService(sp_rsd) as dvt:
                LocationSimulation(dvt).set(latitude, longitude)
                print(f"Location set to ({latitude}, {longitude}) for device {udid} via tunnel at {rsd_host}:{rsd_port}")
                while not terminate_location_thread:
                    await asyncio.sleep(0.5)
    except Exception as e:
        print(f"Exception in location updater for UDID={udid}: {e}")

def get_tunnel_for_udid(udid):
    # tunneld_runner is your global TunneldRunner
    for ip, active_tunnel in tunneld_runner._tunneld_core.tunnel_tasks.items():
        if active_tunnel.udid == udid and active_tunnel.tunnel is not None:
            return (udid, active_tunnel.tunnel.address, active_tunnel.tunnel.port)
    raise RuntimeError(f"No tunnel found for UDID: {udid}")

if __name__ == "__main__":
    server_thread = threading.Thread(target=start_tunneld_server, daemon=True)
    server_thread.start()
    print("Tunneld server is starting in background...")

    # Wait for server to be fully ready and enumerate
    time.sleep(10)

    # For demo, just pick the first UDID available
    tunnels_map = tunneld_runner._tunneld_core.get_tunnels_ips()
    if not tunnels_map:
        print("No tunnels available!")
        exit(1)
    first_udid = next(iter(tunnels_map))
    udid, rsd_host, rsd_port = get_tunnel_for_udid(first_udid)

    # Start updater in background thread (so main thread isn't blocked)
    updater_thread = threading.Thread(
        target=lambda: asyncio.run(
            update_location_over_tunnel(udid, rsd_host, rsd_port, 52.370216, 4.895168)
        ),
        daemon=True
    )
    updater_thread.start()
    print("Location updater running...")

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("Terminating location updater...")
        terminate_location_thread = True
        updater_thread.join()
