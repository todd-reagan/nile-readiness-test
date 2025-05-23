#!/usr/bin/env python3
"""
nrt.py - Configures host interface, OSPF adjacency dynamically,
loopback interfaces, default route fallback, connectivity tests (DHCP/RADIUS,
NTP, HTTPS), then restores host to original state (including removing FRR config,
stopping FRR) and DNS changes.

This script runs in the default namespace and uses the specified interface for FRR tests,
allowing VNC to run in a separate namespace on another interface.

Usage: 
  sudo ./nrt.py [--debug] [--config CONFIG_FILE]
  
Options:
  --debug           Enable debug output
  --config FILE     Use JSON configuration file instead of interactive prompts
"""

import os
import threading
import sys
import shutil
import subprocess
import random
import ipaddress
import socket
import time
import json
import argparse
import re
from urllib.parse import urlparse
from scapy.config import conf
from scapy.all import sniff, sr1, send, Raw
from scapy.layers.inet import IP, UDP
from scapy.contrib.ospf import OSPF_Hdr, OSPF_Hello
from scapy.layers.l2 import Ether
from scapy.layers.dhcp import BOOTP, DHCP

# Import dhcppython for improved DHCP testing
import dhcppython.client as dhcp_client
import dhcppython.options as dhcp_options
import dhcppython.utils as dhcp_utils

# Constants for Nile Connect tests
NILE_HOSTNAME = "ne-u1.nile-global.cloud"
S3_HOSTNAME = "s3.us-west-2.amazonaws.com"
GUEST_IPS = ["145.40.90.203","145.40.64.129","145.40.113.105","147.28.179.61"]
UDP_PORT = 6081
SSL_PORT = 443

# Check UDP connectivity using netcat
def check_udp_connectivity_netcat(ip: str, port: int = UDP_PORT, timeout: int = 5) -> bool:
    """
    Check UDP connectivity using netcat (nc -vzu) with a timeout.
    
    Args:
        ip: IP address to check
        port: UDP port to check (default: 6081)
        timeout: Timeout in seconds (default: 5)
        
    Returns:
        bool: True if connectivity successful, False otherwise
    """
    try:
        result = subprocess.run(
            ['nc', '-vzu', ip, str(port)],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        # Check for success indicators in output
        if "open" in result.stderr.lower():
            return True
            
        if result.returncode == 0:
            return True
            
        return False
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        print(f"  Error: netcat (nc) command not found. Please install netcat.")
        return False
    except Exception as e:
        print(f"  Error: {e}")
        return False

# Check SSL certificate
def check_ssl_certificate(ip: str, hostname: str, expected_org: str) -> bool:
    """
    Test SSL certificate validity and organization.
    
    Args:
        ip: IP address to check
        hostname: Hostname for SNI
        expected_org: Expected organization in certificate issuer
        
    Returns:
        bool: True if SSL certificate is valid and contains expected organization, False otherwise
    """
    try:
        result = subprocess.run(
            ['openssl', 's_client', '-connect', f'{ip}:{SSL_PORT}', '-servername', hostname],
            capture_output=True,
            text=True
        )
        
        if "issuer=" in result.stdout:
            issuer_start = result.stdout.find("issuer=")
            issuer_end = result.stdout.find("\n", issuer_start)
            issuer = result.stdout[issuer_start:issuer_end].strip()
            
            # Check if issuer contains the expected organization
            if expected_org not in issuer:
                return False
                
            return True
        else:
            return False
    except Exception as e:
        print(f"  Error: {e}")
        return False



# Parse command line arguments
def parse_args():
    """
    Parse command line arguments for the Nile Readiness Test.
    
    Returns:
        argparse.Namespace: Parsed command line arguments
    """
    parser = argparse.ArgumentParser(description='Nile Readiness Test')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--config', type=str, help='Path to JSON configuration file')
    return parser.parse_args()

# Read configuration from JSON file
def read_config(config_file):
    """
    Read and parse the JSON configuration file.
    
    Args:
        config_file: Path to the JSON configuration file
        
    Returns:
        dict: Parsed configuration data
        
    Raises:
        SystemExit: If the file cannot be read or parsed
    """
    try:
        with open(config_file, 'r') as f:
            config = json.load(f)
        print(f"Loaded configuration from {config_file}")
        return config
    except Exception as e:
        print(f"Error reading config file {config_file}: {e}")
        sys.exit(1)

# Parse arguments
args = parse_args()
DEBUG = args.debug

# ANSI color codes
GREEN = '\033[32m'
RED   = '\033[31m'
RESET = '\033[0m'

# Pre-flight checks
required_bins = {
    'vtysh': 'FRR (vtysh)',
    'radclient': 'FreeRADIUS client (radclient)',
    'dig': 'DNS lookup utility (dig)',
    'ntpdate': 'NTP utility (ntpdate)',
    'curl': 'HTTPS test utility (curl)',
    'nc': 'Netcat (nc) for UDP connectivity testing',
    'openssl': 'OpenSSL for SSL certificate verification'
}
missing = [name for name in required_bins if shutil.which(name) is None]
if missing:
    print('Error: the following required tools are missing:')
    for name in missing:
        print(f'  - {required_bins[name]}')
    print()
    print('Please install them, e.g.:')
    print('  sudo apt update && sudo apt install frr freeradius-client dnsutils ntpdate curl netcat-openbsd openssl')
    sys.exit(1)

# Wrapper for subprocess.run with debug
def run_cmd(cmd, **kwargs):
    """
    Execute a command with improved handling of output capture and debugging.
    
    This function wraps subprocess.run with additional features:
    - Debug output of commands being executed
    - Improved handling of stdout/stderr to avoid buffer deadlocks
    - Consistent error handling
    
    Args:
        cmd: Command to execute (list or string)
        **kwargs: Additional arguments to pass to subprocess.run
        
    Returns:
        subprocess.CompletedProcess: Result of the command execution
    """
    if DEBUG:
        printed = cmd if isinstance(cmd, str) else ' '.join(cmd)
        print(f'DEBUG: Running: {printed} | kwargs={kwargs}')
    
    # Use Popen instead of subprocess.run to avoid buffer deadlocks
    if kwargs.get('capture_output'):
        # Create pipes for stdout and stderr
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=kwargs.get('text', False),
            shell=kwargs.get('shell', False)
        )
        
        # Read stdout and stderr incrementally to avoid buffer deadlocks
        stdout_data = []
        stderr_data = []
        
        while True:
            # Read from stdout and stderr without blocking
            stdout_chunk = process.stdout.read(1024)
            stderr_chunk = process.stderr.read(1024)
            
            # If we got data, store it
            if stdout_chunk:
                stdout_data.append(stdout_chunk)
            if stderr_chunk:
                stderr_data.append(stderr_chunk)
            
            # Check if process has finished
            if process.poll() is not None:
                # Read any remaining data
                stdout_chunk = process.stdout.read()
                stderr_chunk = process.stderr.read()
                if stdout_chunk:
                    stdout_data.append(stdout_chunk)
                if stderr_chunk:
                    stderr_data.append(stderr_chunk)
                break
            
            # Small sleep to avoid CPU spinning
            time.sleep(0.01)
        
        # Join the data
        stdout_output = ''.join(stdout_data) if kwargs.get('text', False) else b''.join(stdout_data)
        stderr_output = ''.join(stderr_data) if kwargs.get('text', False) else b''.join(stderr_data)
        
        # Create a CompletedProcess object to match subprocess.run's return value
        proc = subprocess.CompletedProcess(
            args=cmd,
            returncode=process.returncode,
            stdout=stdout_output,
            stderr=stderr_output
        )
        
        if DEBUG:
            print('DEBUG: stdout:')
            print(proc.stdout)
            print('DEBUG: stderr:')
            print(proc.stderr)
    else:
        # If we're not capturing output, just use subprocess.run
        proc = subprocess.run(cmd, **kwargs)
    
    if kwargs.get('check') and proc.returncode != 0:
        if DEBUG:
            print(f'DEBUG: Command failed with return code {proc.returncode}')
        proc.check_returncode()
    
    return proc

# Prompt helper
def prompt_nonempty(prompt):
    """
    Prompt the user for input and ensure a non-empty response.
    
    Args:
        prompt: The prompt text to display to the user
        
    Returns:
        str: The user's non-empty input
    """
    while True:
        val = input(prompt).strip()
        if val:
            return val
        print('  -> This value cannot be blank.')

