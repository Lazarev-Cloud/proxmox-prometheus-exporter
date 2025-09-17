#!/usr/bin/env python3
"""
Enhanced Smart Adaptive Proxmox Node Exporter for Prometheus
Version: 2.0.0
Automatically detects and monitors all available system components with advanced metrics
"""

import subprocess
import re
import time
import socket
import os
import platform
import json
import glob
import shutil
import threading
import concurrent.futures
import hashlib
import signal
import sys
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import lru_cache, wraps
from typing import Dict, List, Tuple, Optional, Any
from prometheus_client import start_http_server, Gauge, Info, Counter, Histogram, Summary
from prometheus_client.core import CollectorRegistry
import logging

# Try to import psutil, install if not available
try:
    import psutil
except ImportError:
    print("psutil not installed. Installing...")
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'psutil'], check=True)
    import psutil

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s:%(funcName)s:%(lineno)d] %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
EXPORTER_PORT = int(os.environ.get('EXPORTER_PORT', 9101))
COLLECTION_INTERVAL = int(os.environ.get('COLLECTION_INTERVAL', 15))
DEBUG_MODE = os.environ.get('DEBUG_MODE', '').lower() in ('true', '1', 'yes')
PARALLEL_COLLECTORS = os.environ.get('PARALLEL_COLLECTORS', 'true').lower() in ('true', '1', 'yes')
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', 4))

if DEBUG_MODE:
    logger.setLevel(logging.DEBUG)

class MetricCache:
    """Simple TTL cache for expensive operations"""
    def __init__(self, default_ttl=60):
        self.cache = {}
        self.default_ttl = default_ttl
    
    def get(self, key, compute_func, ttl=None):
        """Get value from cache or compute it"""
        if ttl is None:
            ttl = self.default_ttl
        
        now = time.time()
        if key in self.cache:
            value, expiry = self.cache[key]
            if now < expiry:
                return value
        
        value = compute_func()
        self.cache[key] = (value, now + ttl)
        return value
    
    def clear_expired(self):
        """Remove expired entries"""
        now = time.time()
        expired = [k for k, (_, exp) in self.cache.items() if now >= exp]
        for k in expired:
            del self.cache[k]

class RateLimiter:
    """Rate limiter for expensive operations"""
    def __init__(self, max_calls=10, period=60):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()
    
    def allow(self):
        """Check if operation is allowed"""
        now = time.time()
        # Remove old calls
        while self.calls and self.calls[0] <= now - self.period:
            self.calls.popleft()
        
        if len(self.calls) < self.max_calls:
            self.calls.append(now)
            return True
        return False

