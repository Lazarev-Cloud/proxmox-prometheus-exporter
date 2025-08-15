#!/usr/bin/env python3
"""
Smart Adaptive Proxmox Node Exporter for Prometheus
Automatically detects and monitors available system components
Supports: CPU, Memory, Disk, Network, Temperature, GPU, ZFS, VMs/Containers
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
from collections import defaultdict
from prometheus_client import start_http_server, Gauge, Info, Counter, Histogram
from prometheus_client.core import CollectorRegistry
import logging
import sys

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
    format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Non-default port to avoid conflicts
EXPORTER_PORT = 9101

class SmartProxmoxExporter:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.hostname = socket.gethostname()
        
        # Feature detection flags
        self.features = {
            'sensors': False,
            'zfs': False,
            'nvidia_gpu': False,
            'amd_gpu': False,
            'intel_gpu': False,
            'qemu_vms': False,
            'lxc_containers': False,
            'smart_monitoring': False,
            'ipmi': False,
            'nvme': False,
            'systemd': False
        }
        
        # Detect available features
        self._detect_features()
        
        # Initialize metrics based on detected features
        self._init_base_metrics()
        
        # Initialize feature-specific metrics
        if self.features['sensors']:
            self._init_temperature_metrics()
        if self.features['zfs']:
            self._init_zfs_metrics()
        if any([self.features['nvidia_gpu'], self.features['amd_gpu'], self.features['intel_gpu']]):
            self._init_gpu_metrics()
        if self.features['qemu_vms'] or self.features['lxc_containers']:
            self._init_vm_metrics()
        if self.features['smart_monitoring']:
            self._init_smart_metrics()
        if self.features['ipmi']:
            self._init_ipmi_metrics()
        
        # Collection statistics
        self._init_exporter_metrics()
        
        logger.info(f"Smart Exporter initialized with features: {[k for k,v in self.features.items() if v]}")
    
    def _detect_features(self):
        """Detect available system features"""
        logger.info("Detecting system features...")
        
        # Check for temperature sensors
        if shutil.which('sensors'):
            try:
                result = subprocess.run(['sensors'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0 and len(result.stdout) > 10:
                    self.features['sensors'] = True
                    logger.info("✓ Temperature sensors detected")
            except:
                pass
        
        # Check for ZFS
        if os.path.exists('/proc/spl/kstat/zfs') or shutil.which('zpool'):
            try:
                result = subprocess.run(['zpool', 'list'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    self.features['zfs'] = True
                    logger.info("✓ ZFS detected")
            except:
                pass
        
        # Check for NVIDIA GPU
        if shutil.which('nvidia-smi'):
            try:
                result = subprocess.run(['nvidia-smi', '-L'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0 and 'GPU' in result.stdout:
                    self.features['nvidia_gpu'] = True
                    logger.info("✓ NVIDIA GPU detected")
            except:
                pass
        
        # Check for AMD GPU
        if os.path.exists('/sys/class/drm'):
            amd_cards = glob.glob('/sys/class/drm/card*/device/vendor')
            for vendor_file in amd_cards:
                try:
                    with open(vendor_file, 'r') as f:
                        vendor = f.read().strip()
                        if vendor == '0x1002':  # AMD vendor ID
                            self.features['amd_gpu'] = True
                            logger.info("✓ AMD GPU detected")
                            break
                except:
                    pass
        
        # Alternative AMD GPU detection via rocm-smi
        if not self.features['amd_gpu'] and shutil.which('rocm-smi'):
            try:
                result = subprocess.run(['rocm-smi', '--showid'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    self.features['amd_gpu'] = True
                    logger.info("✓ AMD GPU detected (ROCm)")
            except:
                pass
        
        # Check for Intel GPU
        if os.path.exists('/sys/class/drm'):
            intel_cards = glob.glob('/sys/class/drm/card*/device/vendor')
            for vendor_file in intel_cards:
                try:
                    with open(vendor_file, 'r') as f:
                        vendor = f.read().strip()
                        if vendor == '0x8086':  # Intel vendor ID
                            # Check if it's actually a GPU (not just integrated graphics)
                            card_dir = os.path.dirname(vendor_file)
                            if os.path.exists(os.path.join(card_dir, 'drm')):
                                self.features['intel_gpu'] = True
                                logger.info("✓ Intel GPU detected")
                                break
                except:
                    pass
        
        # Check for QEMU VMs
        if shutil.which('qm'):
            try:
                result = subprocess.run(['qm', 'list'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    self.features['qemu_vms'] = True
                    logger.info("✓ QEMU VM support detected")
            except:
                pass
        
        # Check for LXC containers
        if shutil.which('pct'):
            try:
                result = subprocess.run(['pct', 'list'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    self.features['lxc_containers'] = True
                    logger.info("✓ LXC container support detected")
            except:
                pass
        
        # Check for SMART monitoring
        if shutil.which('smartctl'):
            self.features['smart_monitoring'] = True
            logger.info("✓ SMART monitoring detected")
        
        # Check for IPMI
        if shutil.which('ipmitool'):
            try:
                result = subprocess.run(['ipmitool', 'sensor'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    self.features['ipmi'] = True
                    logger.info("✓ IPMI detected")
            except:
                pass
        
        # Check for NVMe devices
        if os.path.exists('/sys/class/nvme'):
            nvme_devices = glob.glob('/sys/class/nvme/nvme*')
            if nvme_devices:
                self.features['nvme'] = True
                logger.info("✓ NVMe devices detected")
        
        # Check for systemd
        if shutil.which('systemctl'):
            self.features['systemd'] = True
            logger.info("✓ Systemd detected")
    
    def _init_base_metrics(self):
        """Initialize base metrics that are always collected"""
        # Node information
        self.node_info = Info('node', 'Node information', registry=self.registry)
        self.node_features = Info('node_features', 'Detected node features', registry=self.registry)
        self.pve_version = Info('pve_version', 'Proxmox VE version', registry=self.registry)
        self.boot_time = Gauge('node_boot_time_seconds', 'Node boot time', registry=self.registry)
        
        # CPU metrics
        self.cpu_count = Gauge('node_cpu_count', 'Number of CPUs', ['type'], registry=self.registry)
        self.cpu_usage = Gauge('node_cpu_usage_seconds_total', 'CPU time spent',
                              ['cpu', 'mode'], registry=self.registry)
        self.cpu_percent = Gauge('node_cpu_usage_percent', 'CPU usage percentage',
                                ['cpu'], registry=self.registry)
        self.cpu_frequency = Gauge('node_cpu_frequency_hertz', 'CPU frequency',
                                  ['cpu'], registry=self.registry)
        self.load_1 = Gauge('node_load1', '1 minute load average', registry=self.registry)
        self.load_5 = Gauge('node_load5', '5 minute load average', registry=self.registry)
        self.load_15 = Gauge('node_load15', '15 minute load average', registry=self.registry)
        
        # Memory metrics
        self.memory_total = Gauge('node_memory_MemTotal_bytes', 'Total memory', registry=self.registry)
        self.memory_free = Gauge('node_memory_MemFree_bytes', 'Free memory', registry=self.registry)
        self.memory_available = Gauge('node_memory_MemAvailable_bytes', 'Available memory', registry=self.registry)
        self.memory_cached = Gauge('node_memory_Cached_bytes', 'Cached memory', registry=self.registry)
        self.memory_buffers = Gauge('node_memory_Buffers_bytes', 'Buffer memory', registry=self.registry)
        self.swap_total = Gauge('node_memory_SwapTotal_bytes', 'Total swap', registry=self.registry)
        self.swap_free = Gauge('node_memory_SwapFree_bytes', 'Free swap', registry=self.registry)
        
        # Disk metrics
        self.fs_size = Gauge('node_filesystem_size_bytes', 'Filesystem size',
                            ['device', 'mountpoint', 'fstype'], registry=self.registry)
        self.fs_free = Gauge('node_filesystem_free_bytes', 'Filesystem free',
                            ['device', 'mountpoint', 'fstype'], registry=self.registry)
        self.disk_read_bytes = Counter('node_disk_read_bytes_total', 'Disk bytes read',
                                      ['device'], registry=self.registry)
        self.disk_written_bytes = Counter('node_disk_written_bytes_total', 'Disk bytes written',
                                         ['device'], registry=self.registry)
        self.disk_io_time = Counter('node_disk_io_time_seconds_total', 'Disk I/O time',
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
        
        # Process metrics
        self.processes_running = Gauge('node_procs_running', 'Running processes', registry=self.registry)
        self.processes_total = Gauge('node_procs_total', 'Total processes', registry=self.registry)
        self.threads_total = Gauge('node_threads_total', 'Total threads', registry=self.registry)
    
    def _init_temperature_metrics(self):
        """Initialize temperature sensor metrics"""
        self.temp_celsius = Gauge('node_hwmon_temp_celsius', 'Temperature in Celsius',
                                 ['chip', 'sensor', 'label'], registry=self.registry)
        self.temp_max = Gauge('node_hwmon_temp_max_celsius', 'Maximum temperature',
                             ['chip', 'sensor', 'label'], registry=self.registry)
        self.temp_crit = Gauge('node_hwmon_temp_crit_celsius', 'Critical temperature',
                              ['chip', 'sensor', 'label'], registry=self.registry)
        self.fan_rpm = Gauge('node_hwmon_fan_rpm', 'Fan speed RPM',
                            ['chip', 'sensor'], registry=self.registry)
        self.power_watts = Gauge('node_hwmon_power_watt', 'Power consumption',
                                ['chip', 'sensor'], registry=self.registry)
    
    def _init_gpu_metrics(self):
        """Initialize GPU metrics"""
        self.gpu_info = Info('node_gpu_info', 'GPU information', registry=self.registry)
        self.gpu_count = Gauge('node_gpu_count', 'Number of GPUs', ['vendor'], registry=self.registry)
        
        # Common GPU metrics
        self.gpu_temp = Gauge('node_gpu_temp_celsius', 'GPU temperature',
                             ['gpu', 'name', 'vendor'], registry=self.registry)
        self.gpu_utilization = Gauge('node_gpu_utilization_percent', 'GPU utilization',
                                    ['gpu', 'name', 'vendor'], registry=self.registry)
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
        
        # NVIDIA specific
        if self.features['nvidia_gpu']:
            self.gpu_clock_graphics = Gauge('node_gpu_clock_graphics_hertz', 'GPU graphics clock',
                                           ['gpu', 'name'], registry=self.registry)
            self.gpu_clock_memory = Gauge('node_gpu_clock_memory_hertz', 'GPU memory clock',
                                         ['gpu', 'name'], registry=self.registry)
            self.gpu_fan_speed = Gauge('node_gpu_fan_speed_percent', 'GPU fan speed',
                                      ['gpu', 'name'], registry=self.registry)
            self.gpu_pcie_link_gen = Gauge('node_gpu_pcie_link_gen', 'PCIe link generation',
                                          ['gpu', 'name'], registry=self.registry)
            self.gpu_pcie_link_width = Gauge('node_gpu_pcie_link_width', 'PCIe link width',
                                            ['gpu', 'name'], registry=self.registry)
    
    def _init_zfs_metrics(self):
        """Initialize ZFS metrics"""
        self.zfs_arc_size = Gauge('node_zfs_arc_size_bytes', 'ZFS ARC size', registry=self.registry)
        self.zfs_arc_hits = Counter('node_zfs_arc_hits_total', 'ZFS ARC hits', registry=self.registry)
        self.zfs_arc_misses = Counter('node_zfs_arc_misses_total', 'ZFS ARC misses', registry=self.registry)
        self.zfs_arc_c = Gauge('node_zfs_arc_c_bytes', 'ZFS ARC target size', registry=self.registry)
        self.zfs_arc_c_max = Gauge('node_zfs_arc_c_max_bytes', 'ZFS ARC max size', registry=self.registry)
        
        self.zpool_health = Gauge('node_zfs_zpool_health', 'ZFS pool health',
                                 ['pool'], registry=self.registry)
        self.zpool_size = Gauge('node_zfs_zpool_size_bytes', 'ZFS pool size',
                               ['pool'], registry=self.registry)
        self.zpool_free = Gauge('node_zfs_zpool_free_bytes', 'ZFS pool free',
                               ['pool'], registry=self.registry)
        self.zpool_allocated = Gauge('node_zfs_zpool_allocated_bytes', 'ZFS pool allocated',
                                    ['pool'], registry=self.registry)
        self.zpool_fragmentation = Gauge('node_zfs_zpool_fragmentation_percent', 'ZFS pool fragmentation',
                                        ['pool'], registry=self.registry)
    
    def _init_vm_metrics(self):
        """Initialize VM/Container metrics"""
        self.vm_count = Gauge('pve_vm_count', 'Number of VMs/Containers',
                             ['type', 'status'], registry=self.registry)
        self.vm_cpu_usage = Gauge('pve_vm_cpu_usage_percent', 'VM CPU usage',
                                 ['vmid', 'name', 'type'], registry=self.registry)
        self.vm_memory_usage = Gauge('pve_vm_memory_bytes', 'VM memory usage',
                                    ['vmid', 'name', 'type'], registry=self.registry)
        self.vm_status = Gauge('pve_vm_status', 'VM status (1=running, 0=stopped)',
                              ['vmid', 'name', 'type'], registry=self.registry)
    
    def _init_smart_metrics(self):
        """Initialize SMART disk metrics"""
        self.smart_healthy = Gauge('node_disk_smart_healthy', 'SMART health status',
                                  ['device', 'model'], registry=self.registry)
        self.smart_temperature = Gauge('node_disk_smart_temperature_celsius', 'Disk temperature',
                                      ['device', 'model'], registry=self.registry)
        self.smart_power_on_hours = Counter('node_disk_smart_power_on_hours', 'Power on hours',
                                           ['device', 'model'], registry=self.registry)
        self.smart_power_cycles = Counter('node_disk_smart_power_cycles', 'Power cycles',
                                         ['device', 'model'], registry=self.registry)
    
    def _init_ipmi_metrics(self):
        """Initialize IPMI sensor metrics"""
        self.ipmi_sensor_value = Gauge('node_ipmi_sensor_value', 'IPMI sensor value',
                                      ['name', 'type', 'unit'], registry=self.registry)
        self.ipmi_sensor_state = Gauge('node_ipmi_sensor_state', 'IPMI sensor state',
                                      ['name', 'type'], registry=self.registry)
    
    def _init_exporter_metrics(self):
        """Initialize exporter statistics"""
        self.collection_errors = Counter('node_exporter_collection_errors_total', 'Collection errors',
                                        ['collector'], registry=self.registry)
        self.collection_duration = Histogram('node_exporter_collection_duration_seconds', 'Collection duration',
                                           ['collector'], registry=self.registry)
        self.feature_enabled = Gauge('node_exporter_feature_enabled', 'Feature detection status',
                                    ['feature'], registry=self.registry)
    
    def collect_base_metrics(self):
        """Collect base system metrics"""
        try:
            with self.collection_duration.labels(collector='base').time():
                # Node info
                self.node_info.info({
                    'hostname': self.hostname,
                    'kernel': platform.release(),
                    'os': platform.system(),
                    'architecture': platform.machine()
                })
                
                # Feature info
                self.node_features.info({
                    feature: str(enabled) for feature, enabled in self.features.items()
                })
                
                # Set feature gauges
                for feature, enabled in self.features.items():
                    self.feature_enabled.labels(feature=feature).set(1 if enabled else 0)
                
                # PVE version
                try:
                    if shutil.which('pveversion'):
                        result = subprocess.run(['pveversion', '--verbose'], 
                                              capture_output=True, text=True, timeout=2)
                        if result.returncode == 0:
                            for line in result.stdout.split('\n'):
                                if line.startswith('pve-manager'):
                                    version = line.split('/')[1].split()[0] if '/' in line else 'unknown'
                                    self.pve_version.info({'version': version})
                                    break
                except:
                    pass
                
                # Boot time
                self.boot_time.set(psutil.boot_time())
                
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
                    
                    if i < len(cpu_percent):
                        self.cpu_percent.labels(cpu=cpu_label).set(cpu_percent[i])
                
                # CPU frequency
                try:
                    freq = psutil.cpu_freq(percpu=True)
                    if freq:
                        for i, f in enumerate(freq):
                            self.cpu_frequency.labels(cpu=f'cpu{i}').set(f.current * 1000000)
                except:
                    pass
                
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
                
                swap = psutil.swap_memory()
                self.swap_total.set(swap.total)
                self.swap_free.set(swap.free)
                
                # Disk I/O
                for partition in psutil.disk_partitions(all=False):
                    try:
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
                    except:
                        pass
                
                disk_io = psutil.disk_io_counters(perdisk=True, nowrap=True)
                if disk_io:
                    for disk, counters in disk_io.items():
                        if not disk.startswith('loop') and not disk.startswith('ram'):
                            self.disk_read_bytes.labels(device=disk)._value.set(counters.read_bytes)
                            self.disk_written_bytes.labels(device=disk)._value.set(counters.write_bytes)
                            if hasattr(counters, 'busy_time'):
                                self.disk_io_time.labels(device=disk)._value.set(counters.busy_time / 1000.0)
                
                # Network I/O
                net_io = psutil.net_io_counters(pernic=True, nowrap=True)
                for interface, counters in net_io.items():
                    if interface != 'lo':
                        self.net_bytes_recv.labels(device=interface)._value.set(counters.bytes_recv)
                        self.net_bytes_sent.labels(device=interface)._value.set(counters.bytes_sent)
                        self.net_packets_recv.labels(device=interface)._value.set(counters.packets_recv)
                        self.net_packets_sent.labels(device=interface)._value.set(counters.packets_sent)
                        self.net_errs_recv.labels(device=interface)._value.set(counters.errin)
                        self.net_errs_sent.labels(device=interface)._value.set(counters.errout)
                
                # Process metrics
                process_count = {'running': 0, 'total': 0}
                thread_count = 0
                
                for proc in psutil.process_iter(['status', 'num_threads']):
                    try:
                        process_count['total'] += 1
                        if proc.info['status'] == psutil.STATUS_RUNNING:
                            process_count['running'] += 1
                        thread_count += proc.info.get('num_threads', 1)
                    except:
                        pass
                
                self.processes_running.set(process_count['running'])
                self.processes_total.set(process_count['total'])
                self.threads_total.set(thread_count)
                
        except Exception as e:
            logger.error(f"Error collecting base metrics: {e}")
            self.collection_errors.labels(collector='base').inc()
    
    def collect_temperature_metrics(self):
        """Collect temperature sensor metrics"""
        if not self.features['sensors']:
            return
        
        try:
            with self.collection_duration.labels(collector='temperature').time():
                # Try psutil first
                if hasattr(psutil, 'sensors_temperatures'):
                    temps = psutil.sensors_temperatures()
                    for chip, sensors in temps.items():
                        chip_name = chip.replace('-', '_')
                        for sensor in sensors:
                            label = sensor.label or 'unknown'
                            sensor_name = label.replace(' ', '_')
                            
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
                
                # Fallback to sensors command
                else:
                    result = subprocess.run(['sensors', '-u'], capture_output=True, text=True, timeout=5)
                    if result.returncode == 0:
                        self._parse_sensors_output(result.stdout)
                
        except Exception as e:
            logger.error(f"Error collecting temperature metrics: {e}")
            self.collection_errors.labels(collector='temperature').inc()
    
    def _parse_sensors_output(self, output):
        """Parse sensors command output"""
        current_chip = None
        current_sensor = None
        
        for line in output.split('\n'):
            line = line.strip()
            if not line:
                continue
            
            if not line.startswith(' ') and ':' not in line:
                current_chip = line.replace('-', '_')
                continue
            
            if ':' in line and current_chip:
                parts = line.split(':')
                key = parts[0].strip()
                value = parts[1].strip()
                
                if '_input' in key:
                    sensor_name = key.replace('_input', '')
                    try:
                        temp_value = float(value)
                        self.temp_celsius.labels(
                            chip=current_chip,
                            sensor=sensor_name,
                            label=sensor_name
                        ).set(temp_value)
                        current_sensor = sensor_name
                    except:
                        pass
                elif '_max' in key and current_sensor:
                    try:
                        temp_value = float(value)
                        if temp_value > -273:
                            self.temp_max.labels(
                                chip=current_chip,
                                sensor=current_sensor,
                                label=current_sensor
                            ).set(temp_value)
                    except:
                        pass
                elif '_crit' in key and current_sensor:
                    try:
                        temp_value = float(value)
                        if temp_value > -273:
                            self.temp_crit.labels(
                                chip=current_chip,
                                sensor=current_sensor,
                                label=current_sensor
                            ).set(temp_value)
                    except:
                        pass
    
    def collect_gpu_metrics(self):
        """Collect GPU metrics"""
        if self.features['nvidia_gpu']:
            self._collect_nvidia_gpu_metrics()
        if self.features['amd_gpu']:
            self._collect_amd_gpu_metrics()
        if self.features['intel_gpu']:
            self._collect_intel_gpu_metrics()
    
    def _collect_nvidia_gpu_metrics(self):
        """Collect NVIDIA GPU metrics using nvidia-smi"""
        try:
            with self.collection_duration.labels(collector='nvidia_gpu').time():
                # Get GPU count
                result = subprocess.run(
                    ['nvidia-smi', '--query-gpu=count', '--format=csv,noheader,nounits'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    gpu_count = len(result.stdout.strip().split('\n'))
                    self.gpu_count.labels(vendor='nvidia').set(gpu_count)
                
                # Query detailed GPU metrics
                query_fields = [
                    'index',
                    'name',
                    'temperature.gpu',
                    'utilization.gpu',
                    'utilization.memory',
                    'memory.total',
                    'memory.used',
                    'memory.free',
                    'power.draw',
                    'power.limit',
                    'clocks.current.graphics',
                    'clocks.current.memory',
                    'fan.speed',
                    'pcie.link.gen.current',
                    'pcie.link.width.current'
                ]
                
                result = subprocess.run(
                    ['nvidia-smi', f'--query-gpu={",".join(query_fields)}', '--format=csv,noheader,nounits'],
                    capture_output=True, text=True, timeout=5
                )
                
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n'):
                        values = [v.strip() for v in line.split(',')]
                        if len(values) >= len(query_fields):
                            gpu_idx = values[0]
                            gpu_name = values[1]
                            
                            # GPU info
                            self.gpu_info.info({
                                'gpu': gpu_idx,
                                'name': gpu_name,
                                'vendor': 'nvidia'
                            })
                            
                            # Temperature
                            try:
                                self.gpu_temp.labels(gpu=gpu_idx, name=gpu_name, vendor='nvidia').set(float(values[2]))
                            except:
                                pass
                            
                            # Utilization
                            try:
                                self.gpu_utilization.labels(gpu=gpu_idx, name=gpu_name, vendor='nvidia').set(float(values[3]))
                            except:
                                pass
                            
                            # Memory
                            try:
                                mem_total = float(values[5]) * 1024 * 1024  # Convert MB to bytes
                                mem_used = float(values[6]) * 1024 * 1024
                                mem_free = float(values[7]) * 1024 * 1024
                                
                                self.gpu_memory_total.labels(gpu=gpu_idx, name=gpu_name, vendor='nvidia').set(mem_total)
                                self.gpu_memory_used.labels(gpu=gpu_idx, name=gpu_name, vendor='nvidia').set(mem_used)
                                self.gpu_memory_free.labels(gpu=gpu_idx, name=gpu_name, vendor='nvidia').set(mem_free)
                            except:
                                pass
                            
                            # Power
                            try:
                                self.gpu_power_draw.labels(gpu=gpu_idx, name=gpu_name, vendor='nvidia').set(float(values[8]))
                                self.gpu_power_limit.labels(gpu=gpu_idx, name=gpu_name, vendor='nvidia').set(float(values[9]))
                            except:
                                pass
                            
                            # Clocks
                            try:
                                self.gpu_clock_graphics.labels(gpu=gpu_idx, name=gpu_name).set(float(values[10]) * 1000000)  # MHz to Hz
                                self.gpu_clock_memory.labels(gpu=gpu_idx, name=gpu_name).set(float(values[11]) * 1000000)
                            except:
                                pass
                            
                            # Fan speed
                            try:
                                self.gpu_fan_speed.labels(gpu=gpu_idx, name=gpu_name).set(float(values[12]))
                            except:
                                pass
                            
                            # PCIe info
                            try:
                                self.gpu_pcie_link_gen.labels(gpu=gpu_idx, name=gpu_name).set(float(values[13]))
                                self.gpu_pcie_link_width.labels(gpu=gpu_idx, name=gpu_name).set(float(values[14]))
                            except:
                                pass
                
        except Exception as e:
            logger.error(f"Error collecting NVIDIA GPU metrics: {e}")
            self.collection_errors.labels(collector='nvidia_gpu').inc()
    
    def _collect_amd_gpu_metrics(self):
        """Collect AMD GPU metrics"""
        try:
            with self.collection_duration.labels(collector='amd_gpu').time():
                # Try rocm-smi first
                if shutil.which('rocm-smi'):
                    self._collect_amd_gpu_rocm()
                else:
                    # Fallback to sysfs
                    self._collect_amd_gpu_sysfs()
        except Exception as e:
            logger.error(f"Error collecting AMD GPU metrics: {e}")
            self.collection_errors.labels(collector='amd_gpu').inc()
    
    def _collect_amd_gpu_rocm(self):
        """Collect AMD GPU metrics using rocm-smi"""
        try:
            # Get temperature
            result = subprocess.run(['rocm-smi', '--showtemp'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                # Parse output - format varies by version
                pass  # Implementation depends on rocm-smi version
            
            # Get utilization
            result = subprocess.run(['rocm-smi', '--showuse'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                # Parse output
                pass
            
            # Get memory
            result = subprocess.run(['rocm-smi', '--showmeminfo', 'vram'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                # Parse output
                pass
        except:
            pass
    
    def _collect_amd_gpu_sysfs(self):
        """Collect AMD GPU metrics from sysfs"""
        try:
            amd_cards = glob.glob('/sys/class/drm/card*/device')
            gpu_idx = 0
            
            for card_path in amd_cards:
                # Check if it's AMD
                vendor_file = os.path.join(card_path, 'vendor')
                if os.path.exists(vendor_file):
                    with open(vendor_file, 'r') as f:
                        if f.read().strip() != '0x1002':
                            continue
                
                gpu_name = f"amd_gpu_{gpu_idx}"
                
                # Temperature
                hwmon_path = glob.glob(os.path.join(card_path, 'hwmon/hwmon*'))
                if hwmon_path:
                    temp_file = os.path.join(hwmon_path[0], 'temp1_input')
                    if os.path.exists(temp_file):
                        with open(temp_file, 'r') as f:
                            temp = float(f.read().strip()) / 1000.0
                            self.gpu_temp.labels(gpu=str(gpu_idx), name=gpu_name, vendor='amd').set(temp)
                
                # GPU busy percent
                gpu_busy_file = os.path.join(card_path, 'gpu_busy_percent')
                if os.path.exists(gpu_busy_file):
                    with open(gpu_busy_file, 'r') as f:
                        utilization = float(f.read().strip())
                        self.gpu_utilization.labels(gpu=str(gpu_idx), name=gpu_name, vendor='amd').set(utilization)
                
                # Memory info
                mem_info_file = os.path.join(card_path, 'mem_info_vram_total')
                if os.path.exists(mem_info_file):
                    with open(mem_info_file, 'r') as f:
                        mem_total = int(f.read().strip())
                        self.gpu_memory_total.labels(gpu=str(gpu_idx), name=gpu_name, vendor='amd').set(mem_total)
                
                mem_used_file = os.path.join(card_path, 'mem_info_vram_used')
                if os.path.exists(mem_used_file):
                    with open(mem_used_file, 'r') as f:
                        mem_used = int(f.read().strip())
                        self.gpu_memory_used.labels(gpu=str(gpu_idx), name=gpu_name, vendor='amd').set(mem_used)
                
                gpu_idx += 1
            
            if gpu_idx > 0:
                self.gpu_count.labels(vendor='amd').set(gpu_idx)
                
        except Exception as e:
            logger.debug(f"Error reading AMD GPU sysfs: {e}")
    
    def _collect_intel_gpu_metrics(self):
        """Collect Intel GPU metrics"""
        try:
            with self.collection_duration.labels(collector='intel_gpu').time():
                # Intel GPU metrics are limited without intel_gpu_top
                intel_cards = glob.glob('/sys/class/drm/card*/device')
                gpu_idx = 0
                
                for card_path in intel_cards:
                    # Check if it's Intel
                    vendor_file = os.path.join(card_path, 'vendor')
                    if os.path.exists(vendor_file):
                        with open(vendor_file, 'r') as f:
                            if f.read().strip() != '0x8086':
                                continue
                    
                    gpu_name = f"intel_gpu_{gpu_idx}"
                    
                    # Try to get basic info from sysfs
                    # Intel GPUs have limited sysfs exposure
                    
                    gpu_idx += 1
                
                if gpu_idx > 0:
                    self.gpu_count.labels(vendor='intel').set(gpu_idx)
                    
        except Exception as e:
            logger.error(f"Error collecting Intel GPU metrics: {e}")
            self.collection_errors.labels(collector='intel_gpu').inc()
    
    def collect_zfs_metrics(self):
        """Collect ZFS metrics"""
        if not self.features['zfs']:
            return
        
        try:
            with self.collection_duration.labels(collector='zfs').time():
                # ARC stats
                arc_stats_file = '/proc/spl/kstat/zfs/arcstats'
                if os.path.exists(arc_stats_file):
                    with open(arc_stats_file, 'r') as f:
                        for line in f:
                            parts = line.split()
                            if len(parts) >= 3:
                                if parts[0] == 'size':
                                    self.zfs_arc_size.set(int(parts[2]))
                                elif parts[0] == 'hits':
                                    self.zfs_arc_hits._value.set(int(parts[2]))
                                elif parts[0] == 'misses':
                                    self.zfs_arc_misses._value.set(int(parts[2]))
                                elif parts[0] == 'c':
                                    self.zfs_arc_c.set(int(parts[2]))
                                elif parts[0] == 'c_max':
                                    self.zfs_arc_c_max.set(int(parts[2]))
                
                # ZPool status
                result = subprocess.run(['zpool', 'list', '-Hp'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if line.strip():
                            parts = line.split('\t')
                            if len(parts) >= 10:
                                pool = parts[0]
                                size = int(parts[1])
                                alloc = int(parts[2])
                                free = int(parts[3])
                                frag = parts[6].rstrip('%') if parts[6] != '-' else '0'
                                health = parts[9]
                                
                                self.zpool_size.labels(pool=pool).set(size)
                                self.zpool_allocated.labels(pool=pool).set(alloc)
                                self.zpool_free.labels(pool=pool).set(free)
                                self.zpool_fragmentation.labels(pool=pool).set(float(frag))
                                
                                health_value = 0
                                if health == 'DEGRADED':
                                    health_value = 1
                                elif health == 'FAULTED':
                                    health_value = 2
                                self.zpool_health.labels(pool=pool).set(health_value)
                
        except Exception as e:
            logger.error(f"Error collecting ZFS metrics: {e}")
            self.collection_errors.labels(collector='zfs').inc()
    
    def collect_vm_metrics(self):
        """Collect VM and container metrics"""
        try:
            with self.collection_duration.labels(collector='vm').time():
                # QEMU VMs
                if self.features['qemu_vms']:
                    try:
                        result = subprocess.run(['qm', 'list'], capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            vm_total = 0
                            vm_running = 0
                            
                            for line in result.stdout.split('\n')[1:]:
                                if line.strip():
                                    parts = line.split()
                                    if len(parts) >= 3:
                                        vmid = parts[0]
                                        name = parts[1]
                                        status = parts[2]
                                        
                                        vm_total += 1
                                        is_running = 1 if status == 'running' else 0
                                        vm_running += is_running
                                        
                                        self.vm_status.labels(vmid=vmid, name=name, type='qemu').set(is_running)
                                        
                                        # Get detailed stats if running
                                        if is_running:
                                            self._collect_vm_stats(vmid, name, 'qemu')
                            
                            self.vm_count.labels(type='qemu', status='total').set(vm_total)
                            self.vm_count.labels(type='qemu', status='running').set(vm_running)
                    except:
                        pass
                
                # LXC Containers
                if self.features['lxc_containers']:
                    try:
                        result = subprocess.run(['pct', 'list'], capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            ct_total = 0
                            ct_running = 0
                            
                            for line in result.stdout.split('\n')[1:]:
                                if line.strip():
                                    parts = line.split()
                                    if len(parts) >= 3:
                                        vmid = parts[0]
                                        status = parts[1]
                                        name = parts[2] if len(parts) > 2 else vmid
                                        
                                        ct_total += 1
                                        is_running = 1 if status == 'running' else 0
                                        ct_running += is_running
                                        
                                        self.vm_status.labels(vmid=vmid, name=name, type='lxc').set(is_running)
                                        
                                        # Get detailed stats if running
                                        if is_running:
                                            self._collect_vm_stats(vmid, name, 'lxc')
                            
                            self.vm_count.labels(type='lxc', status='total').set(ct_total)
                            self.vm_count.labels(type='lxc', status='running').set(ct_running)
                    except:
                        pass
                
        except Exception as e:
            logger.error(f"Error collecting VM metrics: {e}")
            self.collection_errors.labels(collector='vm').inc()
    
    def _collect_vm_stats(self, vmid, name, vm_type):
        """Collect individual VM/Container stats"""
        try:
            if vm_type == 'qemu':
                # Try to get QEMU VM stats from QMP or config
                pass  # Implementation would require QMP socket access
            elif vm_type == 'lxc':
                # Try to get LXC stats from cgroup
                pass  # Implementation would require cgroup parsing
        except:
            pass
    
    def collect_smart_metrics(self):
        """Collect SMART disk metrics"""
        if not self.features['smart_monitoring']:
            return
        
        try:
            with self.collection_duration.labels(collector='smart').time():
                # Get list of block devices
                block_devices = []
                for device in os.listdir('/sys/block'):
                    if device.startswith(('sd', 'nvme', 'hd')):
                        block_devices.append(f'/dev/{device}')
                
                for device in block_devices:
                    try:
                        result = subprocess.run(
                            ['smartctl', '-A', '-H', '-i', device, '--json'],
                            capture_output=True, text=True, timeout=5
                        )
                        
                        if result.returncode in [0, 4]:  # 4 = SMART support not enabled
                            data = json.loads(result.stdout)
                            
                            # Device info
                            model = data.get('model_name', 'unknown')
                            
                            # Health status
                            smart_status = data.get('smart_status', {})
                            passed = smart_status.get('passed', False)
                            self.smart_healthy.labels(device=device, model=model).set(1 if passed else 0)
                            
                            # Temperature
                            temp = data.get('temperature', {}).get('current')
                            if temp:
                                self.smart_temperature.labels(device=device, model=model).set(temp)
                            
                            # Power on hours
                            attrs = data.get('ata_smart_attributes', {}).get('table', [])
                            for attr in attrs:
                                if attr.get('name') == 'Power_On_Hours':
                                    hours = attr.get('raw', {}).get('value', 0)
                                    self.smart_power_on_hours.labels(device=device, model=model)._value.set(hours)
                                elif attr.get('name') == 'Power_Cycle_Count':
                                    cycles = attr.get('raw', {}).get('value', 0)
                                    self.smart_power_cycles.labels(device=device, model=model)._value.set(cycles)
                    except:
                        pass
                        
        except Exception as e:
            logger.error(f"Error collecting SMART metrics: {e}")
            self.collection_errors.labels(collector='smart').inc()
    
    def collect_ipmi_metrics(self):
        """Collect IPMI sensor metrics"""
        if not self.features['ipmi']:
            return
        
        try:
            with self.collection_duration.labels(collector='ipmi').time():
                result = subprocess.run(
                    ['ipmitool', 'sensor'],
                    capture_output=True, text=True, timeout=10
                )
                
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if '|' in line:
                            parts = [p.strip() for p in line.split('|')]
                            if len(parts) >= 3:
                                name = parts[0]
                                value = parts[1]
                                unit = parts[2]
                                
                                # Try to parse numeric value
                                try:
                                    if value and value != 'na':
                                        numeric_value = float(value.split()[0])
                                        self.ipmi_sensor_value.labels(
                                            name=name,
                                            type='sensor',
                                            unit=unit
                                        ).set(numeric_value)
                                except:
                                    pass
                                    
        except Exception as e:
            logger.error(f"Error collecting IPMI metrics: {e}")
            self.collection_errors.labels(collector='ipmi').inc()
    
    def collect_all_metrics(self):
        """Collect all metrics based on detected features"""
        logger.debug("Starting metric collection cycle...")
        
        # Always collect base metrics
        self.collect_base_metrics()
        
        # Collect feature-specific metrics
        if self.features['sensors']:
            self.collect_temperature_metrics()
        
        if any([self.features['nvidia_gpu'], self.features['amd_gpu'], self.features['intel_gpu']]):
            self.collect_gpu_metrics()
        
        if self.features['zfs']:
            self.collect_zfs_metrics()
        
        if self.features['qemu_vms'] or self.features['lxc_containers']:
            self.collect_vm_metrics()
        
        if self.features['smart_monitoring']:
            self.collect_smart_metrics()
        
        if self.features['ipmi']:
            self.collect_ipmi_metrics()
        
        logger.debug("Metric collection cycle completed")
    
    def run(self):
        """Main loop"""
        # Start HTTP server
        start_http_server(EXPORTER_PORT, registry=self.registry)
        logger.info(f"Smart Proxmox Exporter started on port {EXPORTER_PORT}")
        logger.info(f"Metrics available at http://0.0.0.0:{EXPORTER_PORT}/metrics")
        logger.info(f"Active features: {[k for k,v in self.features.items() if v]}")
        
        # Initial collection
        self.collect_all_metrics()
        
        # Collection loop
        while True:
            time.sleep(15)
            self.collect_all_metrics()

def main():
    """Main entry point"""
    try:
        # Check for root (optional but recommended)
        if os.geteuid() != 0:
            logger.warning("Not running as root. Some metrics may be unavailable.")
            logger.warning("For full functionality, run with: sudo python3 %s" % sys.argv[0])
        
        # Create and run exporter
        exporter = SmartProxmoxExporter()
        exporter.run()
        
    except KeyboardInterrupt:
        logger.info("Exporter stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise

if __name__ == '__main__':
    main()