# Gather user input
def get_user_input(config_file=None):
    """
    Gather network configuration input from either a config file or interactive prompts.
    
    This function handles both configuration file parsing and interactive user input
    for setting up network interfaces, subnets, and test parameters.
    
    Args:
        config_file: Optional path to a JSON configuration file
        
    Returns:
        tuple: A tuple containing all configuration parameters:
            (test_iface, ip_addr, netmask, gateway, mgmt_interface,
            mgmt1, mgmt2, client_subnet,
            dhcp_servers, radius_servers, secret, username, password,
            run_dhcp, run_radius, custom_dns_servers, custom_ntp_servers)
    """
    # If config file is provided, use it
    if config_file:
        config = read_config(config_file)
        test_iface = config.get('test_interface', 'enxf0a731f41761')
        ip_addr = config.get('ip_address')
        netmask = config.get('netmask')
        gateway = config.get('gateway')
        mgmt_interface = config.get('mgmt_interface', 'end0')
        mgmt1 = config.get('nsb_subnet')
        mgmt2 = config.get('sensor_subnet')
        client_subnet = config.get('client_subnet')
        run_dhcp = config.get('run_dhcp_tests', False)
        dhcp_servers = config.get('dhcp_servers', [])
        run_radius = config.get('run_radius_tests', False)
        radius_servers = config.get('radius_servers', [])
        secret = config.get('radius_secret')
        username = config.get('radius_username')
        password = config.get('radius_password')
        run_custom_dns_tests = config.get('run_custom_dns_tests', False)
        custom_dns_servers = config.get('custom_dns_servers', []) if run_custom_dns_tests else []
        run_custom_ntp_tests = config.get('run_custom_ntp_tests', False)
        custom_ntp_servers = config.get('custom_ntp_servers', []) if run_custom_ntp_tests else []
        
        # Validate required fields
        missing = []
        for field, value in [
            ('ip_address', ip_addr),
            ('netmask', netmask),
            ('gateway', gateway),
            ('nsb_subnet', mgmt1),
            ('sensor_subnet', mgmt2),
            ('client_subnet', client_subnet)
        ]:
            if not value:
                missing.append(field)
        
        if missing:
            print(f"Error: Missing required fields in config file: {', '.join(missing)}")
            sys.exit(1)
            
        # Validate RADIUS fields if RADIUS tests are enabled
        if run_radius:
            missing = []
            for field, value in [
                ('radius_servers', radius_servers),
                ('radius_secret', secret),
                ('radius_username', username),
                ('radius_password', password)
            ]:
                if not value:
                    missing.append(field)
            
            if missing:
                print(f"Error: RADIUS tests enabled but missing fields: {', '.join(missing)}")
                sys.exit(1)
        
        # Validate DHCP fields if DHCP tests are enabled
        if run_dhcp and not dhcp_servers:
            print("Error: DHCP tests enabled but no DHCP servers specified")
            sys.exit(1)
            
        print("\nUsing configuration from file:")
        print(f"  Management Interface: {mgmt_interface}")
        print(f"  NSB Testing Interface: {test_iface}")
        print(f"  IP Address: {ip_addr}")
        print(f"  Netmask: {netmask}")
        print(f"  Gateway: {gateway}")
        print(f"  NSB Subnet: {mgmt1}")
        print(f"  Sensor Subnet: {mgmt2}")
        print(f"  Client Subnet: {client_subnet}")
        print(f"  Run DHCP Tests: {run_dhcp}")
        if run_dhcp:
            print(f"  DHCP Servers: {', '.join(dhcp_servers)}")
        print(f"  Run RADIUS Tests: {run_radius}")
        if run_radius:
            print(f"  RADIUS Servers: {', '.join(radius_servers)}")
        print(f"  Run Custom DNS Tests: {run_custom_dns_tests}")
        if run_custom_dns_tests:
            print(f"  Custom DNS Servers: {', '.join(custom_dns_servers)}")
        print(f"  Run Custom NTP Tests: {run_custom_ntp_tests}")
        if run_custom_ntp_tests:
            print(f"  Custom NTP Servers: {', '.join(custom_ntp_servers)}")
    else:
        # Interactive mode
        print("\nNetwork Interface Configuration:")
        print("--------------------------------")
        mgmt_interface = prompt_nonempty('Management interface of host to keep enabled (default: end0): ') or 'end0'
        test_iface     = prompt_nonempty('Interface for Nile Readiness tests (default: enxf0a731f41761): ') or 'enxf0a731f41761'
        ip_addr       = prompt_nonempty('IP address for NSB Gateway interface: ')
        netmask       = prompt_nonempty('Netmask (e.g. 255.255.255.0): ')
        gateway       = prompt_nonempty('Router or Firewall IP: ')
        mgmt1         = prompt_nonempty('NSB subnet (CIDR, e.g. 192.168.1.0/24): ')
        mgmt2         = prompt_nonempty('Sensor subnet (CIDR): ')
        client_subnet = prompt_nonempty('Client subnet (CIDR): ')

        run_dhcp = input('Perform DHCP tests? [y/N]: ').strip().lower().startswith('y')
        dhcp_servers = []
        if run_dhcp:
            dhcp_servers = [ip.strip() for ip in prompt_nonempty(
                'DHCP server IP(s) (comma-separated): ').split(',')]

        run_radius = input('Perform RADIUS tests? [y/N]: ').strip().lower().startswith('y')
        radius_servers = []
        secret = username = password = None
        if run_radius:
            radius_servers = [ip.strip() for ip in prompt_nonempty(
                'RADIUS server IP(s) (comma-separated): ').split(',')]
            secret   = prompt_nonempty('RADIUS shared secret: ')
            username = prompt_nonempty('RADIUS test username: ')
            password = prompt_nonempty('RADIUS test password: ')

    # For interactive mode, custom_dns_servers and custom_ntp_servers are empty lists
    if not config_file:
        custom_dns_servers = []
        custom_ntp_servers = []
        
        # Ask for custom DNS servers
        custom_dns = input('Add custom DNS servers for testing? [y/N]: ').strip().lower()
        if custom_dns.startswith('y'):
            custom_dns_input = prompt_nonempty('Enter custom DNS server IP(s) (comma-separated): ')
            custom_dns_servers = [ip.strip() for ip in custom_dns_input.split(',')]
            
        # Ask for custom NTP servers
        custom_ntp = input('Add custom NTP servers for testing? [y/N]: ').strip().lower()
        if custom_ntp.startswith('y'):
            custom_ntp_input = prompt_nonempty('Enter custom NTP server(s) (comma-separated): ')
            custom_ntp_servers = [server.strip() for server in custom_ntp_input.split(',')]
    
    # Print custom DNS and NTP servers if provided
    if custom_dns_servers:
        print(f"  Custom DNS Servers: {', '.join(custom_dns_servers)}")
    if custom_ntp_servers:
        print(f"  Custom NTP Servers: {', '.join(custom_ntp_servers)}")
    
    return (test_iface, ip_addr, netmask, gateway, mgmt_interface,
            mgmt1, mgmt2, client_subnet,
            dhcp_servers, radius_servers, secret, username, password,
            run_dhcp, run_radius, custom_dns_servers, custom_ntp_servers)

# Record/restore host state
def record_state(iface):
    """
    Record the current state of a network interface and system configuration.
    
    This function captures the current state of the specified interface,
    including IP addresses, routes, FRR configuration, and DNS settings,
    so they can be restored later.
    
    Args:
        iface: The network interface to record state for
        
    Returns:
        dict: A dictionary containing the recorded state information
    """
    if DEBUG:
        print(f"Recording state of interface {iface}...")
    state = {}
    out = run_cmd(['ip','addr','show','dev',iface], capture_output=True, text=True).stdout
    state['addrs']  = [l.split()[1] for l in out.splitlines() if 'inet ' in l]
    state['routes'] = run_cmd(['ip','route','show','default'], capture_output=True, text=True).stdout.splitlines()
    with open('/etc/frr/daemons') as f: state['daemons'] = f.read()
    with open('/etc/resolv.conf') as f: state['resolv'] = f.read()
    svc = run_cmd(['systemctl','is-enabled','frr'], capture_output=True, text=True)
    state['frr_enabled'] = (svc.returncode == 0)
    return state

def restore_state(iface, state):
    """
    Restore the network interface and system configuration to its original state.
    
    This function reverses the changes made during testing by:
    - Removing dummy interfaces
    - Restoring original IP addresses
    - Restoring original routes
    - Restoring FRR configuration
    - Restoring DNS settings
    - Stopping and disabling FRR service
    
    Args:
        iface: The network interface to restore
        state: The state dictionary created by record_state()
    """
    if DEBUG:
        print('\nRestoring original state...')
    
    # First, remove dummy interfaces
    if DEBUG:
        print("Removing dummy interfaces...")
    for name in ('mgmt1','mgmt2','client'):
        run_cmd(['ip','link','delete',f'dummy_{name}'], check=False, capture_output=True)
    
    # Flush the interface
    if DEBUG:
        print(f"Flushing interface {iface}...")
    run_cmd(['ip','addr','flush','dev',iface], check=True, capture_output=True)
    
    # Apply a temporary IP configuration if there are no addresses in the state
    # This helps with the "Nexthop has invalid gateway" error
    # Use 0.0.0.0/0 without setting a default gateway to avoid adding additional routes
    if not state['addrs']:
        if DEBUG:
            print("No original addresses found, applying temporary IP configuration...")
        run_cmd(['ip','addr','add','0.0.0.0/0','dev',iface], check=False, capture_output=True)
        run_cmd(['ip','link','set','dev',iface,'up'], check=False, capture_output=True)
    
    # Add back the original addresses
    if DEBUG:
        print("Restoring original IP addresses...")
    for addr in state['addrs']:
        run_cmd(['ip','addr','add',addr,'dev',iface], check=False, capture_output=True)
    
    # Make sure the interface is up
    if DEBUG:
        print(f"Ensuring interface {iface} is up...")
    run_cmd(['ip','link','set','dev',iface,'up'], check=False, capture_output=True)
    
    # Flush default routes
    if DEBUG:
        print("Flushing default routes...")
    run_cmd(['ip','route','flush','default'], check=False, capture_output=True)
    
    # Restore FRR configuration
    if DEBUG:
        print("Restoring FRR configuration...")
    with open('/etc/frr/daemons','w') as f: f.write(state['daemons'])
    run_cmd(['rm','-f','/etc/frr/frr.conf'], check=False, capture_output=True)
    
    # Restore DNS configuration
    if DEBUG:
        print("Restoring DNS configuration...")
    with open('/etc/resolv.conf','w') as f: f.write(state['resolv'])
    
    # Stop and disable FRR
    if DEBUG:
        print("Stopping and disabling FRR...")
    run_cmd(['systemctl','stop','frr'], check=False, capture_output=True)
    run_cmd(['systemctl','disable','frr'], check=False, capture_output=True)
    
    if DEBUG:
        print('Removed FRR config, stopped service, restored DNS.')