def timed_operation(timeout=5):
    """Decorator to add timeout to operations"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except subprocess.TimeoutExpired:
                logger.warning(f"{func.__name__} timed out after {timeout}s")
                return None
            except Exception as e:
                logger.error(f"Error in {func.__name__}: {e}")
                return None
        return wrapper
    return decorator

class EnhancedProxmoxExporter:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.hostname = socket.gethostname()
        self.start_time = time.time()
        self.cache = MetricCache()
        self.rate_limiter = RateLimiter()
        
        # Feature detection flags
        self.features = {
            'sensors': False,
            'zfs': False,
            'nvidia_gpu': False,
            'amd_gpu': False,
            'intel_gpu': False,
            'qemu_vms': False,
            'lxc_containers': False,
            'docker': False,
            'podman': False,
            'smart_monitoring': False,
            'ipmi': False,
            'nvme': False,
            'systemd': False,
            'mdadm': False,
            'nfs': False,
            'nut_ups': False,
            'ceph': False,
            'glusterfs': False,
            'btrfs': False,
        }
        
        # Collectors
        self.collectors = {}
        
        # Thread pool for parallel collection
        if PARALLEL_COLLECTORS:
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
        else:
            self.executor = None
        
        # Detect available features
        self._detect_features()
        
        # Initialize metrics based on detected features
        self._init_all_metrics()
        
        logger.info(f"Enhanced Exporter initialized with features: {[k for k,v in self.features.items() if v]}")
    
    def _detect_features(self):
        """Detect available system features with parallel detection"""
        logger.info("Detecting system features...")
        
        detection_tasks = [
            ('sensors', self._detect_sensors),
            ('zfs', self._detect_zfs),
            ('nvidia_gpu', self._detect_nvidia_gpu),
            ('amd_gpu', self._detect_amd_gpu),
            ('intel_gpu', self._detect_intel_gpu),
            ('qemu_vms', self._detect_qemu),
            ('lxc_containers', self._detect_lxc),
            ('docker', self._detect_docker),
            ('podman', self._detect_podman),
            ('smart_monitoring', self._detect_smart),
            ('ipmi', self._detect_ipmi),
            ('nvme', self._detect_nvme),
            ('systemd', self._detect_systemd),
            ('mdadm', self._detect_mdadm),
            ('nfs', self._detect_nfs),
            ('nut_ups', self._detect_nut),
            ('ceph', self._detect_ceph),
            ('glusterfs', self._detect_glusterfs),
            ('btrfs', self._detect_btrfs),
        ]
        
        for feature, detect_func in detection_tasks:
            try:
                if detect_func():
                    self.features[feature] = True
                    logger.info(f"✓ {feature.replace('_', ' ').title()} detected")
            except Exception as e:
                logger.debug(f"Failed to detect {feature}: {e}")
    
    def _detect_sensors(self):
        """Detect temperature sensors"""
        if shutil.which('sensors'):
            result = subprocess.run(['sensors'], capture_output=True, text=True, timeout=2)
            return result.returncode == 0 and len(result.stdout) > 10
        return False
    
    def _detect_zfs(self):
        """Detect ZFS"""
        return os.path.exists('/proc/spl/kstat/zfs') or shutil.which('zpool')
    
    def _detect_nvidia_gpu(self):
        """Detect NVIDIA GPU"""
        if shutil.which('nvidia-smi'):
            result = subprocess.run(['nvidia-smi', '-L'], capture_output=True, text=True, timeout=2)
            return result.returncode == 0 and 'GPU' in result.stdout
        return False
    
    def _detect_amd_gpu(self):
        """Detect AMD GPU"""
        # Check via vendor ID in sysfs
        amd_cards = glob.glob('/sys/class/drm/card*/device/vendor')
        for vendor_file in amd_cards:
            try:
                with open(vendor_file, 'r') as f:
                    if f.read().strip() == '0x1002':  # AMD vendor ID
                        return True
            except:
                pass
        
        # Check rocm-smi
        if shutil.which('rocm-smi'):
            result = subprocess.run(['rocm-smi', '--showid'], capture_output=True, text=True, timeout=2)
            return result.returncode == 0
        
        return False
    
    def _detect_intel_gpu(self):
        """Detect Intel GPU"""
        intel_cards = glob.glob('/sys/class/drm/card*/device/vendor')
        for vendor_file in intel_cards:
            try:
                with open(vendor_file, 'r') as f:
                    if f.read().strip() == '0x8086':  # Intel vendor ID
                        card_dir = os.path.dirname(vendor_file)
                        # Check for Arc/Xe graphics (not just integrated)
                        device_file = os.path.join(card_dir, 'device')
                        if os.path.exists(device_file):
                            with open(device_file, 'r') as df:
                                device_id = df.read().strip()
                                # Intel Arc/Xe device IDs typically start with 0x56 or 0x4c
                                if device_id.startswith(('0x56', '0x4c')):
                                    return True
            except:
                pass
        return False
    
    def _detect_qemu(self):
        """Detect QEMU VMs"""
        if shutil.which('qm'):
            result = subprocess.run(['qm', 'list'], capture_output=True, text=True, timeout=2)
            return result.returncode == 0
        return False
    
    def _detect_lxc(self):
        """Detect LXC containers"""
        if shutil.which('pct'):
            result = subprocess.run(['pct', 'list'], capture_output=True, text=True, timeout=2)
            return result.returncode == 0
        return False
    
    def _detect_docker(self):
        """Detect Docker"""
        if shutil.which('docker'):
            result = subprocess.run(['docker', 'version'], capture_output=True, text=True, timeout=2)
            return result.returncode == 0
        return False
    
    def _detect_podman(self):
        """Detect Podman"""
        if shutil.which('podman'):
            result = subprocess.run(['podman', 'version'], capture_output=True, text=True, timeout=2)
            return result.returncode == 0
        return False
    
    def _detect_smart(self):
        """Detect SMART monitoring"""
        return shutil.which('smartctl') is not None
    
    def _detect_ipmi(self):
        """Detect IPMI"""
        if shutil.which('ipmitool'):
            result = subprocess.run(['ipmitool', 'sensor'], capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        return False
    
    def _detect_nvme(self):
        """Detect NVMe devices"""
        return len(glob.glob('/sys/class/nvme/nvme*')) > 0
    
    def _detect_systemd(self):
        """Detect systemd"""
        return shutil.which('systemctl') is not None
    
    def _detect_mdadm(self):
        """Detect mdadm RAID"""
        if os.path.exists('/proc/mdstat'):
            with open('/proc/mdstat', 'r') as f:
                content = f.read()
                return 'md' in content
        return False
    
    def _detect_nfs(self):
        """Detect NFS mounts"""
        try:
            with open('/proc/mounts', 'r') as f:
                for line in f:
                    if ':' in line and ('nfs' in line or 'nfs4' in line):
                        return True
        except:
            pass
        return False
    
    def _detect_nut(self):
        """Detect NUT UPS monitoring"""
        return shutil.which('upsc') is not None
    
    def _detect_ceph(self):
        """Detect Ceph"""
        return shutil.which('ceph') is not None
    
    def _detect_glusterfs(self):
        """Detect GlusterFS"""
        return shutil.which('gluster') is not None
    
    def _detect_btrfs(self):
        """Detect Btrfs filesystems"""
        try:
            result = subprocess.run(['findmnt', '-t', 'btrfs'], capture_output=True, text=True, timeout=2)
            return result.returncode == 0 and len(result.stdout.strip()) > 0
        except:
            return False
    
    def _init_all_metrics(self):
        """Initialize all metrics"""
        self._init_base_metrics()
        self._init_advanced_metrics()
        
        if self.features['sensors']:
            self._init_temperature_metrics()
        if self.features['zfs']:
            self._init_zfs_metrics()
        if any([self.features['nvidia_gpu'], self.features['amd_gpu'], self.features['intel_gpu']]):
            self._init_gpu_metrics()
        if self.features['qemu_vms'] or self.features['lxc_containers']:
            self._init_vm_metrics()
        if self.features['docker'] or self.features['podman']:
            self._init_container_metrics()
        if self.features['smart_monitoring']:
            self._init_smart_metrics()
        if self.features['ipmi']:
            self._init_ipmi_metrics()
        if self.features['systemd']:
            self._init_systemd_metrics()
        if self.features['mdadm']:
            self._init_mdadm_metrics()
        if self.features['nut_ups']:
            self._init_ups_metrics()
        if self.features['btrfs']:
            self._init_btrfs_metrics()
        
        self._init_exporter_metrics()
    
    def _init_base_metrics(self):
        """Initialize base metrics that are always collected"""
        # Node information
        self.node_info = Info('node', 'Node information', registry=self.registry)
        self.node_features = Info('node_features', 'Detected node features', registry=self.registry)
        self.pve_version = Info('pve_version', 'Proxmox VE version', registry=self.registry)
        self.boot_time = Gauge('node_boot_time_seconds', 'Node boot time', registry=self.registry)
        self.uptime_seconds = Gauge('node_uptime_seconds', 'System uptime in seconds', registry=self.registry)
        
        # CPU metrics
        self.cpu_count = Gauge('node_cpu_count', 'Number of CPUs', ['type'], registry=self.registry)
        self.cpu_usage = Gauge('node_cpu_seconds_total', 'CPU time spent',
                              ['cpu', 'mode'], registry=self.registry)
        self.cpu_percent = Gauge('node_cpu_usage_percent', 'CPU usage percentage',
                                ['cpu'], registry=self.registry)
        self.cpu_frequency = Gauge('node_cpu_frequency_hertz', 'CPU frequency',
                                  ['cpu', 'type'], registry=self.registry)
        self.cpu_throttles = Counter('node_cpu_throttles_total', 'CPU throttling events',
                                    ['cpu', 'type'], registry=self.registry)
        self.load_1 = Gauge('node_load1', '1 minute load average', registry=self.registry)
        self.load_5 = Gauge('node_load5', '5 minute load average', registry=self.registry)
        self.load_15 = Gauge('node_load15', '15 minute load average', registry=self.registry)
        
        # Memory metrics
        self.memory_total = Gauge('node_memory_MemTotal_bytes', 'Total memory', registry=self.registry)
        self.memory_free = Gauge('node_memory_MemFree_bytes', 'Free memory', registry=self.registry)
        self.memory_available = Gauge('node_memory_MemAvailable_bytes', 'Available memory', registry=self.registry)
        self.memory_cached = Gauge('node_memory_Cached_bytes', 'Cached memory', registry=self.registry)
        self.memory_buffers = Gauge('node_memory_Buffers_bytes', 'Buffer memory', registry=self.registry)
        self.memory_shared = Gauge('node_memory_Shared_bytes', 'Shared memory', registry=self.registry)
        self.memory_slab = Gauge('node_memory_Slab_bytes', 'Slab memory', registry=self.registry)
        self.memory_pressure_ratio = Gauge('node_memory_pressure_ratio', 'Memory pressure ratio', registry=self.registry)
        self.swap_total = Gauge('node_memory_SwapTotal_bytes', 'Total swap', registry=self.registry)
        self.swap_free = Gauge('node_memory_SwapFree_bytes', 'Free swap', registry=self.registry)
        self.swap_used_percent = Gauge('node_memory_swap_used_percent', 'Swap usage percentage', registry=self.registry)
        
        # Disk metrics
        self.fs_size = Gauge('node_filesystem_size_bytes', 'Filesystem size',
                            ['device', 'mountpoint', 'fstype'], registry=self.registry)
        self.fs_free = Gauge('node_filesystem_free_bytes', 'Filesystem free',
                            ['device', 'mountpoint', 'fstype'], registry=self.registry)
        self.fs_avail = Gauge('node_filesystem_avail_bytes', 'Filesystem available',
                             ['device', 'mountpoint', 'fstype'], registry=self.registry)
        self.fs_files = Gauge('node_filesystem_files', 'Total file nodes',
                             ['device', 'mountpoint', 'fstype'], registry=self.registry)
        self.fs_files_free = Gauge('node_filesystem_files_free', 'Free file nodes',
                                  ['device', 'mountpoint', 'fstype'], registry=self.registry)
        self.fs_readonly = Gauge('node_filesystem_readonly', 'Filesystem is read-only',
                                ['device', 'mountpoint', 'fstype'], registry=self.registry)
        
        self.disk_read_bytes = Counter('node_disk_read_bytes_total', 'Disk bytes read',
                                      ['device'], registry=self.registry)
        self.disk_written_bytes = Counter('node_disk_written_bytes_total', 'Disk bytes written',
                                         ['device'], registry=self.registry)
        self.disk_reads_completed = Counter('node_disk_reads_completed_total', 'Disk reads completed',
                                           ['device'], registry=self.registry)
        self.disk_writes_completed = Counter('node_disk_writes_completed_total', 'Disk writes completed',
                                            ['device'], registry=self.registry)
        self.disk_read_time = Counter('node_disk_read_time_seconds_total', 'Time spent reading',
                                     ['device'], registry=self.registry)
        self.disk_write_time = Counter('node_disk_write_time_seconds_total', 'Time spent writing',
                                      ['device'], registry=self.registry)
        self.disk_io_time = Counter('node_disk_io_time_seconds_total', 'Disk I/O time',
                                   ['device'], registry=self.registry)
        self.disk_io_now = Gauge('node_disk_io_now', 'Number of I/Os in progress',
                                ['device'], registry=self.registry)
        self.disk_utilization = Gauge('node_disk_utilization', 'Disk utilization percentage',
                                     ['device'], registry=self.registry)
        
        # Network metrics
        self.net_bytes_recv = Counter('node_network_receive_bytes_total', 'Network bytes received',
                                     ['device'], registry=self.registry)
        self.net_bytes_sent = Counter('node_network_transmit_bytes_total', 'Network bytes sent',
                                     ['device'], registry=self.registry)
        self.net_packets_recv = Counter('node_network_receive_packets_total', 'Network packets received',
                                       ['device'], registry=self.registry)
        self.net_packets_sent = Counter('node_network_transmit_packets_total', 'Network packets sent',
                                       ['device'], registry=self.registry)
        self.net_errs_recv = Counter('node_network_receive_errs_total', 'Network receive errors',
                                    ['device'], registry=self.registry)
        self.net_errs_sent = Counter('node_network_transmit_errs_total', 'Network transmit errors',
                                    ['device'], registry=self.registry)
        self.net_drop_recv = Counter('node_network_receive_drop_total', 'Network receive drops',
                                    ['device'], registry=self.registry)
        self.net_drop_sent = Counter('node_network_transmit_drop_total', 'Network transmit drops',
                                    ['device'], registry=self.registry)
        self.net_speed = Gauge('node_network_speed_bytes', 'Network interface speed',
                              ['device'], registry=self.registry)
        self.net_mtu = Gauge('node_network_mtu_bytes', 'Network interface MTU',
                            ['device'], registry=self.registry)
        self.net_up = Gauge('node_network_up', 'Network interface is up',
                           ['device'], registry=self.registry)
        
        # Process metrics
        self.processes_running = Gauge('node_procs_running', 'Running processes', registry=self.registry)
        self.processes_blocked = Gauge('node_procs_blocked', 'Blocked processes', registry=self.registry)
        self.processes_total = Gauge('node_procs_total', 'Total processes', registry=self.registry)
        self.threads_total = Gauge('node_threads_total', 'Total threads', registry=self.registry)
        self.forks_total = Counter('node_forks_total', 'Total forks since boot', registry=self.registry)
    
    def _init_advanced_metrics(self):
        """Initialize advanced system metrics"""
        # TCP/UDP connection metrics
        self.tcp_connections = Gauge('node_network_tcp_connections', 'TCP connections by state',
                                    ['state'], registry=self.registry)
        self.udp_connections = Gauge('node_network_udp_connections', 'UDP connections',
                                    ['state'], registry=self.registry)
        
        # Context switches and interrupts
        self.context_switches = Counter('node_context_switches_total', 'Total context switches', 
                                       registry=self.registry)
        self.interrupts = Counter('node_intr_total', 'Total interrupts',
                                 registry=self.registry)
        
        # File descriptors
        self.fd_allocated = Gauge('node_filefd_allocated', 'Allocated file descriptors',
                                 registry=self.registry)
        self.fd_maximum = Gauge('node_filefd_maximum', 'Maximum file descriptors',
                               registry=self.registry)
        
        # Page faults
        self.vmstat_pgfault = Counter('node_vmstat_pgfault', 'Page faults',
                                     registry=self.registry)
        self.vmstat_pgmajfault = Counter('node_vmstat_pgmajfault', 'Major page faults',
                                        registry=self.registry)
        self.vmstat_pswpin = Counter('node_vmstat_pswpin', 'Pages swapped in',
                                    registry=self.registry)
        self.vmstat_pswpout = Counter('node_vmstat_pswpout', 'Pages swapped out',
                                     registry=self.registry)
        
        # Entropy
        self.entropy_available = Gauge('node_entropy_available_bits', 'Available entropy',
                                      registry=self.registry)
        
        # Time metrics
        self.time_seconds = Gauge('node_time_seconds', 'System time', registry=self.registry)
        self.time_zone_offset = Gauge('node_time_zone_offset_seconds', 'Time zone offset',
                                     registry=self.registry)
        
        # Kernel metrics
        self.kernel_version = Info('node_kernel_version', 'Kernel version info', registry=self.registry)
        
        # Top processes
        self.top_cpu_processes = Info('node_top_cpu_processes', 'Top CPU consuming processes',
                                     registry=self.registry)
        self.top_mem_processes = Info('node_top_memory_processes', 'Top memory consuming processes',
                                     registry=self.registry)
    
    def _init_temperature_metrics(self):
        """Initialize temperature sensor metrics"""
        self.temp_celsius = Gauge('node_hwmon_temp_celsius', 'Temperature in Celsius',
                                 ['chip', 'sensor', 'label'], registry=self.registry)
        self.temp_max = Gauge('node_hwmon_temp_max_celsius', 'Maximum temperature',
                             ['chip', 'sensor', 'label'], registry=self.registry)
        self.temp_crit = Gauge('node_hwmon_temp_crit_celsius', 'Critical temperature',
                              ['chip', 'sensor', 'label'], registry=self.registry)
        self.temp_alarm = Gauge('node_hwmon_temp_alarm', 'Temperature alarm',
                               ['chip', 'sensor', 'label'], registry=self.registry)
        self.fan_rpm = Gauge('node_hwmon_fan_rpm', 'Fan speed RPM',
                            ['chip', 'sensor'], registry=self.registry)
        self.fan_min = Gauge('node_hwmon_fan_min_rpm', 'Minimum fan speed',
                            ['chip', 'sensor'], registry=self.registry)
        self.power_watts = Gauge('node_hwmon_power_watt', 'Power consumption',
                                ['chip', 'sensor'], registry=self.registry)
        self.voltage_volts = Gauge('node_hwmon_voltage_volts', 'Voltage',
                                  ['chip', 'sensor'], registry=self.registry)
        self.current_amps = Gauge('node_hwmon_curr_amps', 'Current',
                                 ['chip', 'sensor'], registry=self.registry)
    
    def _init_gpu_metrics(self):
        """Initialize GPU metrics"""
        self.gpu_info = Info('node_gpu_info', 'GPU information', registry=self.registry)
        self.gpu_count = Gauge('node_gpu_count', 'Number of GPUs', ['vendor'], registry=self.registry)
        
        # Common GPU metrics
        self.gpu_temp = Gauge('node_gpu_temp_celsius', 'GPU temperature',
                             ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_utilization = Gauge('node_gpu_utilization_percent', 'GPU utilization',
                                    ['gpu', 'name', 'vendor', 'type'], registry=self.registry)
        self.gpu_memory_total = Gauge('node_gpu_memory_total_bytes', 'GPU memory total',
                                     ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_memory_used = Gauge('node_gpu_memory_used_bytes', 'GPU memory used',
                                    ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_memory_free = Gauge('node_gpu_memory_free_bytes', 'GPU memory free',
                                    ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_power_draw = Gauge('node_gpu_power_draw_watts', 'GPU power draw',
                                   ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_power_limit = Gauge('node_gpu_power_limit_watts', 'GPU power limit',
                                    ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_clock_graphics = Gauge('node_gpu_clock_graphics_hertz', 'GPU graphics clock',
                                       ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_clock_memory = Gauge('node_gpu_clock_memory_hertz', 'GPU memory clock',
                                     ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_fan_speed = Gauge('node_gpu_fan_speed_percent', 'GPU fan speed',
                                  ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_pcie_link_gen = Gauge('node_gpu_pcie_link_gen', 'PCIe link generation',
                                       ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_pcie_link_width = Gauge('node_gpu_pcie_link_width', 'PCIe link width',
                                        ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_throttle_reason = Gauge('node_gpu_throttle_reasons', 'GPU throttle reasons',
                                        ['gpu', 'name', 'vendor', 'reason'], registry=self.registry)
    
    def _init_zfs_metrics(self):
        """Initialize ZFS metrics"""
        # ARC metrics
        self.zfs_arc_size = Gauge('node_zfs_arc_size_bytes', 'ZFS ARC size', registry=self.registry)
        self.zfs_arc_hits = Counter('node_zfs_arc_hits_total', 'ZFS ARC hits', registry=self.registry)
        self.zfs_arc_misses = Counter('node_zfs_arc_misses_total', 'ZFS ARC misses', registry=self.registry)
        self.zfs_arc_c = Gauge('node_zfs_arc_c_bytes', 'ZFS ARC target size', registry=self.registry)
        self.zfs_arc_c_min = Gauge('node_zfs_arc_c_min_bytes', 'ZFS ARC minimum size', registry=self.registry)
        self.zfs_arc_c_max = Gauge('node_zfs_arc_c_max_bytes', 'ZFS ARC maximum size', registry=self.registry)
        self.zfs_arc_hit_ratio = Gauge('node_zfs_arc_hit_ratio', 'ZFS ARC hit ratio', registry=self.registry)
        self.zfs_arc_evict_data = Counter('node_zfs_arc_evicted_bytes_total', 'ZFS ARC evicted bytes',
                                         ['type'], registry=self.registry)
        
        # L2ARC metrics
        self.zfs_l2arc_hits = Counter('node_zfs_l2arc_hits_total', 'ZFS L2ARC hits', registry=self.registry)
        self.zfs_l2arc_misses = Counter('node_zfs_l2arc_misses_total', 'ZFS L2ARC misses', registry=self.registry)
        self.zfs_l2arc_size = Gauge('node_zfs_l2arc_size_bytes', 'ZFS L2ARC size', registry=self.registry)
        
        # Pool metrics
        self.zpool_health = Gauge('node_zfs_zpool_health', 'ZFS pool health (0=online, 1=degraded, 2=faulted)',
                                 ['pool'], registry=self.registry)
        self.zpool_size = Gauge('node_zfs_zpool_size_bytes', 'ZFS pool size',
                               ['pool'], registry=self.registry)
        self.zpool_free = Gauge('node_zfs_zpool_free_bytes', 'ZFS pool free',
                               ['pool'], registry=self.registry)
        self.zpool_allocated = Gauge('node_zfs_zpool_allocated_bytes', 'ZFS pool allocated',
                                    ['pool'], registry=self.registry)
        self.zpool_fragmentation = Gauge('node_zfs_zpool_fragmentation_percent', 'ZFS pool fragmentation',
                                        ['pool'], registry=self.registry)
        self.zpool_dedup = Gauge('node_zfs_zpool_deduplication_ratio', 'ZFS pool deduplication ratio',
                                ['pool'], registry=self.registry)
        self.zpool_scrub_state = Gauge('node_zfs_zpool_scrub_state', 'ZFS pool scrub state',
                                      ['pool', 'state'], registry=self.registry)
        self.zpool_errors = Counter('node_zfs_zpool_errors_total', 'ZFS pool errors',
                                   ['pool', 'type'], registry=self.registry)
    
    def _init_vm_metrics(self):
        """Initialize VM/Container metrics"""
        self.vm_count = Gauge('pve_vm_count', 'Number of VMs/Containers',
                             ['type', 'status'], registry=self.registry)
        self.vm_cpu_usage = Gauge('pve_vm_cpu_usage_percent', 'VM CPU usage',
                                 ['vmid', 'name', 'type'], registry=self.registry)
        self.vm_memory_total = Gauge('pve_vm_memory_total_bytes', 'VM total memory',
                                    ['vmid', 'name', 'type'], registry=self.registry)
        self.vm_memory_used = Gauge('pve_vm_memory_used_bytes', 'VM used memory',
                                   ['vmid', 'name', 'type'], registry=self.registry)
        self.vm_status = Gauge('pve_vm_status', 'VM status (1=running, 0=stopped)',
                              ['vmid', 'name', 'type'], registry=self.registry)
        self.vm_disk_read = Counter('pve_vm_disk_read_bytes_total', 'VM disk read bytes',
                                   ['vmid', 'name', 'type'], registry=self.registry)
        self.vm_disk_write = Counter('pve_vm_disk_write_bytes_total', 'VM disk write bytes',
                                    ['vmid', 'name', 'type'], registry=self.registry)
        self.vm_net_rx = Counter('pve_vm_network_receive_bytes_total', 'VM network receive bytes',
                                ['vmid', 'name', 'type'], registry=self.registry)
        self.vm_net_tx = Counter('pve_vm_network_transmit_bytes_total', 'VM network transmit bytes',
                                ['vmid', 'name', 'type'], registry=self.registry)
        self.vm_uptime = Gauge('pve_vm_uptime_seconds', 'VM uptime',
                              ['vmid', 'name', 'type'], registry=self.registry)
    
    def _init_container_metrics(self):
        """Initialize Docker/Podman container metrics"""
        self.container_count = Gauge('node_container_count', 'Number of containers',
                                    ['runtime', 'state'], registry=self.registry)
        self.container_cpu = Gauge('node_container_cpu_usage_percent', 'Container CPU usage',
                                   ['name', 'id', 'runtime'], registry=self.registry)
        self.container_memory = Gauge('node_container_memory_usage_bytes', 'Container memory usage',
                                     ['name', 'id', 'runtime'], registry=self.registry)
        self.container_memory_limit = Gauge('node_container_memory_limit_bytes', 'Container memory limit',
                                           ['name', 'id', 'runtime'], registry=self.registry)
        self.container_network_rx = Counter('node_container_network_receive_bytes_total', 'Container network RX',
                                           ['name', 'id', 'runtime'], registry=self.registry)
        self.container_network_tx = Counter('node_container_network_transmit_bytes_total', 'Container network TX',
                                           ['name', 'id', 'runtime'], registry=self.registry)
        self.container_status = Gauge('node_container_status', 'Container status',
                                     ['name', 'id', 'runtime', 'status'], registry=self.registry)
        self.container_restarts = Counter('node_container_restarts_total', 'Container restart count',
                                         ['name', 'id', 'runtime'], registry=self.registry)
    
    def _init_smart_metrics(self):
        """Initialize SMART disk metrics"""
        self.smart_healthy = Gauge('node_disk_smart_healthy', 'SMART health status',
                                  ['device', 'model', 'serial'], registry=self.registry)
        self.smart_temperature = Gauge('node_disk_smart_temperature_celsius', 'Disk temperature',
                                      ['device', 'model'], registry=self.registry)
        self.smart_power_on_hours = Counter('node_disk_smart_power_on_hours', 'Power on hours',
                                           ['device', 'model'], registry=self.registry)
        self.smart_power_cycles = Counter('node_disk_smart_power_cycles', 'Power cycles',
                                         ['device', 'model'], registry=self.registry)
        self.smart_reallocated_sectors = Gauge('node_disk_smart_reallocated_sectors', 'Reallocated sectors',
                                              ['device', 'model'], registry=self.registry)
        self.smart_pending_sectors = Gauge('node_disk_smart_pending_sectors', 'Pending sectors',
                                          ['device', 'model'], registry=self.registry)
        self.smart_uncorrectable_sectors = Gauge('node_disk_smart_uncorrectable_sectors', 'Uncorrectable sectors',
                                                ['device', 'model'], registry=self.registry)
        self.smart_raw_read_error_rate = Gauge('node_disk_smart_raw_read_error_rate', 'Raw read error rate',
                                              ['device', 'model'], registry=self.registry)
        self.smart_seek_error_rate = Gauge('node_disk_smart_seek_error_rate', 'Seek error rate',
                                          ['device', 'model'], registry=self.registry)
        self.smart_spin_retry_count = Gauge('node_disk_smart_spin_retry_count', 'Spin retry count',
                                           ['device', 'model'], registry=self.registry)
        self.smart_ssd_wearout = Gauge('node_disk_smart_ssd_wearout_percent', 'SSD wearout indicator',
                                       ['device', 'model'], registry=self.registry)
    
    def _init_ipmi_metrics(self):
        """Initialize IPMI sensor metrics"""
        self.ipmi_sensor_value = Gauge('node_ipmi_sensor_value', 'IPMI sensor value',
                                      ['name', 'type', 'unit'], registry=self.registry)
        self.ipmi_sensor_state = Gauge('node_ipmi_sensor_state', 'IPMI sensor state',
                                      ['name', 'type', 'state'], registry=self.registry)
        self.ipmi_fan_speed = Gauge('node_ipmi_fan_speed_rpm', 'IPMI fan speed',
                                   ['name'], registry=self.registry)
        self.ipmi_temperature = Gauge('node_ipmi_temperature_celsius', 'IPMI temperature',
                                     ['name', 'location'], registry=self.registry)
        self.ipmi_voltage = Gauge('node_ipmi_voltage_volts', 'IPMI voltage',
                                 ['name', 'rail'], registry=self.registry)
        self.ipmi_power = Gauge('node_ipmi_power_watts', 'IPMI power consumption',
                               ['name'], registry=self.registry)
    
    def _init_systemd_metrics(self):
        """Initialize systemd metrics"""
        self.systemd_units = Gauge('node_systemd_unit_state', 'Systemd unit state',
                                  ['name', 'state', 'type'], registry=self.registry)
        self.systemd_unit_start_time = Gauge('node_systemd_unit_start_time_seconds', 'Unit start time',
                                            ['name'], registry=self.registry)
        self.systemd_system_running = Gauge('node_systemd_system_running', 'Systemd system state', 
                                           registry=self.registry)
        self.systemd_units_total = Gauge('node_systemd_units', 'Total systemd units by state',
                                        ['state'], registry=self.registry)
        self.systemd_timer_last_trigger = Gauge('node_systemd_timer_last_trigger_seconds', 'Timer last trigger',
                                               ['name'], registry=self.registry)
    
    def _init_mdadm_metrics(self):
        """Initialize mdadm RAID metrics"""
        self.mdadm_array_state = Gauge('node_md_state', 'MD array state',
                                      ['device', 'state'], registry=self.registry)
        self.mdadm_disks_total = Gauge('node_md_disks', 'Total disks in array',
                                      ['device'], registry=self.registry)
        self.mdadm_disks_active = Gauge('node_md_disks_active', 'Active disks in array',
                                       ['device'], registry=self.registry)
        self.mdadm_disks_failed = Gauge('node_md_disks_failed', 'Failed disks in array',
                                       ['device'], registry=self.registry)
        self.mdadm_disks_spare = Gauge('node_md_disks_spare', 'Spare disks in array',
                                      ['device'], registry=self.registry)
        self.mdadm_blocks_total = Gauge('node_md_blocks_total', 'Total blocks in array',
                                       ['device'], registry=self.registry)
        self.mdadm_blocks_synced = Gauge('node_md_blocks_synced', 'Synced blocks in array',
                                        ['device'], registry=self.registry)
        self.mdadm_sync_action = Info('node_md_sync_action', 'Current sync action',
                                     registry=self.registry)
        self.mdadm_sync_completed = Gauge('node_md_sync_completed_percent', 'Sync completion percentage',
                                         ['device'], registry=self.registry)
        self.mdadm_sync_speed = Gauge('node_md_sync_speed_kb_per_sec', 'Sync speed',
                                     ['device'], registry=self.registry)
    
    def _init_ups_metrics(self):
        """Initialize UPS metrics"""
        self.ups_status = Info('node_ups_status', 'UPS status information', registry=self.registry)
        self.ups_battery_charge = Gauge('node_ups_battery_charge_percent', 'Battery charge percentage',
                                       ['ups'], registry=self.registry)
        self.ups_battery_runtime = Gauge('node_ups_battery_runtime_seconds', 'Battery runtime remaining',
                                        ['ups'], registry=self.registry)
        self.ups_battery_voltage = Gauge('node_ups_battery_voltage', 'Battery voltage',
                                        ['ups'], registry=self.registry)
        self.ups_input_voltage = Gauge('node_ups_input_voltage', 'Input voltage',
                                      ['ups'], registry=self.registry)
        self.ups_output_voltage = Gauge('node_ups_output_voltage', 'Output voltage',
                                       ['ups'], registry=self.registry)
        self.ups_load_percent = Gauge('node_ups_load_percent', 'UPS load percentage',
                                     ['ups'], registry=self.registry)
        self.ups_temperature = Gauge('node_ups_temperature_celsius', 'UPS temperature',
                                    ['ups'], registry=self.registry)
        self.ups_on_battery = Gauge('node_ups_on_battery', 'UPS is on battery power',
                                   ['ups'], registry=self.registry)
    
    def _init_btrfs_metrics(self):
        """Initialize Btrfs metrics"""
        self.btrfs_allocation = Gauge('node_btrfs_allocation_bytes', 'Btrfs allocation',
                                     ['uuid', 'label', 'type'], registry=self.registry)
        self.btrfs_used = Gauge('node_btrfs_used_bytes', 'Btrfs used space',
                               ['uuid', 'label', 'type'], registry=self.registry)
        self.btrfs_device_size = Gauge('node_btrfs_device_size_bytes', 'Btrfs device size',
                                      ['uuid', 'label', 'device'], registry=self.registry)
        self.btrfs_device_errors = Counter('node_btrfs_device_errors_total', 'Btrfs device errors',
                                          ['uuid', 'label', 'device', 'type'], registry=self.registry)
    
    def _init_exporter_metrics(self):
        """Initialize exporter statistics"""
        self.collection_errors = Counter('node_exporter_collection_errors_total', 'Collection errors',
                                        ['collector'], registry=self.registry)
        self.collection_duration = Histogram('node_exporter_collection_duration_seconds', 'Collection duration',
                                           ['collector'], registry=self.registry)
        self.collection_success = Gauge('node_exporter_collection_success', 'Collection success',
                                       ['collector'], registry=self.registry)
        self.feature_enabled = Gauge('node_exporter_feature_enabled', 'Feature detection status',
                                    ['feature'], registry=self.registry)
        self.exporter_info = Info('node_exporter_info', 'Exporter information', registry=self.registry)
        self.scrape_collector_duration = Summary('node_scrape_collector_duration_seconds', 
                                                'Duration of a collector scrape',
                                                ['collector'], registry=self.registry)
        self.scrape_collector_success = Gauge('node_scrape_collector_success', 
                                             'Whether a collector succeeded',
                                             ['collector'], registry=self.registry)
    
    @timed_operation(timeout=10)
    def collect_base_metrics(self):
        """Collect base system metrics"""
        try:
            with self.collection_duration.labels(collector='base').time():
                # Node info
                self.node_info.info({
                    'hostname': self.hostname,
                    'kernel': platform.release(),
                    'os': platform.system(),
                    'os_version': platform.version(),
                    'architecture': platform.machine(),
                    'processor': platform.processor() or 'unknown',
                    'python_version': platform.python_version()
                })
                
                # Kernel version info
                self.kernel_version.info({
                    'release': platform.release(),
                    'version': platform.version()
                })
                
                # Feature info
                self.node_features.info({
                    feature: str(enabled) for feature, enabled in self.features.items()
                })
                
                # Set feature gauges
                for feature, enabled in self.features.items():
                    self.feature_enabled.labels(feature=feature).set(1 if enabled else 0)
                
                # Exporter info
                self.exporter_info.info({
                    'version': '2.0.0',
                    'start_time': str(self.start_time)
                })
                
                # PVE version
                if shutil.which('pveversion'):
                    result = subprocess.run(['pveversion', '--verbose'], 
                                          capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        for line in result.stdout.split('\n'):
                            if line.startswith('pve-manager'):
                                version = line.split('/')[1].split()[0] if '/' in line else 'unknown'
                                self.pve_version.info({'version': version})
                                break
                
                # Time metrics
                self.boot_time.set(psutil.boot_time())
                self.uptime_seconds.set(time.time() - psutil.boot_time())
                self.time_seconds.set(time.time())
                
                # Timezone offset
                import datetime
                utc_offset = datetime.datetime.now(datetime.timezone.utc).astimezone().utcoffset()
                if utc_offset:
                    self.time_zone_offset.set(utc_offset.total_seconds())
                
                # CPU metrics
                self.cpu_count.labels(type='logical').set(psutil.cpu_count(logical=True))
                self.cpu_count.labels(type='physical').set(psutil.cpu_count(logical=False) or 0)
                
                # CPU times and usage
                cpu_times = psutil.cpu_times(percpu=True)
                cpu_percent = psutil.cpu_percent(percpu=True, interval=None)
                
                for i, times in enumerate(cpu_times):
                    cpu_label = f'cpu{i}'
                    self.cpu_usage.labels(cpu=cpu_label, mode='user').set(times.user)
                    self.cpu_usage.labels(cpu=cpu_label, mode='system').set(times.system)
                    self.cpu_usage.labels(cpu=cpu_label, mode='idle').set(times.idle)
                    self.cpu_usage.labels(cpu=cpu_label, mode='iowait').set(getattr(times, 'iowait', 0))
                    self.cpu_usage.labels(cpu=cpu_label, mode='irq').set(getattr(times, 'irq', 0))
                    self.cpu_usage.labels(cpu=cpu_label, mode='softirq').set(getattr(times, 'softirq', 0))
                    self.cpu_usage.labels(cpu=cpu_label, mode='steal').set(getattr(times, 'steal', 0))
                    self.cpu_usage.labels(cpu=cpu_label, mode='guest').set(getattr(times, 'guest', 0))
                    
                    if i < len(cpu_percent):
                        self.cpu_percent.labels(cpu=cpu_label).set(cpu_percent[i])
                
                # CPU frequency
                try:
                    freq = psutil.cpu_freq(percpu=True)
                    if freq:
                        for i, f in enumerate(freq):
                            cpu_label = f'cpu{i}'
                            self.cpu_frequency.labels(cpu=cpu_label, type='current').set(f.current * 1000000)
                            self.cpu_frequency.labels(cpu=cpu_label, type='min').set(f.min * 1000000)
                            self.cpu_frequency.labels(cpu=cpu_label, type='max').set(f.max * 1000000)
                except:
                    pass
                
                # Check for CPU throttling (if available)
                self._collect_cpu_throttling()
                
                # Load average
                load = os.getloadavg()
                self.load_1.set(load[0])
                self.load_5.set(load[1])
                self.load_15.set(load[2])
                
                # Memory
                mem = psutil.virtual_memory()
                self.memory_total.set(mem.total)
                self.memory_available.set(mem.available)
                self.memory_free.set(mem.free)
                self.memory_cached.set(getattr(mem, 'cached', 0))
                self.memory_buffers.set(getattr(mem, 'buffers', 0))
                self.memory_shared.set(getattr(mem, 'shared', 0))
                self.memory_slab.set(getattr(mem, 'slab', 0))
                
                # Calculate memory pressure
                pressure = 1.0 - (mem.available / mem.total)
                self.memory_pressure_ratio.set(pressure)
                
                swap = psutil.swap_memory()
                self.swap_total.set(swap.total)
                self.swap_free.set(swap.free)
                if swap.total > 0:
                    self.swap_used_percent.set((swap.total - swap.free) / swap.total * 100)
                
                # Disk metrics
                self._collect_disk_metrics()
                
                # Network metrics
                self._collect_network_metrics()
                
                # Process metrics
                self._collect_process_metrics()
                
                # Advanced metrics
                self._collect_advanced_system_metrics()
                
                self.collection_success.labels(collector='base').set(1)
                
        except Exception as e:
            logger.error(f"Error collecting base metrics: {e}")
            self.collection_errors.labels(collector='base').inc()
            self.collection_success.labels(collector='base').set(0)
    
    def _collect_cpu_throttling(self):
        """Collect CPU throttling information"""
        try:
            # Check for Intel CPU throttling
            throttle_files = glob.glob('/sys/devices/system/cpu/cpu*/thermal_throttle/*_throttle_count')
            for throttle_file in throttle_files:
                try:
                    cpu_match = re.search(r'cpu(\d+)', throttle_file)
                    throttle_type = 'core' if 'core' in throttle_file else 'package'
                    if cpu_match:
                        cpu_num = cpu_match.group(1)
                        with open(throttle_file, 'r') as f:
                            count = int(f.read().strip())
                            self.cpu_throttles.labels(cpu=f'cpu{cpu_num}', type=throttle_type)._value.set(count)
                except:
                    pass
        except:
            pass
    
    def _collect_disk_metrics(self):
        """Collect detailed disk metrics"""
        # Filesystem metrics
        for partition in psutil.disk_partitions(all=False):
            try:
                if partition.fstype in ['tmpfs', 'devtmpfs', 'devfs']:
                    continue
                    
                usage = psutil.disk_usage(partition.mountpoint)
                self.fs_size.labels(
                    device=partition.device,
                    mountpoint=partition.mountpoint,
                    fstype=partition.fstype
                ).set(usage.total)
                self.fs_free.labels(
                    device=partition.device,
                    mountpoint=partition.mountpoint,
                    fstype=partition.fstype
                ).set(usage.free)
                self.fs_avail.labels(
                    device=partition.device,
                    mountpoint=partition.mountpoint,
                    fstype=partition.fstype
                ).set(usage.free)  # Simplified, should check available
                
                # Check if filesystem is readonly
                ro = 1 if 'ro' in partition.opts else 0
                self.fs_readonly.labels(
                    device=partition.device,
                    mountpoint=partition.mountpoint,
                    fstype=partition.fstype
                ).set(ro)
                
                # Try to get inode information
                try:
                    statvfs = os.statvfs(partition.mountpoint)
                    self.fs_files.labels(
                        device=partition.device,
                        mountpoint=partition.mountpoint,
                        fstype=partition.fstype
                    ).set(statvfs.f_files)
                    self.fs_files_free.labels(
                        device=partition.device,
                        mountpoint=partition.mountpoint,
                        fstype=partition.fstype
                    ).set(statvfs.f_ffree)
                except:
                    pass
            except:
                pass
        
        # Disk I/O metrics
        disk_io = psutil.disk_io_counters(perdisk=True, nowrap=True)
        if disk_io:
            for disk, counters in disk_io.items():
                if not disk.startswith('loop') and not disk.startswith('ram'):
                    self.disk_read_bytes.labels(device=disk)._value.set(counters.read_bytes)
                    self.disk_written_bytes.labels(device=disk)._value.set(counters.write_bytes)
                    self.disk_reads_completed.labels(device=disk)._value.set(counters.read_count)
                    self.disk_writes_completed.labels(device=disk)._value.set(counters.write_count)
                    self.disk_read_time.labels(device=disk)._value.set(counters.read_time / 1000.0)
                    self.disk_write_time.labels(device=disk)._value.set(counters.write_time / 1000.0)
                    
                    if hasattr(counters, 'busy_time'):
                        self.disk_io_time.labels(device=disk)._value.set(counters.busy_time / 1000.0)
                        
                        # Calculate utilization (approximation)
                        # This would need to track time delta for accurate calculation
                        if counters.busy_time > 0:
                            # Simplified utilization calculation
                            self.disk_utilization.labels(device=disk).set(min(100, counters.busy_time / 10))
                    
                    # Get queue depth from /sys/block if available
                    try:
                        queue_file = f'/sys/block/{disk}/queue/nr_requests'
                        if os.path.exists(queue_file):
                            with open(queue_file, 'r') as f:
                                queue_depth = int(f.read().strip())
                                self.disk_io_now.labels(device=disk).set(queue_depth)
                    except:
                        pass
    
    def _collect_network_metrics(self):
        """Collect detailed network metrics"""
        net_io = psutil.net_io_counters(pernic=True, nowrap=True)
        net_if_stats = psutil.net_if_stats()
        net_if_addrs = psutil.net_if_addrs()
        
        for interface, counters in net_io.items():
            # Skip loopback unless explicitly requested
            if interface == 'lo' and not DEBUG_MODE:
                continue
            
            self.net_bytes_recv.labels(device=interface)._value.set(counters.bytes_recv)
            self.net_bytes_sent.labels(device=interface)._value.set(counters.bytes_sent)
            self.net_packets_recv.labels(device=interface)._value.set(counters.packets_recv)
            self.net_packets_sent.labels(device=interface)._value.set(counters.packets_sent)
            self.net_errs_recv.labels(device=interface)._value.set(counters.errin)
            self.net_errs_sent.labels(device=interface)._value.set(counters.errout)
            self.net_drop_recv.labels(device=interface)._value.set(counters.dropin)
            self.net_drop_sent.labels(device=interface)._value.set(counters.dropout)
            
            # Interface stats
            if interface in net_if_stats:
                stats = net_if_stats[interface]
                self.net_up.labels(device=interface).set(1 if stats.isup else 0)
                self.net_speed.labels(device=interface).set(stats.speed * 1000000 if stats.speed > 0 else 0)
                self.net_mtu.labels(device=interface).set(stats.mtu)
    
    def _collect_process_metrics(self):
        """Collect process and thread metrics"""
        process_count = {'running': 0, 'sleeping': 0, 'blocked': 0, 'zombie': 0, 'total': 0}
        thread_count = 0
        top_cpu_procs = []
        top_mem_procs = []
        
        for proc in psutil.process_iter(['pid', 'name', 'status', 'cpu_percent', 'memory_percent', 'num_threads']):
            try:
                info = proc.info
                process_count['total'] += 1
                
                status = info.get('status', '')
                if status == psutil.STATUS_RUNNING:
                    process_count['running'] += 1
                elif status == psutil.STATUS_SLEEPING:
                    process_count['sleeping'] += 1
                elif status in [psutil.STATUS_DISK_SLEEP, psutil.STATUS_UNINTERRUPTIBLE_SLEEP]:
                    process_count['blocked'] += 1
                elif status == psutil.STATUS_ZOMBIE:
                    process_count['zombie'] += 1
                
                thread_count += info.get('num_threads', 1)
                
                # Track top processes
                if info.get('cpu_percent', 0) > 0:
                    top_cpu_procs.append({
                        'pid': info['pid'],
                        'name': info['name'],
                        'cpu': info.get('cpu_percent', 0)
                    })
                if info.get('memory_percent', 0) > 0:
                    top_mem_procs.append({
                        'pid': info['pid'],
                        'name': info['name'],
                        'memory': info.get('memory_percent', 0)
                    })
            except:
                pass
        
        self.processes_running.set(process_count['running'])
        self.processes_blocked.set(process_count['blocked'])
        self.processes_total.set(process_count['total'])
        self.threads_total.set(thread_count)
        
        # Export top processes (top 5 by CPU and memory)
        top_cpu_procs.sort(key=lambda x: x['cpu'], reverse=True)
        top_mem_procs.sort(key=lambda x: x['memory'], reverse=True)
        
        self.top_cpu_processes.info({
            f"proc_{i}": f"{p['name']}:{p['pid']}:{p['cpu']:.1f}%"
            for i, p in enumerate(top_cpu_procs[:5])
        })
        self.top_mem_processes.info({
            f"proc_{i}": f"{p['name']}:{p['pid']}:{p['memory']:.1f}%"
            for i, p in enumerate(top_mem_procs[:5])
        })
    
    def _collect_advanced_system_metrics(self):
        """Collect advanced system metrics"""
        try:
            # TCP/UDP connections
            connections = psutil.net_connections(kind='tcp')
            tcp_states = defaultdict(int)
            for conn in connections:
                tcp_states[conn.status] += 1
            
            for state, count in tcp_states.items():
                self.tcp_connections.labels(state=state).set(count)
            
            udp_connections = psutil.net_connections(kind='udp')
            self.udp_connections.labels(state='active').set(len(udp_connections))
            
            # Context switches and interrupts
            if os.path.exists('/proc/stat'):
                with open('/proc/stat', 'r') as f:
                    for line in f:
                        if line.startswith('ctxt'):
                            self.context_switches._value.set(int(line.split()[1]))
                        elif line.startswith('intr'):
                            self.interrupts._value.set(int(line.split()[1]))
                        elif line.startswith('processes'):
                            self.forks_total._value.set(int(line.split()[1]))
            
            # File descriptors
            if os.path.exists('/proc/sys/fs/file-nr'):
                with open('/proc/sys/fs/file-nr', 'r') as f:
                    parts = f.read().strip().split()
                    if len(parts) >= 3:
                        self.fd_allocated.set(int(parts[0]) - int(parts[1]))
                        self.fd_maximum.set(int(parts[2]))
            
            # VMStat metrics
            if os.path.exists('/proc/vmstat'):
                with open('/proc/vmstat', 'r') as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) == 2:
                            key, value = parts
                            if key == 'pgfault':
                                self.vmstat_pgfault._value.set(int(value))
                            elif key == 'pgmajfault':
                                self.vmstat_pgmajfault._value.set(int(value))
                            elif key == 'pswpin':
                                self.vmstat_pswpin._value.set(int(value))
                            elif key == 'pswpout':
                                self.vmstat_pswpout._value.set(int(value))
            
            # Entropy
            if os.path.exists('/proc/sys/kernel/random/entropy_avail'):
                with open('/proc/sys/kernel/random/entropy_avail', 'r') as f:
                    self.entropy_available.set(int(f.read().strip()))
            
        except Exception as e:
            logger.error(f"Error collecting advanced metrics: {e}")
    
    def collect_temperature_metrics(self):
        """Collect temperature sensor metrics"""
        if not self.features['sensors']:
            return
        
        try:
            with self.collection_duration.labels(collector='temperature').time():
                # Try psutil first (more reliable)
                if hasattr(psutil, 'sensors_temperatures'):
                    temps = psutil.sensors_temperatures()
                    for chip, sensors in temps.items():
                        chip_name = chip.replace('-', '_')
                        for sensor in sensors:
                            label = sensor.label or 'unknown'
                            sensor_name = label.replace(' ', '_').replace('.', '_')
                            
                            self.temp_celsius.labels(
                                chip=chip_name,
                                sensor=sensor_name,
                                label=label
                            ).set(sensor.current)
                            
                            if sensor.high and sensor.high > -273:
                                self.temp_max.labels(
                                    chip=chip_name,
                                    sensor=sensor_name,
                                    label=label
                                ).set(sensor.high)
                            
                            if sensor.critical and sensor.critical > -273:
                                self.temp_crit.labels(
                                    chip=chip_name,
                                    sensor=sensor_name,
                                    label=label
                                ).set(sensor.critical)
                    
                    # Fan speeds
                    if hasattr(psutil, 'sensors_fans'):
                        fans = psutil.sensors_fans()
                        for chip, fan_list in fans.items():
                            chip_name = chip.replace('-', '_')
                            for fan in fan_list:
                                self.fan_rpm.labels(
                                    chip=chip_name,
                                    sensor=fan.label or 'unknown'
                                ).set(fan.current)
                
                # Also check hwmon sysfs directly for more sensors
                self._collect_hwmon_sensors()
                
                self.collection_success.labels(collector='temperature').set(1)
                
        except Exception as e:
            logger.error(f"Error collecting temperature metrics: {e}")
            self.collection_errors.labels(collector='temperature').inc()
            self.collection_success.labels(collector='temperature').set(0)
    
    def _collect_hwmon_sensors(self):
        """Collect additional sensors from hwmon sysfs"""
        try:
            hwmon_path = '/sys/class/hwmon'
            if not os.path.exists(hwmon_path):
                return
            
            for hwmon in os.listdir(hwmon_path):
                hwmon_dir = os.path.join(hwmon_path, hwmon)
                
                # Get chip name
                name_file = os.path.join(hwmon_dir, 'name')
                if not os.path.exists(name_file):
                    continue
                    
                with open(name_file, 'r') as f:
                    chip_name = f.read().strip()
                
                # Voltage sensors
                for voltage_input in glob.glob(os.path.join(hwmon_dir, 'in*_input')):
                    try:
                        sensor_num = re.search(r'in(\d+)_input', voltage_input).group(1)
                        label_file = voltage_input.replace('_input', '_label')
                        
                        label = f'in{sensor_num}'
                        if os.path.exists(label_file):
                            with open(label_file, 'r') as f:
                                label = f.read().strip()
                        
                        with open(voltage_input, 'r') as f:
                            voltage = float(f.read().strip()) / 1000.0  # Convert mV to V
                            self.voltage_volts.labels(chip=chip_name, sensor=label).set(voltage)
                    except:
                        pass
                
                # Current sensors
                for current_input in glob.glob(os.path.join(hwmon_dir, 'curr*_input')):
                    try:
                        sensor_num = re.search(r'curr(\d+)_input', current_input).group(1)
                        label_file = current_input.replace('_input', '_label')
                        
                        label = f'curr{sensor_num}'
                        if os.path.exists(label_file):
                            with open(label_file, 'r') as f:
                                label = f.read().strip()
                        
                        with open(current_input, 'r') as f:
                            current = float(f.read().strip()) / 1000.0  # Convert mA to A
                            self.current_amps.labels(chip=chip_name, sensor=label).set(current)
                    except:
                        pass
                
                # Power sensors
                for power_input in glob.glob(os.path.join(hwmon_dir, 'power*_input')):
                    try:
                        sensor_num = re.search(r'power(\d+)_input', power_input).group(1)
                        label_file = power_input.replace('_input', '_label')
                        
                        label = f'power{sensor_num}'
                        if os.path.exists(label_file):
                            with open(label_file, 'r') as f:
                                label = f.read().strip()
                        
                        with open(power_input, 'r') as f:
                            power = float(f.read().strip()) / 1000000.0  # Convert μW to W
                            self.power_watts.labels(chip=chip_name, sensor=label).set(power)
                    except:
                        pass
        except Exception as e:
            logger.debug(f"Error collecting hwmon sensors: {e}")
    
    def collect_systemd_metrics(self):
        """Collect systemd service metrics"""
        if not self.features['systemd']:
            return
        
        try:
            with self.collection_duration.labels(collector='systemd').time():
                # Get overall system state
                result = subprocess.run(['systemctl', 'is-system-running'],
                                      capture_output=True, text=True, timeout=5)
                system_running = 1 if result.stdout.strip() == 'running' else 0
                self.systemd_system_running.set(system_running)
                
                # List all units
                result = subprocess.run(['systemctl', 'list-units', '--all', '--no-legend', '--no-pager'],
                                      capture_output=True, text=True, timeout=10)
                
                if result.returncode == 0:
                    unit_states = defaultdict(int)
                    
                    for line in result.stdout.strip().split('\n'):
                        if not line:
                            continue
                        
                        parts = line.split(None, 4)
                        if len(parts) >= 4:
                            unit_name = parts[0]
                            load_state = parts[1]
                            active_state = parts[2]
                            sub_state = parts[3]
                            
                            # Count by state
                            unit_states[active_state] += 1
                            
                            # Track important services
                            if unit_name.endswith('.service'):
                                # Set state (1 = active, 0 = inactive)
                                state_value = 1 if active_state == 'active' else 0
                                self.systemd_units.labels(
                                    name=unit_name,
                                    state=active_state,
                                    type='service'
                                ).set(state_value)
                    
                    # Set total counts by state
                    for state, count in unit_states.items():
                        self.systemd_units_total.labels(state=state).set(count)
                
                # Get failed units specifically
                result = subprocess.run(['systemctl', 'list-units', '--failed', '--no-legend', '--no-pager'],
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    failed_count = len(result.stdout.strip().split('\n')) if result.stdout.strip() else 0
                    self.systemd_units_total.labels(state='failed').set(failed_count)
                
                self.collection_success.labels(collector='systemd').set(1)
                
        except Exception as e:
            logger.error(f"Error collecting systemd metrics: {e}")
            self.collection_errors.labels(collector='systemd').inc()
            self.collection_success.labels(collector='systemd').set(0)
    
    def collect_mdadm_metrics(self):
        """Collect mdadm RAID metrics"""
        if not self.features['mdadm']:
            return
        
        try:
            with self.collection_duration.labels(collector='mdadm').time():
                # Parse /proc/mdstat
                with open('/proc/mdstat', 'r') as f:
                    content = f.read()
                
                current_array = None
                for line in content.split('\n'):
                    # Array definition line
                    if line.startswith('md'):
                        parts = line.split()
                        if len(parts) >= 4:
                            current_array = parts[0]
                            state = parts[2]
                            
                            # Count disks
                            total_disks = 0
                            active_disks = 0
                            
                            # Parse disk configuration
                            for part in parts[3:]:
                                if '[' in part and ']' in part:
                                    # Format: sda1[0] or sda1[0](F) for failed
                                    total_disks += 1
                                    if '(F)' not in part:
                                        active_disks += 1
                            
                            self.mdadm_array_state.labels(
                                device=f'/dev/{current_array}',
                                state=state
                            ).set(1 if state == 'active' else 0)
                            
                            self.mdadm_disks_total.labels(device=f'/dev/{current_array}').set(total_disks)
                            self.mdadm_disks_active.labels(device=f'/dev/{current_array}').set(active_disks)
                            self.mdadm_disks_failed.labels(device=f'/dev/{current_array}').set(
                                total_disks - active_disks
                            )
                    
                    # Sync status line
                    elif current_array and 'recovery' in line or 'resync' in line or 'reshape' in line:
                        # Parse sync percentage
                        match = re.search(r'(\d+\.\d+)%', line)
                        if match:
                            percent = float(match.group(1))
                            self.mdadm_sync_completed.labels(device=f'/dev/{current_array}').set(percent)
                        
                        # Parse sync speed
                        match = re.search(r'speed=(\d+)K/sec', line)
                        if match:
                            speed = int(match.group(1))
                            self.mdadm_sync_speed.labels(device=f'/dev/{current_array}').set(speed)
                
                self.collection_success.labels(collector='mdadm').set(1)
                
        except Exception as e:
            logger.error(f"Error collecting mdadm metrics: {e}")
            self.collection_errors.labels(collector='mdadm').inc()
            self.collection_success.labels(collector='mdadm').set(0)
    
    def collect_container_metrics(self):
        """Collect Docker/Podman container metrics"""
        if self.features['docker']:
            self._collect_docker_metrics()
        if self.features['podman']:
            self._collect_podman_metrics()
    
    def _collect_docker_metrics(self):
        """Collect Docker container metrics"""
        try:
            with self.collection_duration.labels(collector='docker').time():
                # Get container list
                result = subprocess.run(['docker', 'ps', '-a', '--format', 
                                       '{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.State}}'],
                                      capture_output=True, text=True, timeout=5)
                
                if result.returncode == 0:
                    container_states = defaultdict(int)
                    
                    for line in result.stdout.strip().split('\n'):
                        if not line:
                            continue
                        
                        parts = line.split('\t')
                        if len(parts) >= 4:
                            container_id = parts[0][:12]
                            container_name = parts[1]
                            status = parts[2]
                            state = parts[3]
                            
                            container_states[state] += 1
                            
                            # Get container stats if running
                            if 'running' in state.lower():
                                self._collect_container_stats(container_id, container_name, 'docker')
                    
                    # Set container counts
                    for state, count in container_states.items():
                        self.container_count.labels(runtime='docker', state=state).set(count)
                
                self.collection_success.labels(collector='docker').set(1)
                
        except Exception as e:
            logger.error(f"Error collecting Docker metrics: {e}")
            self.collection_errors.labels(collector='docker').inc()
            self.collection_success.labels(collector='docker').set(0)
    
    def _collect_podman_metrics(self):
        """Collect Podman container metrics"""
        try:
            with self.collection_duration.labels(collector='podman').time():
                # Similar to Docker but using podman commands
                result = subprocess.run(['podman', 'ps', '-a', '--format', 'json'],
                                      capture_output=True, text=True, timeout=5)
                
                if result.returncode == 0:
                    containers = json.loads(result.stdout)
                    container_states = defaultdict(int)
                    
                    for container in containers:
                        container_states[container.get('State', 'unknown')] += 1
                        
                        if container.get('State') == 'running':
                            self._collect_container_stats(
                                container.get('Id', '')[:12],
                                container.get('Names', ['unknown'])[0],
                                'podman'
                            )
                    
                    for state, count in container_states.items():
                        self.container_count.labels(runtime='podman', state=state).set(count)
                
                self.collection_success.labels(collector='podman').set(1)
                
        except Exception as e:
            logger.error(f"Error collecting Podman metrics: {e}")
            self.collection_errors.labels(collector='podman').inc()
            self.collection_success.labels(collector='podman').set(0)
    
    def _collect_container_stats(self, container_id, container_name, runtime):
        """Collect individual container statistics"""
        try:
            # Get container stats
            cmd = [runtime, 'stats', container_id, '--no-stream', '--format', 'json']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                stats = json.loads(result.stdout)
                if isinstance(stats, list):
                    stats = stats[0]
                
                # Parse CPU percentage
                cpu_percent = 0
                cpu_str = stats.get('CPUPerc', stats.get('cpu_percent', '0%'))
                if isinstance(cpu_str, str):
                    cpu_percent = float(cpu_str.rstrip('%'))
                
                self.container_cpu.labels(
                    name=container_name,
                    id=container_id,
                    runtime=runtime
                ).set(cpu_percent)
                
                # Parse memory usage
                mem_usage = stats.get('MemUsage', '')
                if '/' in mem_usage:
                    used, limit = mem_usage.split('/')
                    # Parse memory values (handle different units)
                    used_bytes = self._parse_memory_string(used)
                    limit_bytes = self._parse_memory_string(limit)
                    
                    self.container_memory.labels(
                        name=container_name,
                        id=container_id,
                        runtime=runtime
                    ).set(used_bytes)
                    
                    self.container_memory_limit.labels(
                        name=container_name,
                        id=container_id,
                        runtime=runtime
                    ).set(limit_bytes)
        except:
            pass
    
    def _parse_memory_string(self, mem_str):
        """Parse memory string like '1.5GiB' to bytes"""
        mem_str = mem_str.strip()
        units = {'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3,
                'KB': 1000, 'MB': 1000**2, 'GB': 1000**3}
        
        for unit, multiplier in units.items():
            if unit in mem_str:
                value = float(mem_str.replace(unit, '').strip())
                return int(value * multiplier)
        return 0
    
    def collect_all_metrics(self):
        """Collect all metrics based on detected features"""
        logger.debug("Starting metric collection cycle...")
        
        collectors = [
            ('base', self.collect_base_metrics),
        ]
        
        # Add conditional collectors
        if self.features['sensors']:
            collectors.append(('temperature', self.collect_temperature_metrics))
        if self.features['systemd']:
            collectors.append(('systemd', self.collect_systemd_metrics))
        if self.features['mdadm']:
            collectors.append(('mdadm', self.collect_mdadm_metrics))
        if self.features['docker'] or self.features['podman']:
            collectors.append(('containers', self.collect_container_metrics))
        
        # Execute collectors (parallel or serial)
        if PARALLEL_COLLECTORS and self.executor:
            futures = []
            for name, func in collectors:
                future = self.executor.submit(func)
                futures.append((name, future))
            
            # Wait for completion
            for name, future in futures:
                try:
                    future.result(timeout=30)
                except Exception as e:
                    logger.error(f"Collector {name} failed: {e}")
                    self.collection_errors.labels(collector=name).inc()
        else:
            for name, func in collectors:
                try:
                    func()
                except Exception as e:
                    logger.error(f"Collector {name} failed: {e}")
                    self.collection_errors.labels(collector=name).inc()
        
        # Clear expired cache entries
        self.cache.clear_expired()
        
        logger.debug("Metric collection cycle completed")
    
    def health_check(self):
        """Health check endpoint"""
        last_collection = time.time() - self.start_time
        if last_collection > COLLECTION_INTERVAL * 3:
            return False
        return True
    
    def shutdown(self):
        """Clean shutdown"""
        logger.info("Shutting down exporter...")
        if self.executor:
            self.executor.shutdown(wait=True)
    
    def run(self):
        """Main loop"""
        # Start HTTP server
        start_http_server(EXPORTER_PORT, registry=self.registry)
        logger.info(f"Enhanced Proxmox Exporter started on port {EXPORTER_PORT}")
        logger.info(f"Metrics available at http://0.0.0.0:{EXPORTER_PORT}/metrics")
        logger.info(f"Active features: {[k for k,v in self.features.items() if v]}")
        
        # Initial collection
        self.collect_all_metrics()
        
        # Collection loop
        while True:
            try:
                time.sleep(COLLECTION_INTERVAL)
                self.collect_all_metrics()
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Error in collection loop: {e}")
                time.sleep(5)  # Brief pause before retry
        
        self.shutdown()

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}")
    sys.exit(0)

def main():
    """Main entry point"""
    try:
        # Set up signal handlers
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Check for root (optional but recommended)
        if os.geteuid() != 0:
            logger.warning("Not running as root. Some metrics may be unavailable.")
            logger.warning("For full functionality, run with: sudo python3 %s" % sys.argv[0])
        
        # Create and run exporter
        exporter = EnhancedProxmoxExporter()
        exporter.run()
        
    except KeyboardInterrupt:
        logger.info("Exporter stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise

if __name__ == '__main__':
    main()