# Configure main interface
def configure_interface(iface, ip_addr, netmask, mgmt_interface='end0'):
    """
    Configure the main network interface for testing.
    
    This function:
    - Disables all interfaces except loopback and management interface
    - Configures the specified interface with the given IP address and netmask
    - Ensures the interface is up and properly configured
    
    Args:
        iface: The network interface to configure
        ip_addr: The IP address to assign to the interface
        netmask: The netmask in dotted decimal notation (e.g., 255.255.255.0)
        mgmt_interface: The management interface to keep enabled (default: end0)
        
    Returns:
        bool: True if the interface was successfully configured, False otherwise
    """

    if DEBUG:
        print(f'Configuring {iface} → {ip_addr}/{netmask}')
    
    # Get a list of all network interfaces
    if DEBUG:
        print("Getting list of network interfaces...")
    interfaces_output = run_cmd(['ip', 'link', 'show'], capture_output=True, text=True).stdout
    interfaces = []
    for line in interfaces_output.splitlines():
        if ': ' in line:
            # Extract interface name (remove number and colon)
            interface_name = line.split(': ')[1].split('@')[0]
            interfaces.append(interface_name)
    
    # Check for and clean up dummy interfaces from previous runs
    if DEBUG:
        print("Checking for dummy interfaces from previous runs...")
    dummy_interfaces = ['dummy_mgmt1', 'dummy_mgmt2', 'dummy_client']
    for dummy in dummy_interfaces:
        if dummy in interfaces:
            if DEBUG:
                print(f"Removing leftover dummy interface {dummy}...")
            run_cmd(['ip', 'link', 'delete', dummy], check=False)
    
    # Check if management interface has a default gateway and remove it
    if mgmt_interface in interfaces and mgmt_interface != iface:
        if DEBUG:
            print(f"Checking if management interface {mgmt_interface} has a default gateway...")
        route_output = run_cmd(['ip', 'route', 'show', 'dev', mgmt_interface], capture_output=True, text=True).stdout
        if 'default' in route_output:
            if DEBUG:
                print(f"Removing default gateway from management interface {mgmt_interface}...")
            run_cmd(['ip', 'route', 'del', 'default', 'dev', mgmt_interface], check=False)
            if DEBUG:
                print(f"Default gateway removed from {mgmt_interface}")
    
    # Disable all interfaces except loopback and management interface
    if DEBUG:
        print(f"Disabling all interfaces except loopback and {mgmt_interface} (management interface)...")
    interfaces_to_disable = [interface for interface in interfaces 
                            if interface != 'lo' and interface != mgmt_interface and not interface.startswith('dummy_')]
    
    # First attempt to disable all interfaces
    for interface in interfaces_to_disable:
        if DEBUG:
            print(f"Disabling interface {interface}...")
        run_cmd(['ip', 'link', 'set', 'dev', interface, 'down'], check=False)
    
    # Verify interfaces are actually down and retry if needed
    if DEBUG:
        print("Verifying interfaces are down...")
    max_attempts = 3
    for attempt in range(max_attempts):
        all_down = True
        interfaces_output = run_cmd(['ip', 'link', 'show'], capture_output=True, text=True).stdout
        for interface in interfaces_to_disable:
            # Check if interface is still up
            if f"{interface}: " in interfaces_output and "state UP" in interfaces_output.split(f"{interface}: ")[1].split("\n")[0]:
                if DEBUG:
                    print(f"Interface {interface} is still up, retrying...")
                run_cmd(['ip', 'link', 'set', 'dev', interface, 'down'], check=False)
                all_down = False
        
        if all_down:
            if DEBUG:
                print("All interfaces successfully disabled.")
            break
        
        if attempt < max_attempts - 1:
            if DEBUG:
                print(f"Some interfaces still up. Waiting before retry {attempt+1}/{max_attempts}...")
            time.sleep(2)
    
    # Configure the specified interface
    if DEBUG:
        print(f'Configuring {iface}...')
    prefix = ipaddress.IPv4Network(f'0.0.0.0/{netmask}').prefixlen
    
    # Then flush and configure
    if DEBUG:
        print(f"Flushing and configuring {iface}...")
    run_cmd(['ip', 'addr', 'flush', 'dev', iface], check=True)
    run_cmd(['ip', 'addr', 'add', f'{ip_addr}/{prefix}', 'dev', iface], check=True)
    
    # Finally enable the interface
    if DEBUG:
        print(f"Enabling {iface}...")
    run_cmd(['ip', 'link', 'set', 'dev', iface, 'up'], check=True)
    
    # Ensure the interface is up and properly configured with retry logic
    if DEBUG:
        print(f"\nEnsuring interface {iface} is up and properly configured...")
    max_retries = 5
    retry_delay = 2
    interface_up = False
    
    for attempt in range(max_retries):
        # Check if interface is up
        iface_status = run_cmd(['ip', 'link', 'show', 'dev', iface], capture_output=True, text=True).stdout
        iface_addr = run_cmd(['ip', 'addr', 'show', 'dev', iface], capture_output=True, text=True).stdout
        
        if "state UP" in iface_status and f"inet {ip_addr}" in iface_addr:
            if DEBUG:
                print(f"Interface {iface} is up and properly configured with IP {ip_addr}")
            interface_up = True
            break
        else:
            if DEBUG:
                print(f"Attempt {attempt+1}/{max_retries}: Interface {iface} is not properly configured")
            if "state UP" not in iface_status:
                if DEBUG:
                    print(f"  - Interface is not up, bringing it up...")
                run_cmd(['ip', 'link', 'set', 'dev', iface, 'up'], check=True)
            
            if f"inet {ip_addr}" not in iface_addr:
                if DEBUG:
                    print(f"  - IP address {ip_addr} not configured, reconfiguring...")
                run_cmd(['ip', 'addr', 'flush', 'dev', iface], check=False)
                run_cmd(['ip', 'addr', 'add', f'{ip_addr}/{prefix}', 'dev', iface], check=True)
            
            if DEBUG:
                print(f"  - Waiting {retry_delay} seconds before checking again...")
            time.sleep(retry_delay)
    
    if not interface_up:
        print(f"{RED}ERROR: Failed to bring up interface {iface} after {max_retries} attempts.{RESET}")
        print("This will likely cause subsequent tests to fail.")
        print("Continuing anyway, but expect failures...")
    
    return interface_up

# Add loopbacks
def add_loopbacks(m1,m2,client):
    """
    Create dummy loopback interfaces for each subnet.
    
    This function creates three dummy interfaces:
    - dummy_mgmt1: For the NSB subnet
    - dummy_mgmt2: For the sensor subnet
    - dummy_client: For the client subnet
    
    Each interface is assigned the first IP address in its respective subnet.
    
    Args:
        m1: NSB subnet in CIDR notation
        m2: Sensor subnet in CIDR notation
        client: Client subnet in CIDR notation
    """
    for name,subnet in [('mgmt1',m1),('mgmt2',m2),('client',client)]:
        net = ipaddress.IPv4Network(subnet)
        iface = f'dummy_{name}'
        addr = str(net.network_address+1)
        prefix = net.prefixlen
        run_cmd(['ip','link','add',iface,'type','dummy'], check=False)
        run_cmd(['ip','addr','add',f'{addr}/{prefix}','dev',iface], check=False)
        run_cmd(['ip','link','set','dev',iface,'up'], check=True)
        if DEBUG:
            print(f'Loopback {iface} → {addr}/{prefix}')

# OSPF Hello sniff
def sniff_ospf_hello(iface, timeout=60):
    """
    Sniff for OSPF Hello packets on the specified interface.
    
    This function waits for an OSPF Hello packet on the specified interface
    and extracts the source IP, area, hello interval, and dead interval.
    
    Args:
        iface: The network interface to sniff on
        timeout: Maximum time to wait for an OSPF Hello packet in seconds (default: 60)
        
    Returns:
        tuple: (source_ip, area, hello_interval, dead_interval)
        
    Raises:
        SystemExit: If no OSPF Hello packet is received within the timeout
    """
    print(f'\nWaiting for OSPF Hello on {iface}...')
    
    # Use scapy to sniff for OSPF Hello packets
    pkts = sniff(iface=iface, filter='ip proto 89', timeout=timeout, count=1)
    if not pkts:
        print('No OSPF Hello received; aborting.')
        sys.exit(1)
    
    pkt = pkts[0]
    src = pkt[IP].src
    area = pkt[OSPF_Hdr].area
    hi = pkt[OSPF_Hello].hellointerval
    di = pkt[OSPF_Hello].deadinterval
    
    print(f"Received OSPF Hello from {src} (Area: {area}, Hello: {hi}s, Dead: {di}s)")
    
    # Don't cast area to int as it can be in dotted notation (e.g., 0.0.0.0)
    return src, area, int(hi), int(di)

# Configure OSPF - using vtysh commands directly like the original
def configure_ospf(iface, ip, prefix, m1, m2, client, up, area, hi, di):
    """
    Configure OSPF routing using FRR.
    
    This function:
    - Enables the OSPF daemon in FRR
    - Configures OSPF networks and parameters
    - Verifies the interface is up and properly configured
    - Tests connectivity to the upstream router
    
    Args:
        iface: The network interface to configure OSPF on
        ip: The IP address of the interface
        prefix: The network prefix length
        m1: NSB subnet in CIDR notation
        m2: Sensor subnet in CIDR notation
        client: Client subnet in CIDR notation
        up: Upstream router IP address
        area: OSPF area
        hi: Hello interval in seconds
        di: Dead interval in seconds
        
    Raises:
        SystemExit: If connectivity to the upstream router cannot be established
    """
    n1=ipaddress.IPv4Network(m1);n2=ipaddress.IPv4Network(m2);n3=ipaddress.IPv4Network(client)
    
    if DEBUG:
        print(f"Configuring FRR and OSPF for interface {iface}")
    
    # Enable ospfd in daemons file
    lines=open('/etc/frr/daemons').read().splitlines()
    with open('/etc/frr/daemons','w') as f:
        for l in lines: f.write('ospfd=yes\n' if l.startswith('ospfd=') else l+'\n')
    
    # Restart FRR
    if DEBUG:
        print("Restarting FRR...")
    run_cmd(['systemctl', 'restart', 'frr'], check=True)
    
    # Wait for FRR to start
    if DEBUG:
        print("Waiting for FRR to start...")
    time.sleep(5)
    
    # Configure OSPF using vtysh commands
    cmds=[
        'vtysh','-c','configure terminal','-c','router ospf',
        '-c',f'network {ip}/{prefix} area {area}',
        '-c',f'network {n1.network_address}/{n1.prefixlen} area {area}',
        '-c',f'network {n2.network_address}/{n2.prefixlen} area {area}',
        '-c',f'network {n3.network_address}/{n3.prefixlen} area {area}',
        '-c','exit','-c',f'interface {iface}',
        '-c',f'ip ospf hello-interval {hi}',
        '-c',f'ip ospf dead-interval {di}',
        '-c','exit','-c','end','-c','write memory'
    ]
    run_cmd(cmds, check=True, capture_output=True)
    
    # Verify interface is up and properly configured
    if DEBUG:    
        print(f"Verifying interface {iface} is up and properly configured...")
    iface_status = run_cmd(['ip', 'link', 'show', 'dev', iface], capture_output=True, text=True).stdout

    if "state UP" not in iface_status:
        if DEBUG:
            print(f"Interface {iface} is not up. Attempting to bring it up...")
        run_cmd(['ip', 'link', 'set', 'dev', iface, 'up'], check=True)
        # Wait for interface to come up
        time.sleep(2)
        # Check again
        iface_status = run_cmd(['ip', 'link', 'show', 'dev', iface], capture_output=True, text=True).stdout
        if "state UP" not in iface_status:
            print(f"WARNING: Interface {iface} could not be brought up. OSPF may not work correctly.")
    
    # Verify IP address is configured
    iface_addr = run_cmd(['ip', 'addr', 'show', 'dev', iface], capture_output=True, text=True).stdout
    if f"inet {ip}" not in iface_addr:
        if DEBUG:
            print(f"IP address {ip} not found on interface {iface}. Reconfiguring...")
        run_cmd(['ip', 'addr', 'flush', 'dev', iface], check=False)
        run_cmd(['ip', 'addr', 'add', f'{ip}/{prefix}', 'dev', iface], check=True)
    
    # Show the routing table
    route_output = run_cmd(['ip', 'route'], capture_output=True, text=True).stdout
    if DEBUG:
        print("Current routing table:")
        print(route_output)
    
    # Check connectivity to the upstream router with retry logic
    print(f"Testing connectivity to upstream router {up}")
    max_retries = 3
    retry_delay = 5
    router_reachable = False
    
    for attempt in range(max_retries):
        ping_result = run_cmd(['ping', '-c', '4', up], capture_output=True)
        if ping_result.returncode == 0:
            router_reachable = True
            print(f"Connectivity to upstream router {up}: {GREEN}Success{RESET}")
            break
        else:
            print(f"Attempt {attempt+1}/{max_retries}: Connectivity to upstream router {up}: {RED}Fail{RESET}")
            if attempt < max_retries - 1:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
    
    if not router_reachable:
        print(f"{RED}ERROR: Failed to reach upstream router {up} after {max_retries} attempts.{RESET}")
        print(f"{RED}There is no reason to continue tests without upstream router connectivity.{RESET}")
        print(f"{RED}Terminating tests...{RESET}")
        sys.exit(1)
    
    print("OSPF configuration complete")

# Show OSPF state Full/DR - using the original approach with active waiting
def show_ospf_status():
    """
    Wait for and verify OSPF adjacency reaches Full/DR state.
    
    This function:
    - Polls the OSPF neighbor state every second for up to 30 seconds
    - Checks for Full/DR state in the OSPF neighbor output
    - Displays routing tables for debugging purposes
    
    Returns:
        bool: True if OSPF reached Full/DR state, False otherwise
    """
    print('\n=== Waiting for OSPF state Full/DR (30s timeout) ===')
    success = False
    for _ in range(30):
        out = run_cmd(['vtysh','-c','show ip ospf neighbor'], capture_output=True, text=True).stdout
        if any('Full/DR' in l for l in out.splitlines()[1:]):
            success = True
            print('OSPF reached Full/DR state')
            break
        time.sleep(1)
    if not success:
        print('OSPF never reached Full/DR state after 30s')
    
    # Wait a bit more to ensure stability
    time.sleep(5)
    
    # Show the routing table from FRR
    frr_routes = run_cmd(['vtysh', '-c', 'show ip route'], capture_output=True, text=True).stdout
    if DEBUG:
        print("\n=== FRR Routing Table ===")
        print(frr_routes)
    
    # Show the kernel routing table
    route_output = run_cmd(['ip', 'route'], capture_output=True, text=True).stdout
    if DEBUG:
        print(f'\n=== Kernel Routing Table ===')
        print(route_output)
    
    return success

# Add floating static default
def configure_static_route(gateway, iface):
    """
    Configure a static default route and verify it was added successfully.
    
    Args:
        gateway: Gateway IP address
        iface: Interface name
        
    Returns:
        bool: True if route was added successfully, False otherwise
    """
    if DEBUG:
        print(f"\nConfiguring static default route via {gateway} on {iface}...")
    
    # Try to add the route
    run_cmd(['ip','route','add','default','via',gateway,'metric','200'],check=False)
    
    # Verify the route was added
    max_retries = 5
    retry_delay = 2
    route_added = False
    
    for attempt in range(max_retries):
        # Check if route exists
        route_output = run_cmd(['ip','route','show','default'], capture_output=True, text=True).stdout
        if f"default via {gateway}" in route_output:
            if DEBUG:
                print(f"Static default route via {gateway} successfully added")
            route_added = True
            break
        else:
            if DEBUG:
                print(f"Attempt {attempt+1}/{max_retries}: Static route not found")
                print(f"  - Current default routes:")
                for line in route_output.splitlines():
                    print(f"    {line}")
            
            # Try to ensure interface is up before adding route
            iface_status = run_cmd(['ip', 'link', 'show', 'dev', iface], capture_output=True, text=True).stdout
            if "state UP" not in iface_status:
                if DEBUG:
                    print(f"  - Interface {iface} is not up, bringing it up...")
                run_cmd(['ip', 'link', 'set', 'dev', iface, 'up'], check=True)
                time.sleep(1)  # Give it a moment to come up
            
            # Try adding the route again
            if DEBUG:
                print(f"  - Retrying route addition...")
            # First try to delete any existing default routes to avoid conflicts
            run_cmd(['ip','route','del','default'], check=False)
            # Then add our route
            run_cmd(['ip','route','add','default','via',gateway,'metric','200'],check=False)
            
            if DEBUG:
                print(f"  - Waiting {retry_delay} seconds before checking again...")
            time.sleep(retry_delay)
    
    if not route_added:
        print(f"{RED}ERROR: Failed to add static default route via {gateway} after {max_retries} attempts.{RESET}")
        print("This will likely cause subsequent tests to fail.")
        print("Continuing anyway, but expect failures...")
    
    return route_added

# Connectivity tests with DNS fallback logic
def run_tests(iface, ip_addr, mgmt1, client_subnet, dhcp_servers, radius_servers, secret, user, pwd, run_dhcp, run_radius, custom_dns_servers=None, custom_ntp_servers=None, test_results=None):
    """
    Run a comprehensive suite of network connectivity tests.
    
    This function tests:
    - DNS resolution and connectivity
    - DHCP relay functionality
    - RADIUS authentication
    - NTP synchronization
    - HTTPS connectivity
    - SSL certificate validation
    - UDP connectivity to guest access points
    
    Args:
        iface: The network interface to use for tests
        ip_addr: The IP address of the interface
        mgmt1: NSB subnet in CIDR notation
        client_subnet: Client subnet in CIDR notation
        dhcp_servers: List of DHCP server IP addresses
        radius_servers: List of RADIUS server IP addresses
        secret: RADIUS shared secret
        user: RADIUS username
        pwd: RADIUS password
        run_dhcp: Whether to run DHCP tests
        run_radius: Whether to run RADIUS tests
        custom_dns_servers: Optional list of custom DNS servers to test
        custom_ntp_servers: Optional list of custom NTP servers to test
        test_results: Optional list to append test results to
        
    Returns:
        list: A list of tuples containing test results (test_name, result)
    """
    # Initialize empty lists if None
    custom_dns_servers = custom_dns_servers or []
    custom_ntp_servers = custom_ntp_servers or []
    # Dictionary to store test results for summary
    if test_results is None:
        test_results = []
    # Set initial DNS
    dns_servers = ['8.8.8.8', '8.8.4.4']
    
    # Write DNS servers to resolv.conf
    def write_resolv(servers):
        with open('/etc/resolv.conf','w') as f:
            for s in servers:
                f.write(f'nameserver {s}\n')
    write_resolv(dns_servers)
    conf.route.resync()
    
    # Initial connectivity
    ping_ok = False
    print(f'\nInitial Ping Tests from {ip_addr}:')
    
    # Test default DNS servers with retry logic
    for tgt in dns_servers:
        # First attempt
        r = run_cmd(['ping', '-c', '2', '-I', ip_addr, tgt], capture_output=True)
        result = r.returncode == 0
        
        # If first attempt fails, retry once more
        if not result:
            print(f'Ping {tgt} from {ip_addr}: {RED}Fail{RESET} (First attempt)')
            print(f'Retrying ping to {tgt}...')
            r = run_cmd(['ping', '-c', '3', '-I', ip_addr, tgt], capture_output=True)  # Try with more pings
            result = r.returncode == 0
        
        print(f'Ping {tgt} from {ip_addr}: ' + (GREEN+'Success'+RESET if result else RED+'Fail{RESET} (After retry)'))
        test_results.append((f'Initial Ping {tgt} from {ip_addr}', result))
        ping_ok |= result
    
    # Test custom DNS servers if provided (with retry logic)
    if custom_dns_servers:
        print(f'\nCustom DNS Server Ping Tests from {ip_addr}:')
        custom_ping_ok = False
        for tgt in custom_dns_servers:
            # First attempt
            r = run_cmd(['ping', '-c', '2', '-I', ip_addr, tgt], capture_output=True)
            result = r.returncode == 0
            
            # If first attempt fails, retry once more
            if not result:
                print(f'Ping {tgt} from {ip_addr}: {RED}Fail{RESET} (First attempt)')
                print(f'Retrying ping to {tgt}...')
                r = run_cmd(['ping', '-c', '3', '-I', ip_addr, tgt], capture_output=True)  # Try with more pings
                result = r.returncode == 0
            
            print(f'Ping {tgt} from {ip_addr}: ' + (GREEN+'Success'+RESET if result else RED+'Fail{RESET} (After retry)'))
            test_results.append((f'Initial Ping Custom DNS {tgt} from {ip_addr}', result))
            custom_ping_ok |= result
        
        # Update ping_ok to include custom DNS server ping results
        ping_ok |= custom_ping_ok


    print(f'\nInitial DNS Tests from {ip_addr} (@ ' + ', '.join(dns_servers) + '):')
    for d in dns_servers:
        r = run_cmd(['dig', f'@{d}', '-b', ip_addr, 'www.google.com', '+short'], capture_output=True, text=True)
        ok = (r.returncode==0 and bool(r.stdout.strip()))
        print(f'DNS @{d} from {ip_addr}: ' + (GREEN+'Success'+RESET if ok else RED+'Fail'+RESET))
        test_results.append((f'Initial DNS @{d} from {ip_addr}', ok))

    # Custom DNS tests from iface interface if provided
    if custom_dns_servers:
        print(f'\n=== Custom DNS tests from {ip_addr} ===')
        for d in custom_dns_servers:
            r = run_cmd(['dig', f'@{d}', '-b', ip_addr, 'www.google.com', '+short'], capture_output=True, text=True)
            ok = (r.returncode==0 and bool(r.stdout.strip()))
            print(f'Custom DNS @{d} from {ip_addr}: ' + (GREEN+'Success'+RESET if ok else RED+'Fail'+RESET))
            test_results.append((f'Custom DNS @{d} from {ip_addr}', ok))
        
        # If custom DNS servers are provided and successful, use them
        successful_custom_dns = []
        for d in custom_dns_servers:
            r = run_cmd(['dig', f'@{d}', '-b', ip_addr, 'www.google.com', '+short'], capture_output=True, text=True)
            if r.returncode == 0 and bool(r.stdout.strip()):
                successful_custom_dns.append(d)
        
        if successful_custom_dns:
            print(f"\nUsing successful custom DNS servers: {', '.join(successful_custom_dns)}")
            dns_servers = successful_custom_dns
            write_resolv(dns_servers)
            # Skip the prompt for DNS servers if we're using custom ones
            if not args.config:  # Only in interactive mode
                print("Using custom DNS servers instead of prompting for new ones.")
        else:
            # If no custom DNS servers were successful, prompt for new ones
            new_dns = prompt_nonempty('Enter DNS server IP(s) (comma-separated): ')
            dns_servers = [s.strip() for s in new_dns.split(',')]
            write_resolv(dns_servers)
    else:
        # No custom DNS servers provided, prompt for new ones
        new_dns = prompt_nonempty('Enter DNS server IP(s) (comma-separated): ')
        dns_servers = [s.strip() for s in new_dns.split(',')]
        write_resolv(dns_servers)

    if not ping_ok:
        print(f"\n{RED}ERROR: Initial connectivity tests beyond local network have failed.{RESET}")
        print(f"{RED}All DNS servers (default and custom) are unreachable.{RESET}")
        print(f"{RED}Please validate your routing configuration and try again.{RESET}")
        print(f"{RED}Terminating tests...{RESET}")
        sys.exit(1)

    # Full suite
    print(f'\nFull Test Suite:')
    
    # Get the IP address of the mgmt1 dummy loopback interface
    mgmt1_ip = str(ipaddress.IPv4Network(mgmt1).network_address+1)
    print(f"Using mgmt1 dummy loopback interface with IP {mgmt1_ip} as source for tests")
    
    # Verify the dummy loopback interface is working properly with retry logic
    if DEBUG:
        print(f"Verifying {mgmt1_ip} can reach external targets...")
    max_retries = 5
    retry_delay = 2
    loopback_working = False
    
    for attempt in range(max_retries):
        if DEBUG:
            print(f"Attempt {attempt+1}/{max_retries}: Testing connectivity from {mgmt1_ip}...")
        ping_result = run_cmd(['ping', '-c', '2', '-I', mgmt1_ip, tgt], capture_output=True)
        if ping_result.returncode == 0:
            if DEBUG:
                print(f"Connectivity from {mgmt1_ip}: {GREEN}Success{RESET}")
            loopback_working = True
            break
        else:
            if DEBUG:
                print(f"Connectivity from {mgmt1_ip}: {RED}Fail{RESET}")
                print(f"Waiting {retry_delay} seconds before retrying...")
            time.sleep(retry_delay)
    
    if not loopback_working:
        print(f"\n{RED}ERROR: Could not establish connectivity from the dummy loopback interface {mgmt1_ip}.{RESET}")
        print(f"{RED}Please try the test again after resolving network issues.{RESET}")
        print(f"{RED}Terminating tests...{RESET}")
        return test_results  # Return early with the tests we've done so far

    # Ping tests
    print(f'\n=== Ping tests ===')
    for tgt in dns_servers:
        r = run_cmd(['ping', '-c', '4', '-I', mgmt1_ip, tgt], capture_output=True, text=True)
        result = r.returncode == 0
        print(f'Ping {tgt} from {mgmt1_ip}: ' + (GREEN+'Success'+RESET if result else RED+'Fail'+RESET))
        test_results.append((f'Ping {tgt} from {mgmt1_ip}', result))
    
    # DNS tests
    print(f'\n=== DNS tests ===')
    for d in dns_servers:
        r = run_cmd(['dig', f'@{d}', '-b', mgmt1_ip, 'www.google.com', '+short'], capture_output=True, text=True)
        ok = (r.returncode==0 and bool(r.stdout.strip()))
        print(f'DNS @{d} from {mgmt1_ip}: ' + (GREEN+'Success'+RESET if ok else RED+'Fail'+RESET))
        test_results.append((f'DNS @{d} from {mgmt1_ip}', ok))
    
    # DHCP relay with ping pre-check - using dhcppython library
    if run_dhcp:
        print(f'\n=== DHCP tests (L3 relay) ===')
        # Use the first IP of the client subnet as the helper IP (giaddr)
        helper_ip = str(ipaddress.IPv4Network(client_subnet).network_address+1)
        print(f"Using client subnet first IP {helper_ip} as DHCP relay agent (giaddr)")
        
        # For the source IP, we should use the helper IP address (giaddr)
        # This is what the server will see as the source of the packet
        source_ip = helper_ip
        if DEBUG:
            print(f"Using helper IP {source_ip} as source IP for DHCP packets")
        
        for srv in dhcp_servers:
            p = run_cmd(['ping', '-c', '5', srv], capture_output=True)
            if p.returncode != 0:
                result = False
                print(f'DHCP relay to {srv}: {RED}Fail (unreachable){RESET}')
                test_results.append((f'DHCP relay to {srv}', result))
                continue
            
            # Get the MAC address for the main interface (macOS way)
            iface_mac = None
            try:
                ifconfig_out = run_cmd(['ifconfig', iface], capture_output=True, text=True, check=True).stdout
                match = re.search(r'ether\s+([0-9a-fA-F:]{17})', ifconfig_out)
                if match:
                    iface_mac = match.group(1)
                    if DEBUG: print(f"  Found MAC {iface_mac} for {iface}")
            except subprocess.CalledProcessError as e:
                if DEBUG: print(f"  Error getting MAC for {iface}: {e.stderr}")
            except Exception as e_gen:
                 if DEBUG: print(f"  Unexpected error getting MAC for {iface}: {e_gen}")

            if not iface_mac:
                print(f"Warning: Could not determine MAC address for {iface} using ifconfig, using random MAC")
                iface_mac = dhcp_utils.random_mac()
                
            # Create a random client MAC address
            client_mac = dhcp_utils.random_mac()
            
            # Using dhcppython for DHCP testing

            if DEBUG:
                print(f"DHCP Test Details:")
                print(f"  Interface: {iface} (MAC: {iface_mac})")
                print(f"  Source IP: {source_ip}")
                print(f"  Destination IP: {srv}")
                print(f"  Client MAC: {client_mac}")
            
            try:
                # Create DHCP client using the main interface
                if DEBUG:
                    print(f"Creating DHCP client on {iface} interface...")
                c = dhcp_client.DHCPClient(
                    interface=iface,      # Main test interface (e.g., en0), used for MAC lookup etc.
                    send_from_port=67,    # Source port for outgoing DHCP Discover packets (as a relay)
                    send_to_port=67       # Destination port on the DHCP server
                )
                
                if DEBUG:
                    print(f"  Using helper IP {helper_ip} as relay address in DHCP request")
                
                # Create a list of DHCP options
                if DEBUG:
                    print(f"Setting up DHCP options...")
                options_list = dhcp_options.OptionList([
                    # Add standard options
                    dhcp_options.options.short_value_to_object(60, "nile-readiness-test"),  # Class identifier
                    dhcp_options.options.short_value_to_object(12, socket.gethostname()),   # Hostname
                    # Parameter request list - request common options
                    dhcp_options.options.short_value_to_object(55, [1, 3, 6, 15, 26, 28, 51, 58, 59, 43])
                ])
                
                if DEBUG:
                    print(f"Attempting to get DHCP lease from {srv}...")
                # Set broadcast=False for unicast to specific server
                # Set server to the DHCP server IP
                try:
                    # Flag to track if we received an OFFER
                    offer_received = False
                    
                    # Function to process DHCP packets
                    def dhcp_callback(pkt):
                        nonlocal offer_received
                        # Check for UDP traffic on DHCP ports (67, 68)
                        # Note: In relay scenarios, server may send to port 67 (relay port)
                        if UDP in pkt and (pkt[UDP].dport == 67 or pkt[UDP].dport == 68 or 
                                          pkt[UDP].sport == 67 or pkt[UDP].sport == 68):
                            if DEBUG:
                                print("  [Scapy Sniffer] Detected UDP packet on DHCP ports")
                                print(f"  [Scapy Sniffer] Source: {pkt[IP].src}:{pkt[UDP].sport} -> Dest: {pkt[IP].dst}:{pkt[UDP].dport}")
                            
                            # Look for BOOTP/DHCP packets
                            if BOOTP in pkt:
                                if DEBUG:
                                    print("  [Scapy Sniffer] Detected BOOTP packet")
                                    print(f"  [Scapy Sniffer] BOOTP details:")
                                    print(f"    Your IP: {pkt[BOOTP].yiaddr}")
                                    print(f"    Server IP: {pkt[BOOTP].siaddr}")
                                    print(f"    Gateway: {pkt[BOOTP].giaddr}")
                                
                                # Check if this is a DHCP packet with options
                                if DHCP in pkt:
                                    if DEBUG:
                                        print("  [Scapy Sniffer] Detected DHCP options")
                                    
                                    # Look for DHCP message type option (53)
                                    for opt in pkt[DHCP].options:
                                        if isinstance(opt, tuple) and len(opt) >= 2:
                                            if opt[0] == 'message-type':
                                                if DEBUG:
                                                    print(f"  [Scapy Sniffer] DHCP message type: {opt[1]}")
                                                
                                                # Check if it's an OFFER (type 2)
                                                if opt[1] == 2:  # OFFER
                                                    if DEBUG:
                                                        print("  [Scapy Sniffer] Found DHCP OFFER!")
                                                        print(f"  [Scapy Sniffer] DHCP OFFER details:")
                                                        print(f"    Your IP: {pkt[BOOTP].yiaddr}")
                                                        print(f"    Server IP: {pkt[BOOTP].siaddr}")
                                                        print(f"    Gateway: {pkt[BOOTP].giaddr}")
                                                        print(f"    Source: {pkt[IP].src}:{pkt[UDP].sport}")
                                                        print(f"    Destination: {pkt[IP].dst}:{pkt[UDP].dport}")
                                                    offer_received = True
                                                    return None
                        # Return None instead of False to prevent printing
                        return None
                    
                    # Start sniffing in a separate thread
                    if DEBUG:
                        print("  Starting packet capture for DHCP OFFER...")
                    
                    # Set up sniffing timeout
                    sniff_timeout = 10  # seconds
                    
                    # Start sniffing before sending the DHCP request
                    sniff_thread = threading.Thread(
                        target=lambda: sniff(
                            iface=iface, 
                            filter="udp and (port 67 or port 68)", 
                            prn=dhcp_callback, 
                            stop_filter=lambda p: offer_received, 
                            timeout=sniff_timeout
                        )
                    )
                    sniff_thread.daemon = True
                    sniff_thread.start()
                    
                    # Wait a moment for sniffing to start
                    time.sleep(0.5)
                    
                    # Now try to get a lease using dhcppython
                    if DEBUG:
                        print("  Sending DHCP request using dhcppython...")
                    
                    try:
                        lease = c.get_lease(
                            client_mac,
                            broadcast=False,
                            options_list=options_list,
                            server=srv,
                            relay=helper_ip
                        )
                        
                        # If we get here, we got a lease with dhcppython
                        if DEBUG:
                            print(f"\nSuccessfully obtained DHCP lease with dhcppython!")
                            print(f"DEBUG: Lease details:")
                            print(f"  Your IP: {lease.ack.yiaddr}")
                            print(f"  Server IP: {lease.ack.siaddr}")
                            print(f"  Gateway: {lease.ack.giaddr}")
                            print(f"  Options: {lease.ack.options}")
                        
                        result = True
                        print(f'DHCP relay to {srv}: ' + GREEN+'Success (dhcppython)'+RESET)
                        test_results.append((f'DHCP relay to {srv}', result))
                    except Exception as e:
                        if DEBUG:
                            print(f"Error during DHCP lease request with dhcppython: {e}")
                            import traceback
                            traceback.print_exc()
                        # In non-debug mode, don't print anything about fallback methods
                        
                        # Wait for the sniffing thread to complete
                        sniff_thread.join()
                        
                        # Check if we received an OFFER via packet sniffing
                        if offer_received:
                            result = True
                            print(f'DHCP relay to {srv}: ' + GREEN+'Success (OFFER detected)'+RESET)
                            test_results.append((f'DHCP relay to {srv}', result))
                        else:
                            # If no OFFER was detected with packet sniffing either, try a direct approach
                            if DEBUG:
                                print("  Attempting direct DHCP DISCOVER...")
                            
                            # Generate a random transaction ID
                            xid = random.randint(1, 0xFFFFFFFF)
                            
                            # Parse MAC address
                            mac_bytes = bytes.fromhex(client_mac.replace(':', ''))
                            
                            # Create and send DHCP DISCOVER packet
                            dhcp_discover = (Ether(dst="ff:ff:ff:ff:ff:ff", src=client_mac) /
                                            IP(src="0.0.0.0", dst="255.255.255.255") /
                                            UDP(sport=68, dport=67) /
                                            BOOTP(chaddr=mac_bytes, xid=xid, flags=0x8000) /
                                            DHCP(options=[("message-type", "discover"), "end"]))
                            
                            # Reset the offer_received flag
                            offer_received = False
                            
                            # Start a new sniffing thread
                            sniff_thread = threading.Thread(
                                target=lambda: sniff(
                                    iface=iface, 
                                    filter="udp and (port 67 or port 68)", 
                                    prn=dhcp_callback, 
                                    stop_filter=lambda p: offer_received, 
                                    timeout=sniff_timeout
                                )
                            )
                            sniff_thread.daemon = True
                            sniff_thread.start()
                            
                            # Wait a moment for sniffing to start
                            time.sleep(0.5)
                            
                            # Send the packet
                            if DEBUG:
                                print("  Sending DHCP DISCOVER packet with scapy...")
                            sendp(dhcp_discover, iface=iface, verbose=0)
                            
                            # Wait for sniffing to complete
                            sniff_thread.join()
                            
                            # Check the result
                            if offer_received:
                                result = True
                                print(f'DHCP relay to {srv}: ' + GREEN+'Success (OFFER received)'+RESET)
                                test_results.append((f'DHCP relay to {srv}', result))
                            else:
                                result = False
                                print(f'DHCP relay to {srv}: ' + RED+'Fail (no DHCP OFFER detected)'+RESET)
                                test_results.append((f'DHCP relay to {srv}', result))
                except Exception as e:
                    print(f"Error during DHCP test: {e}")
                    if DEBUG:
                        import traceback
                        traceback.print_exc()
                    result = False
                    print(f'DHCP relay to {srv}: ' + RED+'Fail'+RESET)
                    test_results.append((f'DHCP relay to {srv}', result))
                
                
            except Exception as e:
                print(f"Error during DHCP test: {e}")
                if DEBUG:
                    import traceback
                    traceback.print_exc()
                result = False
                print(f'DHCP relay to {srv}: ' + RED+'Fail'+RESET)
                test_results.append((f'DHCP relay to {srv}', result))
    else:
        print('\nSkipping DHCP tests')

    # RADIUS with ping pre-check
    if run_radius:
        print(f'\n=== RADIUS tests ===')
        for srv in radius_servers:
            p = run_cmd(['ping', '-c', '1', srv], capture_output=True)
            if p.returncode != 0:
                result = False
                print(f'RADIUS {srv}: {RED}Fail (unreachable){RESET}')
                test_results.append((f'RADIUS {srv}', result))
                continue
            cmd = (f'echo "User-Name={user},User-Password={pwd}" '
                  f'| radclient -x -s {srv}:1812 auth {secret}')
            res = run_cmd(cmd, shell=True, capture_output=True, text=True)
            result = res.returncode == 0
            print(f'RADIUS {srv}: ' + (GREEN+'Success'+RESET if result else RED+'Fail'+RESET))
            test_results.append((f'RADIUS {srv}', result))
    else:
        print('\nSkipping RADIUS tests')

    # NTP tests from main interface
    print(f'\n=== NTP tests from main interface ({ip_addr}) ===')
    # Test default NTP servers
    for ntp in ('time.google.com', 'pool.ntp.org'):
        r = run_cmd(['ntpdate', '-q', '-b', ip_addr, ntp], capture_output=True, text=True)
        result = r.returncode == 0
        print(f'NTP {ntp} from {ip_addr}: ' + (GREEN+'Success'+RESET if result else RED+'Fail'+RESET))
        test_results.append((f'NTP {ntp} from {ip_addr}', result))
    
    # Test custom NTP servers if provided
    if custom_ntp_servers:
        print(f'\n=== Custom NTP tests from main interface ({ip_addr}) ===')
        for ntp in custom_ntp_servers:
            r = run_cmd(['ntpdate', '-q', '-b', ip_addr, ntp], capture_output=True, text=True)
            result = r.returncode == 0
            print(f'Custom NTP {ntp} from {ip_addr}: ' + (GREEN+'Success'+RESET if result else RED+'Fail'+RESET))
            test_results.append((f'Custom NTP {ntp} from {ip_addr}', result))
    
    # NTP tests from mgmt1
    print(f'\n=== NTP tests from mgmt1 ({mgmt1_ip}) ===')
    # Test default NTP servers
    for ntp in ('time.google.com', 'pool.ntp.org'):
        r = run_cmd(['ntpdate', '-q', '-b', mgmt1_ip, ntp], capture_output=True, text=True)
        result = r.returncode == 0
        print(f'NTP {ntp} from {mgmt1_ip}: ' + (GREEN+'Success'+RESET if result else RED+'Fail'+RESET))
        test_results.append((f'NTP {ntp} from {mgmt1_ip}', result))
    
    # Test custom NTP servers if provided
    if custom_ntp_servers:
        print(f'\n=== Custom NTP tests from mgmt1 ({mgmt1_ip}) ===')
        for ntp in custom_ntp_servers:
            r = run_cmd(['ntpdate', '-q', '-b', mgmt1_ip, ntp], capture_output=True, text=True)
            result = r.returncode == 0
            print(f'Custom NTP {ntp} from {mgmt1_ip}: ' + (GREEN+'Success'+RESET if result else RED+'Fail'+RESET))
            test_results.append((f'Custom NTP {ntp} from {mgmt1_ip}', result))

    # HTTPS and SSL Certificate tests
    print(f'\n=== HTTPS and SSL Certificate tests ===')
    
    # Test HTTPS connectivity and SSL certificates for Nile Cloud from main interface
    print(f'Testing HTTPS for {NILE_HOSTNAME} from {ip_addr}...')
    parsed = urlparse(f'https://{NILE_HOSTNAME}')
    host, port = parsed.hostname, parsed.port or 443
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((ip_addr, 0))  # Bind to ip_addr with a random port
        sock.connect((host, port))
        sock.close()
        https_ok = True
        print(f'HTTPS {NILE_HOSTNAME} from {ip_addr}: {GREEN}Success{RESET}')
    except Exception as e:
        https_ok = False
        if DEBUG:
            print(f'HTTPS {NILE_HOSTNAME} from {ip_addr}: {RED}Fail{RESET} ({e})')
        else:
            print(f'HTTPS {NILE_HOSTNAME} from {ip_addr}: {RED}Fail{RESET}')
    test_results.append((f'HTTPS {NILE_HOSTNAME} from {ip_addr}', https_ok))
    
    # Test HTTPS connectivity and SSL certificates for Nile Cloud from mgmt1
    print(f'\nTesting HTTPS for {NILE_HOSTNAME} from {mgmt1_ip}...')
    parsed = urlparse(f'https://{NILE_HOSTNAME}')
    host, port = parsed.hostname, parsed.port or 443
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((mgmt1_ip, 0))  # Bind to mgmt1_ip with a random port
        sock.connect((host, port))
        sock.close()
        https_ok = True
        print(f'HTTPS {NILE_HOSTNAME} from {mgmt1_ip}: {GREEN}Success{RESET}')
    except Exception as e:
        https_ok = False
        if DEBUG:
            print(f'HTTPS {NILE_HOSTNAME} from {mgmt1_ip}: {RED}Fail{RESET} ({e})')
        else:
            print(f'HTTPS {NILE_HOSTNAME} from {mgmt1_ip}: {RED}Fail{RESET}')
    test_results.append((f'HTTPS {NILE_HOSTNAME} from {mgmt1_ip}', https_ok))
    
    # Now check the SSL certificate
    print(f'\nChecking SSL certificate for {NILE_HOSTNAME}...')
    # Use dig to resolve the hostname to IP addresses
    r = run_cmd(['dig', NILE_HOSTNAME, '+short'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        nile_ips = r.stdout.strip().split('\n')
        print(f"\nResolved {NILE_HOSTNAME} to: {', '.join(nile_ips)}")
        nile_ssl_success = False
        for ip in nile_ips:
            if check_ssl_certificate(ip, NILE_HOSTNAME, "Nile Global Inc."):
                nile_ssl_success = True
                print(f"SSL certificate for {NILE_HOSTNAME}: {GREEN}Success{RESET}")
                break
            else:
                print(f"SSL certificate for {NILE_HOSTNAME} (IP: {ip}): {RED}Fail{RESET}")
        test_results.append((f"SSL Certificate for {NILE_HOSTNAME}", nile_ssl_success))
    else:
        print(f"Could not resolve {NILE_HOSTNAME} for SSL check")
        test_results.append((f"SSL Certificate for {NILE_HOSTNAME}", False))
    
    # Test HTTPS connectivity and SSL certificates for Amazon S3 from main interface
    print(f'\nTesting HTTPS for {S3_HOSTNAME} from {ip_addr}...')
    r = run_cmd(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', 
                 '--connect-timeout', '10', '--interface', ip_addr,
                 '-A', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                 '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                 '-H', 'Accept-Language: en-US,en;q=0.5',
                 f'https://{S3_HOSTNAME}/nile-prod-us-west-2'], capture_output=True, text=True)
    # For HTTPS tests, consider 2xx and 3xx as success (redirects are common)
    https_ok = r.returncode == 0 and (r.stdout.strip().startswith('2') or r.stdout.strip().startswith('3'))
    if https_ok:
        print(f'HTTPS {S3_HOSTNAME} from {ip_addr}: {GREEN}Success{RESET} (Status: {r.stdout.strip()})')
    else:
        # For 403 errors, it's still a successful connection, just forbidden access
        if r.stdout.strip() == '403':
            print(f'HTTPS {S3_HOSTNAME} from {ip_addr}: {GREEN}Success{RESET} (Status: 403 Forbidden - connection successful but access denied)')
            https_ok = True
        else:
            print(f'HTTPS {S3_HOSTNAME} from {ip_addr}: {RED}Fail{RESET} (Status: {r.stdout.strip() if r.stdout else "Connection failed"})')
    test_results.append((f'HTTPS {S3_HOSTNAME} from {ip_addr}', https_ok))
    
    # Test HTTPS connectivity and SSL certificates for Amazon S3 from mgmt1
    print(f'\nTesting HTTPS for {S3_HOSTNAME} from {mgmt1_ip}...')
    r = run_cmd(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', 
                 '--connect-timeout', '10', '--interface', mgmt1_ip,
                 '-A', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                 '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                 '-H', 'Accept-Language: en-US,en;q=0.5',
                 f'https://{S3_HOSTNAME}/nile-prod-us-west-2'], capture_output=True, text=True)
    # For HTTPS tests, consider 2xx and 3xx as success (redirects are common)
    https_ok = r.returncode == 0 and (r.stdout.strip().startswith('2') or r.stdout.strip().startswith('3'))
    if https_ok:
        print(f'HTTPS {S3_HOSTNAME} from {mgmt1_ip}: {GREEN}Success{RESET} (Status: {r.stdout.strip()})')
    else:
        # For 403 errors, it's still a successful connection, just forbidden access
        if r.stdout.strip() == '403':
            print(f'HTTPS {S3_HOSTNAME} from {mgmt1_ip}: {GREEN}Success{RESET} (Status: 403 Forbidden - connection successful but access denied)')
            https_ok = True
        else:
            print(f'HTTPS {S3_HOSTNAME} from {mgmt1_ip}: {RED}Fail{RESET} (Status: {r.stdout.strip() if r.stdout else "Connection failed"})')
    test_results.append((f'HTTPS {S3_HOSTNAME} from {mgmt1_ip}', https_ok))
    
    # Now check the SSL certificate
    print(f'\nChecking SSL certificate for {S3_HOSTNAME}...')
    # Use dig to resolve the hostname to IP addresses
    r = run_cmd(['dig', S3_HOSTNAME, '+short'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        s3_ips = r.stdout.strip().split('\n')
        print(f"\nResolved {S3_HOSTNAME} to: {', '.join(s3_ips)}")
        s3_ssl_success = False
        # Only test the first 2 IPs for S3
        for ip in s3_ips[:2]:
            if check_ssl_certificate(ip, S3_HOSTNAME, "Amazon"):
                s3_ssl_success = True
                print(f"SSL certificate for {S3_HOSTNAME}: {GREEN}Success{RESET}")
                break
            else:
                print(f"SSL certificate for {S3_HOSTNAME} (IP: {ip}): {RED}Fail{RESET}")
        test_results.append((f"SSL Certificate for {S3_HOSTNAME}", s3_ssl_success))
    else:
        print(f"Could not resolve {S3_HOSTNAME} for SSL check")
        test_results.append((f"SSL Certificate for {S3_HOSTNAME}", False))
    
    # Test HTTPS connectivity for Nile Secure from main interface
    print(f'\nTesting HTTPS for u1.nilesecure.com from {ip_addr}...')
    r = run_cmd(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', 
                 '--connect-timeout', '10', '--interface', ip_addr,
                 '-A', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                 '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                 '-H', 'Accept-Language: en-US,en;q=0.5',
                 'https://u1.nilesecure.com'], capture_output=True, text=True)
    # For HTTPS tests, consider 2xx and 3xx as success (redirects are common)
    https_ok = r.returncode == 0 and (r.stdout.strip().startswith('2') or r.stdout.strip().startswith('3'))
    if https_ok:
        print(f'HTTPS u1.nilesecure.com from {ip_addr}: {GREEN}Success{RESET} (Status: {r.stdout.strip()})')
    else:
        # For 403 errors, it's still a successful connection, just forbidden access
        if r.stdout.strip() == '403':
            print(f'HTTPS u1.nilesecure.com from {ip_addr}: {GREEN}Success{RESET} (Status: 403 Forbidden - connection successful but access denied)')
            https_ok = True
        else:
            print(f'HTTPS u1.nilesecure.com from {ip_addr}: {RED}Fail{RESET} (Status: {r.stdout.strip() if r.stdout else "Connection failed"})')
    test_results.append((f'HTTPS u1.nilesecure.com from {ip_addr}', https_ok))
    
    # Test HTTPS connectivity for Nile Secure from mgmt1
    print(f'\nTesting HTTPS for u1.nilesecure.com from {mgmt1_ip}...')
    r = run_cmd(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', 
                 '--connect-timeout', '10', '--interface', mgmt1_ip,
                 '-A', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                 '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                 '-H', 'Accept-Language: en-US,en;q=0.5',
                 'https://u1.nilesecure.com'], capture_output=True, text=True)
    # For HTTPS tests, consider 2xx and 3xx as success (redirects are common)
    https_ok = r.returncode == 0 and (r.stdout.strip().startswith('2') or r.stdout.strip().startswith('3'))
    if https_ok:
        print(f'HTTPS u1.nilesecure.com from {mgmt1_ip}: {GREEN}Success{RESET} (Status: {r.stdout.strip()})')
    else:
        # For 403 errors, it's still a successful connection, just forbidden access
        if r.stdout.strip() == '403':
            print(f'HTTPS u1.nilesecure.com from {mgmt1_ip}: {GREEN}Success{RESET} (Status: 403 Forbidden - connection successful but access denied)')
            https_ok = True
        else:
            print(f'HTTPS u1.nilesecure.com from {mgmt1_ip}: {RED}Fail{RESET} (Status: {r.stdout.strip() if r.stdout else "Connection failed"})')
    test_results.append((f'HTTPS u1.nilesecure.com from {mgmt1_ip}', https_ok))
    
    # UDP Connectivity Check for Guest Access
    print(f'\n=== UDP Connectivity Check for Guest Access ===')
    guest_success = False
    
    for ip in GUEST_IPS:
        print(f"Testing UDP connectivity to {ip}:{UDP_PORT}...")
        if check_udp_connectivity_netcat(ip, UDP_PORT):
            guest_success = True
            print(f"UDP connectivity to {ip}:{UDP_PORT}: {GREEN}Success{RESET}")
            break
        else:
            print(f"UDP connectivity to {ip}:{UDP_PORT}: {RED}Fail{RESET}")
    
    test_results.append(("UDP Connectivity Check for Guest Access", guest_success))
    
    
    return test_results

# Print test summary
def print_test_summary(test_results):
    """
    Print a summary of all test results.
    
    This function:
    - Filters out tests that shouldn't be included in the summary
    - Prints each test name and its result (Success/Fail)
    - Calculates and prints the overall success rate
    
    Args:
        test_results: List of tuples containing test results (test_name, result)
    """
    print("\n=== Test Summary ===")
    success_count = 0
    total_count = 0
    
    # Filter out tests that shouldn't be included in the summary
    excluded_tests = ["Static Default Route Configuration"]
    
    for test_name, result in test_results:
        # Skip excluded tests
        if test_name in excluded_tests:
            continue
            
        status = GREEN + "Success" + RESET if result else RED + "Fail" + RESET
        print(f"{test_name}: {status}")
        total_count += 1
        if result:
            success_count += 1
    
    success_rate = (success_count / total_count) * 100 if total_count > 0 else 0
    print(f"\nOverall: {success_count}/{total_count} tests passed ({success_rate:.1f}%)")

# Main flow
def main():
    """
    Main execution flow of the Nile Readiness Test.
    
    This function:
    1. Gathers configuration parameters from file or user input
    2. Records the original state of the network interface
    3. Configures the interface and adds loopback interfaces
    4. Sets up a static default route
    5. Configures OSPF routing
    6. Runs connectivity tests (DNS, DHCP, RADIUS, NTP, HTTPS, UDP)
    7. Restores the original state of the system
    8. Prints a summary of test results
    """
    # Get user input from config file or interactive prompts
    (test_iface, ip_addr, netmask, gateway, mgmt_interface,
     mgmt1, mgmt2, client_subnet,
     dhcp_servers, radius_servers, secret, username, password,
     run_dhcp, run_radius, custom_dns_servers, custom_ntp_servers) = get_user_input(args.config)
    
    # Print summary of custom DNS and NTP servers if provided
    if custom_dns_servers:
        print(f"\nWill test custom DNS servers: {', '.join(custom_dns_servers)}")
    if custom_ntp_servers:
        print(f"\nWill test custom NTP servers: {', '.join(custom_ntp_servers)}")

    # Record the original state of the interface
    state = record_state(test_iface)
    
    try:
        # Aggressively reset interfaces - in some cases the interfaces get to a unworkable state after initial boot, this works around this situation
        print('\nConfiguring Interfaces... (Can take up to 20 seconds)')
        restore_state(test_iface, state)
        time.sleep(5)
        configure_interface(test_iface, ip_addr, netmask, mgmt_interface)
        add_loopbacks(mgmt1, mgmt2, client_subnet)
        configure_static_route(gateway, test_iface)
        time.sleep(5)
        restore_state(test_iface, state)
        time.sleep(5)

        # Configure the interface (includes validation and retry logic)
        interface_up = configure_interface(test_iface, ip_addr, netmask, mgmt_interface)
        
        # Add loopbacks
        add_loopbacks(mgmt1, mgmt2, client_subnet)
        
        # Configure static route and verify it was added successfully
        prefix = ipaddress.IPv4Network(f'0.0.0.0/{netmask}').prefixlen
        route_added = configure_static_route(gateway, test_iface)
        
        # Add the route status to the test results
        test_results = []
        test_results.append(("Static Default Route Configuration", route_added))

        # Update scapy's routing table
        conf.route.resync()
        
        # Sniff for OSPF Hello packets
        up, area, hi, di = sniff_ospf_hello(test_iface)
        
        # Configure OSPF
        configure_ospf(test_iface, ip_addr, prefix, mgmt1, mgmt2, client_subnet, up, area, hi, di)
        
        # Check OSPF status
        ospf_ok = show_ospf_status()
        print("OSPF adjacency test: " + (GREEN+'Success'+RESET if ospf_ok else RED+'Fail'+RESET))
        
        # Add OSPF test result to the test results list
        test_results.append(("OSPF Adjacency Test", ospf_ok))

        # Run connectivity tests with the existing test_results list
        test_results = run_tests(test_iface, ip_addr, mgmt1, client_subnet, dhcp_servers, radius_servers, secret, username, password, run_dhcp, run_radius, custom_dns_servers, custom_ntp_servers, test_results)
    
    finally:
        # Restore the original state
        print('\nRestoring original state...')
        restore_state(test_iface, state)
        
        # Print test summary after restoring state
        if 'test_results' in locals():
            print_test_summary(test_results)

if __name__=='__main__':
    if os.geteuid()!=0:
        print('Must run as root')
        sys.exit(1)
    
    main()
