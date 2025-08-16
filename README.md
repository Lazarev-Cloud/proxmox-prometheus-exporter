# Smart Adaptive Proxmox Node Exporter

[![License: BSD-3-Clause](https://img.shields.io/badge/License-BSD%203--Clause-blue.svg)](https://opensource.org/licenses/BSD-3-Clause)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)
[![Prometheus](https://img.shields.io/badge/Prometheus-compatible-orange.svg)](https://prometheus.io/)

> **A comprehensive, intelligent monitoring solution for Proxmox VE clusters that automatically detects and monitors available system components.**

## 🎯 Overview

The Smart Adaptive Proxmox Node Exporter is a next-generation monitoring solution designed specifically for Proxmox VE environments. Unlike traditional exporters that require manual configuration, this exporter intelligently detects available hardware and software components, automatically adapting its monitoring capabilities to your specific infrastructure.

### 🔍 Key Differentiators

- **🧠 Intelligent Detection**: Automatically discovers GPUs, sensors, ZFS pools, VMs, and more
- **🔧 Zero Configuration**: Works out-of-the-box with minimal setup
- **⚡ Lightweight**: ~50MB memory footprint, <1% CPU usage
- **🎮 GPU Support**: Native NVIDIA, AMD, and Intel GPU monitoring
- **🏠 Homelab Optimized**: Perfect for home labs and small clusters
- **📊 Rich Metrics**: 100+ metrics covering all aspects of your infrastructure

### 🏗️ Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Prometheus    │◄───┤  Smart Exporter  │───►│   Grafana       │
│   (Scraping)    │    │   (Port 9101)    │    │  (Visualization)│
└─────────────────┘    └──────────────────┘    └─────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │                   │
            ┌───────▼────────┐ ┌────────▼────────┐
            │ Hardware Layer │ │ Software Layer  │
            │ • Temperature  │ │ • Proxmox VMs   │
            │ • GPUs         │ │ • LXC Containers│
            │ • SMART Disks  │ │ • ZFS Pools     │
            │ • IPMI         │ │ • System Stats  │
            └────────────────┘ └─────────────────┘
```


## Features

The exporter automatically detects and monitors:
- ✅ **CPU, Memory, Disk, Network** (always collected)
- 🌡️ **Temperature sensors** (if lm-sensors available)
- 🎮 **GPU metrics** (NVIDIA, AMD, Intel if present)
- 💾 **ZFS pools** (if ZFS is used)
- 🖥️ **VMs and Containers** (if Proxmox)
- 💿 **SMART disk health** (if smartctl available)
- 🔧 **IPMI sensors** (if ipmitool available)

## Quick Start

### 1. Install Base Dependencies
```bash
# Core dependencies (always needed)
apt update
apt install -y python3-pip
pip3 install prometheus-client psutil

# Optional but recommended
apt install -y lm-sensors sysstat smartmontools nvme-cli

# Auto-detect sensors
sensors-detect --auto 2>/dev/null || true

# Load kernel modules
modprobe coretemp 2>/dev/null || true
modprobe k10temp 2>/dev/null || true  # AMD CPUs
modprobe nct6775 2>/dev/null || true  # Some motherboards
```

### 2. GPU Support (Optional)

#### For NVIDIA GPUs:
```bash
# Check if NVIDIA GPU exists
lspci | grep -i nvidia

# If NVIDIA GPU present, ensure nvidia-smi is available
nvidia-smi --query-gpu=name --format=csv,noheader

# The exporter will auto-detect and collect NVIDIA metrics
```

#### For AMD GPUs:
```bash
# Check if AMD GPU exists
lspci | grep -i amd | grep -i vga

# Option 1: Install ROCm (if not already installed)
# Follow AMD ROCm installation guide for your distro

# Option 2: The exporter will use sysfs (basic metrics)
# No additional setup needed
```

#### For Intel GPUs:
```bash
# Check if Intel GPU exists (discrete, not integrated)
lspci | grep -i intel | grep -i vga

# Install intel-gpu-tools for better metrics (optional)
apt install -y intel-gpu-tools
```

### 2. Deploy the Exporter
```bash
# Create directory for the exporter
mkdir -p /opt/proxmox-exporter

# Copy the Python script to the server
cat > /opt/proxmox-exporter/node_exporter.py << 'EOF'
# [Paste the Python script here]
EOF

# Make it executable
chmod +x /opt/proxmox-exporter/node_exporter.py
```

### 3. Create Systemd Service
```bash
cat > /etc/systemd/system/proxmox-node-exporter.service << 'EOF'
[Unit]
Description=Proxmox Node Exporter for Prometheus
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/proxmox-exporter
ExecStart=/usr/bin/python3 /opt/proxmox-exporter/node_exporter.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

### 4. Start and Enable the Service
```bash
# Reload systemd
systemctl daemon-reload

# Enable auto-start on boot
systemctl enable proxmox-node-exporter.service

# Start the service
systemctl start proxmox-node-exporter.service

# Check status
systemctl status proxmox-node-exporter.service

# View logs
journalctl -u proxmox-node-exporter.service -f
```

### 5. Verify It's Working
```bash
# Test locally
curl http://localhost:9101/metrics | grep -E "node_hwmon_temp|node_load|pve_version"
```

## Prometheus Configuration

Add this to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'proxmox-nodes'
    static_configs:
      - targets:
        - 'dasha:9101'
        - 'eva:9101'
        - 'helen:9101'
        - 'polly:9101'
    scrape_interval: 30s
    scrape_timeout: 10s
```

## Deployment Script (Smart Auto-Setup)

Save this as `deploy_smart_exporter.sh`:

```bash
#!/bin/bash

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Smart Adaptive Proxmox Node Exporter${NC}"
echo -e "${BLUE}========================================${NC}"

# Detect system features
echo -e "\n${YELLOW}Detecting system features...${NC}"

# Base dependencies
echo -e "${GREEN}Installing base dependencies...${NC}"
apt update
apt install -y python3-pip
pip3 install -q prometheus-client psutil

# Optional dependencies (install what's available)
echo -e "${YELLOW}Installing optional components...${NC}"

# Temperature sensors
if command -v sensors &> /dev/null || apt install -y lm-sensors 2>/dev/null; then
    echo -e "${GREEN}✓ Temperature sensors support installed${NC}"
    sensors-detect --auto 2>/dev/null || true
    modprobe coretemp 2>/dev/null || true
    modprobe k10temp 2>/dev/null || true
fi

# SMART monitoring
if command -v smartctl &> /dev/null || apt install -y smartmontools 2>/dev/null; then
    echo -e "${GREEN}✓ SMART disk monitoring installed${NC}"
fi

# GPU detection
if lspci | grep -qi nvidia; then
    echo -e "${GREEN}✓ NVIDIA GPU detected${NC}"
    if command -v nvidia-smi &> /dev/null; then
        echo "  nvidia-smi available"
    else
        echo -e "${YELLOW}  nvidia-smi not found - install NVIDIA drivers for GPU metrics${NC}"
    fi
fi

if lspci | grep -E -qi "VGA.*AMD|Display.*AMD"; then
    echo -e "${GREEN}✓ AMD GPU detected${NC}"
fi

if lspci | grep -E -qi "VGA.*Intel|Display.*Intel" | grep -v "HD Graphics"; then
    echo -e "${GREEN}✓ Intel GPU detected${NC}"
fi

# ZFS detection
if command -v zpool &> /dev/null; then
    echo -e "${GREEN}✓ ZFS storage detected${NC}"
fi

# Proxmox detection
if command -v qm &> /dev/null; then
    echo -e "${GREEN}✓ Proxmox QEMU support detected${NC}"
fi

if command -v pct &> /dev/null; then
    echo -e "${GREEN}✓ Proxmox LXC support detected${NC}"
fi

# IPMI detection
if command -v ipmitool &> /dev/null; then
    echo -e "${GREEN}✓ IPMI support detected${NC}"
fi

# Create directory
mkdir -p /opt/proxmox-exporter

# Create the exporter script
echo -e "\n${YELLOW}Installing exporter...${NC}"
cat > /opt/proxmox-exporter/node_exporter.py << 'EXPORTER_EOF'
# [Insert the full Python script here]
EXPORTER_EOF

chmod +x /opt/proxmox-exporter/node_exporter.py

# Create systemd service
cat > /etc/systemd/system/proxmox-node-exporter.service << 'EOF'
[Unit]
Description=Smart Adaptive Proxmox Node Exporter
After=network.target
Wants=lm-sensors.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/proxmox-exporter
ExecStart=/usr/bin/python3 /opt/proxmox-exporter/node_exporter.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Start service
systemctl daemon-reload
systemctl enable proxmox-node-exporter.service
systemctl start proxmox-node-exporter.service

# Wait for startup
sleep 3

# Check status
if systemctl is-active --quiet proxmox-node-exporter.service; then
    echo -e "\n${GREEN}✓ Exporter is running successfully!${NC}"
    
    # Show detected features
    echo -e "\n${YELLOW}Checking active features...${NC}"
    curl -s http://localhost:9101/metrics | grep "node_exporter_feature_enabled" | grep ' 1

## Deploy to All Hosts at Once

From a management machine with SSH access:

```bash
#!/bin/bash
HOSTS="dasha eva helen polly"
SCRIPT_URL="https://your-repo/node_exporter.py"  # Or use scp

for host in $HOSTS; do
    echo "========================================="
    echo "Deploying to $host..."
    echo "========================================="
    
    ssh root@$host << 'ENDSSH'
    # Install dependencies
    apt update && apt install -y python3-pip lm-sensors sysstat smartmontools nvme-cli
    pip3 install prometheus-client psutil
    
    # Setup sensors
    sensors-detect --auto
    modprobe coretemp
    
    # Create directory
    mkdir -p /opt/proxmox-exporter
    
    # Download script (or use scp from management host)
    # wget -O /opt/proxmox-exporter/node_exporter.py "$SCRIPT_URL"
    
    # Create service
    cat > /etc/systemd/system/proxmox-node-exporter.service << 'EOF'
[Unit]
Description=Comprehensive Proxmox Node Exporter
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/proxmox-exporter/node_exporter.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    
    # Start service
    systemctl daemon-reload
    systemctl enable proxmox-node-exporter
    systemctl start proxmox-node-exporter
    
    # Verify
    sleep 3
    systemctl is-active proxmox-node-exporter
ENDSSH
    
    # Copy the script (if using scp instead of wget)
    scp /path/to/node_exporter.py root@$host:/opt/proxmox-exporter/
    ssh root@$host "chmod +x /opt/proxmox-exporter/node_exporter.py"
    ssh root@$host "systemctl restart proxmox-node-exporter"
    
    # Test the endpoint
    echo "Testing $host metrics endpoint..."
    curl -s http://$host:9101/metrics | head -5 && echo "✓ Success" || echo "✗ Failed"
done
```

### Using Ansible

```yaml
---
- name: Deploy Proxmox Node Exporter
  hosts: proxmox_nodes
  become: yes
  tasks:
    - name: Install dependencies
      apt:
        name:
          - python3-pip
          - lm-sensors
          - sysstat
          - smartmontools
          - nvme-cli
        state: present
        update_cache: yes
    
    - name: Install Python packages
      pip:
        name:
          - prometheus-client
          - psutil
        state: present
    
    - name: Create exporter directory
      file:
        path: /opt/proxmox-exporter
        state: directory
        mode: '0755'
    
    - name: Copy exporter script
      copy:
        src: node_exporter.py
        dest: /opt/proxmox-exporter/node_exporter.py
        mode: '0755'
    
    - name: Create systemd service
      copy:
        content: |
          [Unit]
          Description=Comprehensive Proxmox Node Exporter
          After=network.target
          
          [Service]
          Type=simple
          User=root
          ExecStart=/usr/bin/python3 /opt/proxmox-exporter/node_exporter.py
          Restart=always
          RestartSec=10
          
          [Install]
          WantedBy=multi-user.target
        dest: /etc/systemd/system/proxmox-node-exporter.service
    
    - name: Start and enable service
      systemd:
        name: proxmox-node-exporter
        state: started
        enabled: yes
        daemon_reload: yes
    
    - name: Wait for metrics endpoint
      wait_for:
        port: 9101
        host: "{{ inventory_hostname }}"
        delay: 5
    
    - name: Verify metrics endpoint
      uri:
        url: "http://{{ inventory_hostname }}:9101/metrics"
        method: GET
        status_code: 200
```

## Available Metrics

The exporter automatically detects available features and only collects relevant metrics.

### Always Available (Base Metrics)
- `node` - Node information (hostname, kernel, OS)
- `node_features` - Shows which features are detected and active
- `node_exporter_feature_enabled` - Feature detection status (1=enabled, 0=disabled)
- `node_cpu_*` - CPU metrics (usage, frequency, load)
- `node_memory_*` - Memory metrics (total, free, available, swap)
- `node_filesystem_*` - Filesystem metrics
- `node_disk_*` - Disk I/O metrics
- `node_network_*` - Network I/O and errors
- `node_procs_*` - Process counts
- `node_load1/5/15` - Load averages

### Conditionally Available (Auto-Detected)

#### 🌡️ Temperature Sensors (if lm-sensors detected)
- `node_hwmon_temp_celsius` - Temperature readings
- `node_hwmon_temp_max_celsius` - Maximum thresholds
- `node_hwmon_temp_crit_celsius` - Critical thresholds
- `node_hwmon_fan_rpm` - Fan speeds
- `node_hwmon_power_watt` - Power consumption

#### 🎮 GPU Metrics (if GPU detected)
##### NVIDIA GPUs:
- `node_gpu_info` - GPU information (name, vendor)
- `node_gpu_count` - Number of GPUs by vendor
- `node_gpu_temp_celsius` - GPU temperature
- `node_gpu_utilization_percent` - GPU core utilization
- `node_gpu_memory_total_bytes` - Total GPU memory
- `node_gpu_memory_used_bytes` - Used GPU memory
- `node_gpu_memory_free_bytes` - Free GPU memory
- `node_gpu_power_draw_watts` - Current power draw
- `node_gpu_power_limit_watts` - Power limit
- `node_gpu_clock_graphics_hertz` - Core clock speed
- `node_gpu_clock_memory_hertz` - Memory clock speed
- `node_gpu_fan_speed_percent` - Fan speed percentage
- `node_gpu_pcie_link_gen` - PCIe generation
- `node_gpu_pcie_link_width` - PCIe link width

##### AMD GPUs:
- `node_gpu_temp_celsius` - GPU temperature
- `node_gpu_utilization_percent` - GPU utilization
- `node_gpu_memory_total_bytes` - VRAM total
- `node_gpu_memory_used_bytes` - VRAM used

#### 💾 ZFS Metrics (if ZFS detected)
- `node_zfs_arc_size_bytes` - ARC current size
- `node_zfs_arc_hits_total` - ARC hits
- `node_zfs_arc_misses_total` - ARC misses
- `node_zfs_arc_c_bytes` - ARC target size
- `node_zfs_arc_c_max_bytes` - ARC max size
- `node_zfs_zpool_health` - Pool health (0=online, 1=degraded, 2=faulted)
- `node_zfs_zpool_size_bytes` - Pool size
- `node_zfs_zpool_free_bytes` - Pool free space
- `node_zfs_zpool_allocated_bytes` - Pool allocated
- `node_zfs_zpool_fragmentation_percent` - Pool fragmentation

#### 🖥️ VM/Container Metrics (if Proxmox detected)
- `pve_vm_count` - Number of VMs/Containers by type and status
- `pve_vm_status` - VM/Container running status
- `pve_vm_cpu_usage_percent` - VM CPU usage
- `pve_vm_memory_bytes` - VM memory usage

#### 💿 SMART Metrics (if smartctl detected)
- `node_disk_smart_healthy` - Disk health status
- `node_disk_smart_temperature_celsius` - Disk temperature
- `node_disk_smart_power_on_hours` - Power on hours
- `node_disk_smart_power_cycles` - Power cycle count

#### 🔧 IPMI Metrics (if ipmitool detected)
- `node_ipmi_sensor_value` - IPMI sensor readings
- `node_ipmi_sensor_state` - IPMI sensor states

### Exporter Statistics
- `node_exporter_collection_errors_total` - Errors during collection
- `node_exporter_collection_duration_seconds` - Time spent collecting metrics

## GPU Monitoring Examples

### Prometheus Queries for GPUs

```promql
# GPU Temperature
node_gpu_temp_celsius

# GPU Utilization
node_gpu_utilization_percent

# GPU Memory Usage
(node_gpu_memory_used_bytes / node_gpu_memory_total_bytes) * 100

# GPU Power Draw vs Limit
node_gpu_power_draw_watts / node_gpu_power_limit_watts * 100

# High GPU Temperature Alert
node_gpu_temp_celsius > 80

# GPU Memory Pressure
(node_gpu_memory_used_bytes / node_gpu_memory_total_bytes) > 0.9

# Multi-GPU Load Distribution
avg by (name) (node_gpu_utilization_percent)
```

### Grafana Dashboard for GPU Monitoring

```json
{
  "title": "GPU Monitoring",
  "panels": [
    {
      "title": "GPU Temperature",
      "targets": [{
        "expr": "node_gpu_temp_celsius",
        "legendFormat": "{{name}} (GPU {{gpu}})"
      }]
    },
    {
      "title": "GPU Utilization %",
      "targets": [{
        "expr": "node_gpu_utilization_percent",
        "legendFormat": "{{name}} - {{vendor}}"
      }]
    },
    {
      "title": "GPU Memory Usage",
      "targets": [
        {
          "expr": "node_gpu_memory_used_bytes",
          "legendFormat": "{{name}} Used"
        },
        {
          "expr": "node_gpu_memory_total_bytes",
          "legendFormat": "{{name}} Total"
        }
      ]
    },
    {
      "title": "GPU Power Draw",
      "targets": [
        {
          "expr": "node_gpu_power_draw_watts",
          "legendFormat": "{{name}} Current"
        },
        {
          "expr": "node_gpu_power_limit_watts",
          "legendFormat": "{{name}} Limit"
        }
      ]
    },
    {
      "title": "GPU Clock Speeds",
      "targets": [
        {
          "expr": "node_gpu_clock_graphics_hertz / 1000000",
          "legendFormat": "{{name}} Core MHz"
        },
        {
          "expr": "node_gpu_clock_memory_hertz / 1000000",
          "legendFormat": "{{name}} Memory MHz"
        }
      ]
    }
  ]
}
```

## Feature Detection Check

To see which features are active on your host:

```bash
# Check all detected features
curl -s http://localhost:9101/metrics | grep "node_features"

# Check specific feature
curl -s http://localhost:9101/metrics | grep "node_exporter_feature_enabled"

# List active features only
curl -s http://localhost:9101/metrics | \
  grep "node_exporter_feature_enabled" | \
  grep ' 1

## Useful Prometheus Queries

### System Health
```promql
# CPU usage per core
100 - (irate(node_cpu_usage_seconds_total{mode="idle"}[5m]) * 100)

# Memory pressure
node_memory_pressure_ratio

# Disk utilization
node_disk_utilization

# Network errors rate
rate(node_network_receive_errs_total[5m]) + rate(node_network_transmit_errs_total[5m])

# Process load
node_procs_running / node_cpu_count{type="logical"}
```

### Temperature Monitoring
```promql
# Highest core temperature per node
max by (instance) (node_hwmon_temp_celsius{sensor=~"Core.*"})

# NVMe temperature
node_hwmon_temp_celsius{chip=~"nvme.*"}

# Temperature alerts (approaching critical)
node_hwmon_temp_celsius / node_hwmon_temp_crit_celsius > 0.9
```

### Disk Performance
```promql
# Disk IOPS
rate(node_disk_reads_completed_total[5m]) + rate(node_disk_writes_completed_total[5m])

# Disk latency (ms)
rate(node_disk_read_time_seconds_total[5m]) / rate(node_disk_reads_completed_total[5m]) * 1000

# Disk queue depth
node_disk_io_now

# Filesystem usage %
(node_filesystem_size_bytes - node_filesystem_avail_bytes) / node_filesystem_size_bytes * 100
```

### Network Analysis
```promql
# Network packet loss rate
rate(node_network_receive_drop_total[5m]) / rate(node_network_receive_packets_total[5m])

# TCP connection states distribution
node_network_tcp_connections

# Interface saturation (assuming 1Gbps links)
rate(node_network_transmit_bytes_total[5m]) * 8 / 1000000000
```

### VM Monitoring
```promql
# Total VM count by status
sum by (status) (pve_vm_count)

# VM memory overcommit ratio
sum(pve_vm_memory_bytes) / node_memory_MemTotal_bytes

# Top CPU consuming VMs
topk(10, pve_vm_cpu_usage)
```

### ZFS Monitoring
```promql
# ZFS ARC memory usage
node_zfs_arc_size_bytes / node_memory_MemTotal_bytes * 100

# ZFS pool usage
(node_zfs_zpool_size_bytes - node_zfs_zpool_free_bytes) / node_zfs_zpool_size_bytes * 100

# ZFS pool health alerts
node_zfs_zpool_health > 0
```

## Alerting Rules Example

```yaml
groups:
- name: proxmox_alerts
  rules:
  - alert: HighTemperature
    expr: node_hwmon_temp_celsius > 80
    for: 5m
    annotations:
      summary: "High temperature on {{ $labels.instance }}"
      description: "{{ $labels.chip }} {{ $labels.sensor }} is at {{ $value }}°C"
  
  - alert: HighMemoryPressure
    expr: node_memory_pressure_ratio > 0.9
    for: 10m
    annotations:
      summary: "High memory pressure on {{ $labels.instance }}"
  
  - alert: DiskSpaceLow
    expr: (node_filesystem_avail_bytes / node_filesystem_size_bytes) < 0.1
    for: 5m
    annotations:
      summary: "Low disk space on {{ $labels.instance }}"
      description: "{{ $labels.mountpoint }} has less than 10% free"
  
  - alert: HighDiskIO
    expr: node_disk_utilization > 90
    for: 15m
    annotations:
      summary: "High disk I/O on {{ $labels.instance }}"
  
  - alert: NetworkErrors
    expr: rate(node_network_receive_errs_total[5m]) > 10
    for: 5m
    annotations:
      summary: "Network errors on {{ $labels.instance }}"
  
  - alert: ZFSPoolDegraded
    expr: node_zfs_zpool_health > 0
    annotations:
      summary: "ZFS pool degraded on {{ $labels.instance }}"
      description: "Pool {{ $labels.pool }} health status: {{ $value }}"
```

## Troubleshooting

### Check Feature Detection:
```bash
# See what features were detected
curl -s http://localhost:9101/metrics | grep "node_exporter_feature_enabled"

# Check exporter logs for detection details
journalctl -u proxmox-node-exporter | grep -E "✓|detected"
```

### GPU-Specific Issues:

#### NVIDIA GPU not detected:
```bash
# Check if GPU is present
lspci | grep -i nvidia

# Check nvidia-smi
nvidia-smi

# If nvidia-smi missing, install drivers:
# For Proxmox/Debian:
apt install nvidia-driver nvidia-smi

# Check if nvidia-smi works
nvidia-smi --query-gpu=name --format=csv
```

#### AMD GPU not detected:
```bash
# Check if GPU is present
lspci | grep -E "VGA|Display" | grep -i AMD

# Check sysfs entries
ls /sys/class/drm/card*/device/vendor

# For better AMD support, install ROCm (optional)
# Follow: https://rocmdocs.amd.com/en/latest/Installation_Guide/Installation-Guide.html
```

#### No GPU metrics collected:
```bash
# Check if GPU features are enabled
curl -s http://localhost:9101/metrics | grep "gpu"

# Check permissions (must run as root for full GPU access)
systemctl status proxmox-node-exporter | grep User

# Manually test GPU detection in Python
python3 -c "
import subprocess
try:
    result = subprocess.run(['nvidia-smi', '-L'], capture_output=True, text=True)
    print('NVIDIA:', result.stdout if result.returncode == 0 else 'Not found')
except:
    print('nvidia-smi not available')
"
```

### Common Issues and Solutions:

1. **No temperature data despite sensors installed**:
   ```bash
   # Ensure sensors are detected
   sensors-detect --auto
   
   # Load modules
   modprobe coretemp  # Intel
   modprobe k10temp   # AMD
   
   # Test sensors
   sensors
   
   # Restart exporter
   systemctl restart proxmox-node-exporter
   ```

2. **ZFS metrics missing**:
   ```bash
   # Check if ZFS is actually present
   zpool list
   
   # Check ZFS module
   lsmod | grep zfs
   
   # If not loaded
   modprobe zfs
   ```

3. **VM metrics not showing**:
   ```bash
   # Verify Proxmox commands work
   qm list
   pct list
   
   # Check if running on actual Proxmox host
   pveversion
   ```

4. **High CPU usage by exporter**:
   ```bash
   # Check which collectors are taking time
   curl -s http://localhost:9101/metrics | \
     grep "node_exporter_collection_duration_seconds"
   
   # Edit script to disable slow collectors
   # Comment out unused feature collections in collect_all_metrics()
   ```

5. **Feature detected but no metrics**:
   ```bash
   # Check collection errors
   curl -s http://localhost:9101/metrics | \
     grep "node_exporter_collection_errors_total"
   
   # View detailed error logs
   journalctl -u proxmox-node-exporter --since "10 minutes ago" | grep ERROR
   ```

6. **Port 9101 already in use**:
   ```bash
   # Find what's using the port
   netstat -tlnp | grep 9101
   
   # Change port in script
   sed -i 's/EXPORTER_PORT = 9101/EXPORTER_PORT = 9102/' \
     /opt/proxmox-exporter/node_exporter.py
   
   # Restart service
   systemctl restart proxmox-node-exporter
   ```

### Debug Mode

Enable debug logging:
```bash
# Edit the script
nano /opt/proxmox-exporter/node_exporter.py

# Change logging level
# FROM: logging.basicConfig(level=logging.INFO, ...)
# TO:   logging.basicConfig(level=logging.DEBUG, ...)

# Restart and watch logs
systemctl restart proxmox-node-exporter
journalctl -u proxmox-node-exporter -f
```

### Manual Feature Testing

Test individual features:
```bash
# Test temperature sensors
python3 -c "import psutil; print(psutil.sensors_temperatures())"

# Test GPU (NVIDIA)
nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu --format=csv

# Test ZFS
zpool list -Hp

# Test SMART
smartctl -A /dev/sda

# Test VMs
qm list && pct list
```

### Recovery Steps

If exporter fails to start:
```bash
# 1. Check Python and dependencies
python3 --version
pip3 show prometheus-client psutil

# 2. Reinstall dependencies
pip3 install --upgrade prometheus-client psutil

# 3. Test script manually
python3 /opt/proxmox-exporter/node_exporter.py

# 4. Check for syntax errors
python3 -m py_compile /opt/proxmox-exporter/node_exporter.py

# 5. Reset service
systemctl reset-failed proxmox-node-exporter
systemctl start proxmox-node-exporter
```

## Security Note

The exporter runs on port 9101 without authentication. In production:
- Use firewall rules to restrict access
- Consider using a reverse proxy with authentication
- Or use Prometheus node_exporter with custom text collectors

## Port Information

Port **9101** was chosen because:
- 9100 is the default node_exporter port
- 9090 is default Prometheus port  
- 9093 is Alertmanager
- 9094-9099 are commonly used by other exporters
- 9101 is uncommonly used and unlikely to conflict

You can change the port by modifying `EXPORTER_PORT = 9101` in the Python script.

## Performance Tuning

For large environments, consider these optimizations:

### 1. Adjust Collection Interval
```python
# In the script, change the sleep interval
time.sleep(30)  # Instead of 15 seconds
```

### 2. Disable Unused Collectors
If you don't need certain metrics, comment out their collection in `collect_all_metrics()`:
```python
# self.collect_vm_metrics()  # Disable if not using VMs
# self.collect_zfs_metrics()  # Disable if not using ZFS
```

### 3. Use Sampling for Process Metrics
For systems with many processes, sample instead of collecting all:
```python
# Add to process collection
if random.random() > 0.1:  # Sample 10% of processes
    continue
```

### 4. Prometheus Scrape Configuration
Optimize your Prometheus configuration:
```yaml
scrape_configs:
  - job_name: 'proxmox-nodes'
    scrape_interval: 30s     # Match exporter interval
    scrape_timeout: 25s      # Slightly less than interval
    metrics_path: '/metrics'
    static_configs:
      - targets:
        - 'dasha:9101'
        - 'eva:9101'
        - 'helen:9101'
        - 'polly:9101'
    metric_relabel_configs:
      # Drop less important metrics if needed
      - source_labels: [__name__]
        regex: 'node_cpu_usage_seconds_total'
        target_label: __tmp_cpu_mode
        replacement: '${1}'
      # Keep only specific modes
      - source_labels: [__tmp_cpu_mode, mode]
        regex: ';(idle|iowait|system|user)'
        action: keep
```

## High Availability Setup

For production environments, consider running the exporter with supervision:

### Using systemd with automatic restart
```ini
[Service]
Restart=always
RestartSec=10
StartLimitBurst=5
StartLimitInterval=60s
```

### Health Check Script
Create `/opt/proxmox-exporter/health_check.sh`:
```bash
#!/bin/bash
curl -sf http://localhost:9101/metrics > /dev/null
if [ $? -ne 0 ]; then
    systemctl restart proxmox-node-exporter
    logger "Proxmox exporter health check failed, restarted service"
fi
```

Add to crontab:
```bash
*/5 * * * * /opt/proxmox-exporter/health_check.sh
```

## Integration with Existing Monitoring

### If you already have node_exporter
You can run both exporters on different ports and merge metrics in Prometheus:
```yaml
scrape_configs:
  - job_name: 'node'
    static_configs:
      - targets: ['host:9100']  # Standard node_exporter
  - job_name: 'proxmox-extended'
    static_configs:
      - targets: ['host:9101']  # Our comprehensive exporter
```

### Migrate from Netdata
To replace Netdata with this exporter:
1. Export your Netdata dashboards
2. Map Netdata metrics to our metrics (most have similar names)
3. Update your alerting rules
4. Gradually transition by running both in parallel

## Comparison with Other Solutions

| Feature | Our Exporter | node_exporter | Netdata | Telegraf |
|---------|--------------|---------------|---------|----------|
| Proxmox VMs/CTs | ✅ Full | ❌ No | ⚠️ Limited | ⚠️ Plugin |
| Temperature sensors | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| ZFS metrics | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| Disk I/O details | ✅ Full | ✅ Full | ✅ Full | ✅ Full |
| Network details | ✅ Full | ✅ Full | ✅ Full | ✅ Full |
| Process metrics | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| Memory footprint | ~50MB | ~20MB | ~200MB | ~40MB |
| CPU usage | <1% | <0.5% | 2-5% | <1% |
| Native Prometheus | ✅ Yes | ✅ Yes | ⚠️ Export | ⚠️ Export | | while read line; do
        feature=$(echo $line | grep -oP 'feature="\K[^"]+')
        echo -e "  ${GREEN}✓${NC} $feature"
    done
    
    echo -e "\n${GREEN}Metrics available at: http://$(hostname -I | awk '{print $1}'):9101/metrics${NC}"
else
    echo -e "\n${RED}✗ Failed to start exporter${NC}"
    echo "Check logs: journalctl -u proxmox-node-exporter -n 50"
    exit 1
fi

# Show next steps
echo -e "\n${BLUE}Next steps:${NC}"
echo "1. Add to Prometheus configuration:"
echo "   - target: '$(hostname):9101'"
echo "2. Import Grafana dashboards"
echo "3. Set up alerting rules"
echo ""
echo "Useful commands:"
echo "  View logs:    journalctl -u proxmox-node-exporter -f"
echo "  Check GPUs:   curl -s http://localhost:9101/metrics | grep gpu"
echo "  Check temps:  curl -s http://localhost:9101/metrics | grep temp_celsius"
```

Make it executable and run:
```bash
chmod +x deploy_smart_exporter.sh
./deploy_smart_exporter.sh
```

## Deploy to All Hosts at Once

From a management machine with SSH access:

```bash
#!/bin/bash
HOSTS="dasha eva helen polly"
SCRIPT_URL="https://your-repo/node_exporter.py"  # Or use scp

for host in $HOSTS; do
    echo "========================================="
    echo "Deploying to $host..."
    echo "========================================="
    
    ssh root@$host << 'ENDSSH'
    # Install dependencies
    apt update && apt install -y python3-pip lm-sensors sysstat smartmontools nvme-cli
    pip3 install prometheus-client psutil
    
    # Setup sensors
    sensors-detect --auto
    modprobe coretemp
    
    # Create directory
    mkdir -p /opt/proxmox-exporter
    
    # Download script (or use scp from management host)
    # wget -O /opt/proxmox-exporter/node_exporter.py "$SCRIPT_URL"
    
    # Create service
    cat > /etc/systemd/system/proxmox-node-exporter.service << 'EOF'
[Unit]
Description=Comprehensive Proxmox Node Exporter
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/proxmox-exporter/node_exporter.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    
    # Start service
    systemctl daemon-reload
    systemctl enable proxmox-node-exporter
    systemctl start proxmox-node-exporter
    
    # Verify
    sleep 3
    systemctl is-active proxmox-node-exporter
ENDSSH
    
    # Copy the script (if using scp instead of wget)
    scp /path/to/node_exporter.py root@$host:/opt/proxmox-exporter/
    ssh root@$host "chmod +x /opt/proxmox-exporter/node_exporter.py"
    ssh root@$host "systemctl restart proxmox-node-exporter"
    
    # Test the endpoint
    echo "Testing $host metrics endpoint..."
    curl -s http://$host:9101/metrics | head -5 && echo "✓ Success" || echo "✗ Failed"
done
```

### Using Ansible

```yaml
---
- name: Deploy Proxmox Node Exporter
  hosts: proxmox_nodes
  become: yes
  tasks:
    - name: Install dependencies
      apt:
        name:
          - python3-pip
          - lm-sensors
          - sysstat
          - smartmontools
          - nvme-cli
        state: present
        update_cache: yes
    
    - name: Install Python packages
      pip:
        name:
          - prometheus-client
          - psutil
        state: present
    
    - name: Create exporter directory
      file:
        path: /opt/proxmox-exporter
        state: directory
        mode: '0755'
    
    - name: Copy exporter script
      copy:
        src: node_exporter.py
        dest: /opt/proxmox-exporter/node_exporter.py
        mode: '0755'
    
    - name: Create systemd service
      copy:
        content: |
          [Unit]
          Description=Comprehensive Proxmox Node Exporter
          After=network.target
          
          [Service]
          Type=simple
          User=root
          ExecStart=/usr/bin/python3 /opt/proxmox-exporter/node_exporter.py
          Restart=always
          RestartSec=10
          
          [Install]
          WantedBy=multi-user.target
        dest: /etc/systemd/system/proxmox-node-exporter.service
    
    - name: Start and enable service
      systemd:
        name: proxmox-node-exporter
        state: started
        enabled: yes
        daemon_reload: yes
    
    - name: Wait for metrics endpoint
      wait_for:
        port: 9101
        host: "{{ inventory_hostname }}"
        delay: 5
    
    - name: Verify metrics endpoint
      uri:
        url: "http://{{ inventory_hostname }}:9101/metrics"
        method: GET
        status_code: 200
```

## Available Metrics

The exporter automatically detects available features and only collects relevant metrics.

### Always Available (Base Metrics)
- `node` - Node information (hostname, kernel, OS)
- `node_features` - Shows which features are detected and active
- `node_exporter_feature_enabled` - Feature detection status (1=enabled, 0=disabled)
- `node_cpu_*` - CPU metrics (usage, frequency, load)
- `node_memory_*` - Memory metrics (total, free, available, swap)
- `node_filesystem_*` - Filesystem metrics
- `node_disk_*` - Disk I/O metrics
- `node_network_*` - Network I/O and errors
- `node_procs_*` - Process counts
- `node_load1/5/15` - Load averages

### Conditionally Available (Auto-Detected)

#### 🌡️ Temperature Sensors (if lm-sensors detected)
- `node_hwmon_temp_celsius` - Temperature readings
- `node_hwmon_temp_max_celsius` - Maximum thresholds
- `node_hwmon_temp_crit_celsius` - Critical thresholds
- `node_hwmon_fan_rpm` - Fan speeds
- `node_hwmon_power_watt` - Power consumption

#### 🎮 GPU Metrics (if GPU detected)
##### NVIDIA GPUs:
- `node_gpu_info` - GPU information (name, vendor)
- `node_gpu_count` - Number of GPUs by vendor
- `node_gpu_temp_celsius` - GPU temperature
- `node_gpu_utilization_percent` - GPU core utilization
- `node_gpu_memory_total_bytes` - Total GPU memory
- `node_gpu_memory_used_bytes` - Used GPU memory
- `node_gpu_memory_free_bytes` - Free GPU memory
- `node_gpu_power_draw_watts` - Current power draw
- `node_gpu_power_limit_watts` - Power limit
- `node_gpu_clock_graphics_hertz` - Core clock speed
- `node_gpu_clock_memory_hertz` - Memory clock speed
- `node_gpu_fan_speed_percent` - Fan speed percentage
- `node_gpu_pcie_link_gen` - PCIe generation
- `node_gpu_pcie_link_width` - PCIe link width

##### AMD GPUs:
- `node_gpu_temp_celsius` - GPU temperature
- `node_gpu_utilization_percent` - GPU utilization
- `node_gpu_memory_total_bytes` - VRAM total
- `node_gpu_memory_used_bytes` - VRAM used

#### 💾 ZFS Metrics (if ZFS detected)
- `node_zfs_arc_size_bytes` - ARC current size
- `node_zfs_arc_hits_total` - ARC hits
- `node_zfs_arc_misses_total` - ARC misses
- `node_zfs_arc_c_bytes` - ARC target size
- `node_zfs_arc_c_max_bytes` - ARC max size
- `node_zfs_zpool_health` - Pool health (0=online, 1=degraded, 2=faulted)
- `node_zfs_zpool_size_bytes` - Pool size
- `node_zfs_zpool_free_bytes` - Pool free space
- `node_zfs_zpool_allocated_bytes` - Pool allocated
- `node_zfs_zpool_fragmentation_percent` - Pool fragmentation

#### 🖥️ VM/Container Metrics (if Proxmox detected)
- `pve_vm_count` - Number of VMs/Containers by type and status
- `pve_vm_status` - VM/Container running status
- `pve_vm_cpu_usage_percent` - VM CPU usage
- `pve_vm_memory_bytes` - VM memory usage

#### 💿 SMART Metrics (if smartctl detected)
- `node_disk_smart_healthy` - Disk health status
- `node_disk_smart_temperature_celsius` - Disk temperature
- `node_disk_smart_power_on_hours` - Power on hours
- `node_disk_smart_power_cycles` - Power cycle count

#### 🔧 IPMI Metrics (if ipmitool detected)
- `node_ipmi_sensor_value` - IPMI sensor readings
- `node_ipmi_sensor_state` - IPMI sensor states

### Exporter Statistics
- `node_exporter_collection_errors_total` - Errors during collection
- `node_exporter_collection_duration_seconds` - Time spent collecting metrics

## Grafana Dashboard Example

Import this dashboard JSON for comprehensive visualization:

```json
{
  "dashboard": {
    "title": "Proxmox Comprehensive Monitoring",
    "panels": [
      {
        "title": "CPU Package Temperature",
        "targets": [
          {
            "expr": "node_hwmon_temp_celsius{sensor=~\"Package.*\"}",
            "legendFormat": "{{instance}} - {{label}}"
          }
        ],
        "type": "graph"
      },
      {
        "title": "CPU Usage %",
        "targets": [
          {
            "expr": "100 - (avg by (instance) (irate(node_cpu_usage_seconds_total{mode=\"idle\"}[5m])) * 100)",
            "legendFormat": "{{instance}}"
          }
        ],
        "type": "graph"
      },
      {
        "title": "Memory Usage",
        "targets": [
          {
            "expr": "(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100",
            "legendFormat": "{{instance}} Memory %"
          }
        ],
        "type": "graph"
      },
      {
        "title": "Disk I/O",
        "targets": [
          {
            "expr": "rate(node_disk_read_bytes_total[5m])",
            "legendFormat": "{{instance}} {{device}} Read"
          },
          {
            "expr": "rate(node_disk_written_bytes_total[5m])",
            "legendFormat": "{{instance}} {{device}} Write"
          }
        ],
        "type": "graph"
      },
      {
        "title": "Network Traffic",
        "targets": [
          {
            "expr": "rate(node_network_receive_bytes_total{device!=\"lo\"}[5m])",
            "legendFormat": "{{instance}} {{device}} RX"
          },
          {
            "expr": "rate(node_network_transmit_bytes_total{device!=\"lo\"}[5m])",
            "legendFormat": "{{instance}} {{device}} TX"
          }
        ],
        "type": "graph"
      },
      {
        "title": "ZFS ARC Hit Rate",
        "targets": [
          {
            "expr": "rate(node_zfs_arc_hits_total[5m]) / (rate(node_zfs_arc_hits_total[5m]) + rate(node_zfs_arc_misses_total[5m])) * 100",
            "legendFormat": "{{instance}} ARC Hit %"
          }
        ],
        "type": "graph"
      },
      {
        "title": "VM CPU Usage",
        "targets": [
          {
            "expr": "pve_vm_cpu_usage",
            "legendFormat": "{{instance}} - {{name}} ({{vmid}})"
          }
        ],
        "type": "graph"
      },
      {
        "title": "TCP Connections",
        "targets": [
          {
            "expr": "node_network_tcp_connections",
            "legendFormat": "{{instance}} - {{state}}"
          }
        ],
        "type": "graph"
      }
    ]
  }
}
```

## Useful Prometheus Queries

### System Health
```promql
# CPU usage per core
100 - (irate(node_cpu_usage_seconds_total{mode="idle"}[5m]) * 100)

# Memory pressure
node_memory_pressure_ratio

# Disk utilization
node_disk_utilization

# Network errors rate
rate(node_network_receive_errs_total[5m]) + rate(node_network_transmit_errs_total[5m])

# Process load
node_procs_running / node_cpu_count{type="logical"}
```

### Temperature Monitoring
```promql
# Highest core temperature per node
max by (instance) (node_hwmon_temp_celsius{sensor=~"Core.*"})

# NVMe temperature
node_hwmon_temp_celsius{chip=~"nvme.*"}

# Temperature alerts (approaching critical)
node_hwmon_temp_celsius / node_hwmon_temp_crit_celsius > 0.9
```

### Disk Performance
```promql
# Disk IOPS
rate(node_disk_reads_completed_total[5m]) + rate(node_disk_writes_completed_total[5m])

# Disk latency (ms)
rate(node_disk_read_time_seconds_total[5m]) / rate(node_disk_reads_completed_total[5m]) * 1000

# Disk queue depth
node_disk_io_now

# Filesystem usage %
(node_filesystem_size_bytes - node_filesystem_avail_bytes) / node_filesystem_size_bytes * 100
```

### Network Analysis
```promql
# Network packet loss rate
rate(node_network_receive_drop_total[5m]) / rate(node_network_receive_packets_total[5m])

# TCP connection states distribution
node_network_tcp_connections

# Interface saturation (assuming 1Gbps links)
rate(node_network_transmit_bytes_total[5m]) * 8 / 1000000000
```

### VM Monitoring
```promql
# Total VM count by status
sum by (status) (pve_vm_count)

# VM memory overcommit ratio
sum(pve_vm_memory_bytes) / node_memory_MemTotal_bytes

# Top CPU consuming VMs
topk(10, pve_vm_cpu_usage)
```

### ZFS Monitoring
```promql
# ZFS ARC memory usage
node_zfs_arc_size_bytes / node_memory_MemTotal_bytes * 100

# ZFS pool usage
(node_zfs_zpool_size_bytes - node_zfs_zpool_free_bytes) / node_zfs_zpool_size_bytes * 100

# ZFS pool health alerts
node_zfs_zpool_health > 0
```

## Alerting Rules Example

```yaml
groups:
- name: proxmox_alerts
  rules:
  - alert: HighTemperature
    expr: node_hwmon_temp_celsius > 80
    for: 5m
    annotations:
      summary: "High temperature on {{ $labels.instance }}"
      description: "{{ $labels.chip }} {{ $labels.sensor }} is at {{ $value }}°C"
  
  - alert: HighMemoryPressure
    expr: node_memory_pressure_ratio > 0.9
    for: 10m
    annotations:
      summary: "High memory pressure on {{ $labels.instance }}"
  
  - alert: DiskSpaceLow
    expr: (node_filesystem_avail_bytes / node_filesystem_size_bytes) < 0.1
    for: 5m
    annotations:
      summary: "Low disk space on {{ $labels.instance }}"
      description: "{{ $labels.mountpoint }} has less than 10% free"
  
  - alert: HighDiskIO
    expr: node_disk_utilization > 90
    for: 15m
    annotations:
      summary: "High disk I/O on {{ $labels.instance }}"
  
  - alert: NetworkErrors
    expr: rate(node_network_receive_errs_total[5m]) > 10
    for: 5m
    annotations:
      summary: "Network errors on {{ $labels.instance }}"
  
  - alert: ZFSPoolDegraded
    expr: node_zfs_zpool_health > 0
    annotations:
      summary: "ZFS pool degraded on {{ $labels.instance }}"
      description: "Pool {{ $labels.pool }} health status: {{ $value }}"
```

## Troubleshooting

### Check if exporter is running:
```bash
systemctl status proxmox-node-exporter
netstat -tlnp | grep 9101
ps aux | grep node_exporter.py
```

### View logs:
```bash
journalctl -u proxmox-node-exporter -n 50
journalctl -u proxmox-node-exporter -f  # Follow logs
```

### Test metrics endpoint:
```bash
# Basic test
curl -s http://localhost:9101/metrics | head -20

# Check specific metrics
curl -s http://localhost:9101/metrics | grep -E "node_load|temp_celsius|disk_io"

# Count total metrics
curl -s http://localhost:9101/metrics | grep -v "^#" | wc -l
```

### Common Issues:

1. **Port already in use**: 
   ```bash
   # Find what's using port 9101
   lsof -i :9101
   # Change EXPORTER_PORT in the script to another port (e.g., 9102)
   ```

2. **Sensors not detected**:
   ```bash
   # Re-run sensor detection
   sensors-detect --auto
   # Load modules manually
   modprobe coretemp
   modprobe k10temp  # For AMD
   # Check loaded modules
   lsmod | grep temp
   ```

3. **Permission denied**:
   ```bash
   # Ensure running as root for full metrics
   # Check service user
   grep User /etc/systemd/system/proxmox-node-exporter.service
   ```

4. **No temperature data**:
   ```bash
   # Check if lm-sensors is working
   sensors
   # Check for sensor modules
   ls /sys/class/hwmon/
   # Install missing packages
   apt install lm-sensors libsensors5
   ```

5. **Missing VM metrics**:
   ```bash
   # Check if qm/pct commands work
   qm list
   pct list
   # Ensure running on Proxmox host (not inside VM)
   pveversion
   ```

6. **High memory usage**:
   ```bash
   # Check exporter memory
   ps aux | grep node_exporter.py
   # Reduce collection frequency in script
   # Disable unused collectors
   ```

7. **ZFS metrics missing**:
   ```bash
   # Check if ZFS is installed
   zpool list
   # Check ZFS module
   lsmod | grep zfs
   # Load ZFS module
   modprobe zfs
   ```

8. **Network metrics incomplete**:
   ```bash
   # Check interfaces
   ip link show
   # Check psutil version
   pip3 show psutil
   # Upgrade psutil
   pip3 install --upgrade psutil
   ```

9. **Collection errors in logs**:
   ```bash
   # Check specific collector errors
   curl -s http://localhost:9101/metrics | grep collection_errors
   # Increase log verbosity (edit script)
   logging.basicConfig(level=logging.DEBUG)
   ```

10. **Prometheus not scraping**:
    ```bash
    # Test from Prometheus server
    curl http://proxmox-host:9101/metrics
    # Check Prometheus targets
    # http://prometheus:9090/targets
    # Check firewall
    iptables -L -n | grep 9101
    ```

### Debug Mode

Add debug output to the script:
```python
# At the top of the script
import os
os.environ['PYTHONUNBUFFERED'] = '1'
logging.basicConfig(level=logging.DEBUG)

# In collect methods
logger.debug(f"Collecting {collector_name} metrics...")
```

### Performance Issues

If experiencing performance problems:

1. **Profile the exporter**:
   ```python
   import cProfile
   import pstats
   
   # In main block
   profiler = cProfile.Profile()
   profiler.enable()
   # ... run exporter ...
   profiler.disable()
   stats = pstats.Stats(profiler)
   stats.sort_stats('cumulative')
   stats.print_stats(10)
   ```

2. **Monitor exporter metrics**:
   ```bash
   # CPU usage
   top -p $(pgrep -f node_exporter.py)
   
   # Memory usage over time
   while true; do 
     ps aux | grep node_exporter.py | grep -v grep | awk '{print $6}'
     sleep 5
   done
   ```

3. **Reduce collection frequency**:
   - Increase sleep interval between collections
   - Use Prometheus recording rules for expensive queries
   - Cache static information

## Security Note

The exporter runs on port 9101 without authentication. In production:
- Use firewall rules to restrict access
- Consider using a reverse proxy with authentication
- Or use Prometheus node_exporter with custom text collectors

## Port Information

Port **9101** was chosen because:
- 9100 is the default node_exporter port
- 9090 is default Prometheus port  
- 9093 is Alertmanager
- 9094-9099 are commonly used by other exporters
- 9101 is uncommonly used and unlikely to conflict

You can change the port by modifying `EXPORTER_PORT = 9101` in the Python script.

## Performance Tuning

For large environments, consider these optimizations:

### 1. Adjust Collection Interval
```python
# In the script, change the sleep interval
time.sleep(30)  # Instead of 15 seconds
```

### 2. Disable Unused Collectors
If you don't need certain metrics, comment out their collection in `collect_all_metrics()`:
```python
# self.collect_vm_metrics()  # Disable if not using VMs
# self.collect_zfs_metrics()  # Disable if not using ZFS
```

### 3. Use Sampling for Process Metrics
For systems with many processes, sample instead of collecting all:
```python
# Add to process collection
if random.random() > 0.1:  # Sample 10% of processes
    continue
```

### 4. Prometheus Scrape Configuration
Optimize your Prometheus configuration:
```yaml
scrape_configs:
  - job_name: 'proxmox-nodes'
    scrape_interval: 30s     # Match exporter interval
    scrape_timeout: 25s      # Slightly less than interval
    metrics_path: '/metrics'
    static_configs:
      - targets:
        - 'dasha:9101'
        - 'eva:9101'
        - 'helen:9101'
        - 'polly:9101'
    metric_relabel_configs:
      # Drop less important metrics if needed
      - source_labels: [__name__]
        regex: 'node_cpu_usage_seconds_total'
        target_label: __tmp_cpu_mode
        replacement: '${1}'
      # Keep only specific modes
      - source_labels: [__tmp_cpu_mode, mode]
        regex: ';(idle|iowait|system|user)'
        action: keep
```

## High Availability Setup

For production environments, consider running the exporter with supervision:

### Using systemd with automatic restart
```ini
[Service]
Restart=always
RestartSec=10
StartLimitBurst=5
StartLimitInterval=60s
```

### Health Check Script
Create `/opt/proxmox-exporter/health_check.sh`:
```bash
#!/bin/bash
curl -sf http://localhost:9101/metrics > /dev/null
if [ $? -ne 0 ]; then
    systemctl restart proxmox-node-exporter
    logger "Proxmox exporter health check failed, restarted service"
fi
```

Add to crontab:
```bash
*/5 * * * * /opt/proxmox-exporter/health_check.sh
```

## Integration with Existing Monitoring

### If you already have node_exporter
You can run both exporters on different ports and merge metrics in Prometheus:
```yaml
scrape_configs:
  - job_name: 'node'
    static_configs:
      - targets: ['host:9100']  # Standard node_exporter
  - job_name: 'proxmox-extended'
    static_configs:
      - targets: ['host:9101']  # Our comprehensive exporter
```

### Migrate from Netdata
To replace Netdata with this exporter:
1. Export your Netdata dashboards
2. Map Netdata metrics to our metrics (most have similar names)
3. Update your alerting rules
4. Gradually transition by running both in parallel

## Comparison with Other Solutions

| Feature | Our Exporter | node_exporter | Netdata | Telegraf |
|---------|--------------|---------------|---------|----------|
| Proxmox VMs/CTs | ✅ Full | ❌ No | ⚠️ Limited | ⚠️ Plugin |
| Temperature sensors | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| ZFS metrics | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| Disk I/O details | ✅ Full | ✅ Full | ✅ Full | ✅ Full |
| Network details | ✅ Full | ✅ Full | ✅ Full | ✅ Full |
| Process metrics | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| Memory footprint | ~50MB | ~20MB | ~200MB | ~40MB |
| CPU usage | <1% | <0.5% | 2-5% | <1% |
| Native Prometheus | ✅ Yes | ✅ Yes | ⚠️ Export | ⚠️ Export | | \
  awk -F'"' '{print $2}'
```

Example output:
```
sensors
nvidia_gpu
zfs
qemu_vms
lxc_containers
smart_monitoring
```

## Useful Prometheus Queries

### System Health
```promql
# CPU usage per core
100 - (irate(node_cpu_usage_seconds_total{mode="idle"}[5m]) * 100)

# Memory pressure
node_memory_pressure_ratio

# Disk utilization
node_disk_utilization

# Network errors rate
rate(node_network_receive_errs_total[5m]) + rate(node_network_transmit_errs_total[5m])

# Process load
node_procs_running / node_cpu_count{type="logical"}
```

### Temperature Monitoring
```promql
# Highest core temperature per node
max by (instance) (node_hwmon_temp_celsius{sensor=~"Core.*"})

# NVMe temperature
node_hwmon_temp_celsius{chip=~"nvme.*"}

# Temperature alerts (approaching critical)
node_hwmon_temp_celsius / node_hwmon_temp_crit_celsius > 0.9
```

### Disk Performance
```promql
# Disk IOPS
rate(node_disk_reads_completed_total[5m]) + rate(node_disk_writes_completed_total[5m])

# Disk latency (ms)
rate(node_disk_read_time_seconds_total[5m]) / rate(node_disk_reads_completed_total[5m]) * 1000

# Disk queue depth
node_disk_io_now

# Filesystem usage %
(node_filesystem_size_bytes - node_filesystem_avail_bytes) / node_filesystem_size_bytes * 100
```

### Network Analysis
```promql
# Network packet loss rate
rate(node_network_receive_drop_total[5m]) / rate(node_network_receive_packets_total[5m])

# TCP connection states distribution
node_network_tcp_connections

# Interface saturation (assuming 1Gbps links)
rate(node_network_transmit_bytes_total[5m]) * 8 / 1000000000
```

### VM Monitoring
```promql
# Total VM count by status
sum by (status) (pve_vm_count)

# VM memory overcommit ratio
sum(pve_vm_memory_bytes) / node_memory_MemTotal_bytes

# Top CPU consuming VMs
topk(10, pve_vm_cpu_usage)
```

### ZFS Monitoring
```promql
# ZFS ARC memory usage
node_zfs_arc_size_bytes / node_memory_MemTotal_bytes * 100

# ZFS pool usage
(node_zfs_zpool_size_bytes - node_zfs_zpool_free_bytes) / node_zfs_zpool_size_bytes * 100

# ZFS pool health alerts
node_zfs_zpool_health > 0
```

## Alerting Rules Example

```yaml
groups:
- name: proxmox_alerts
  rules:
  - alert: HighTemperature
    expr: node_hwmon_temp_celsius > 80
    for: 5m
    annotations:
      summary: "High temperature on {{ $labels.instance }}"
      description: "{{ $labels.chip }} {{ $labels.sensor }} is at {{ $value }}°C"
  
  - alert: HighMemoryPressure
    expr: node_memory_pressure_ratio > 0.9
    for: 10m
    annotations:
      summary: "High memory pressure on {{ $labels.instance }}"
  
  - alert: DiskSpaceLow
    expr: (node_filesystem_avail_bytes / node_filesystem_size_bytes) < 0.1
    for: 5m
    annotations:
      summary: "Low disk space on {{ $labels.instance }}"
      description: "{{ $labels.mountpoint }} has less than 10% free"
  
  - alert: HighDiskIO
    expr: node_disk_utilization > 90
    for: 15m
    annotations:
      summary: "High disk I/O on {{ $labels.instance }}"
  
  - alert: NetworkErrors
    expr: rate(node_network_receive_errs_total[5m]) > 10
    for: 5m
    annotations:
      summary: "Network errors on {{ $labels.instance }}"
  
  - alert: ZFSPoolDegraded
    expr: node_zfs_zpool_health > 0
    annotations:
      summary: "ZFS pool degraded on {{ $labels.instance }}"
      description: "Pool {{ $labels.pool }} health status: {{ $value }}"
```

## Troubleshooting

### Check if exporter is running:
```bash
systemctl status proxmox-node-exporter
netstat -tlnp | grep 9101
ps aux | grep node_exporter.py
```

### View logs:
```bash
journalctl -u proxmox-node-exporter -n 50
journalctl -u proxmox-node-exporter -f  # Follow logs
```

### Test metrics endpoint:
```bash
# Basic test
curl -s http://localhost:9101/metrics | head -20

# Check specific metrics
curl -s http://localhost:9101/metrics | grep -E "node_load|temp_celsius|disk_io"

# Count total metrics
curl -s http://localhost:9101/metrics | grep -v "^#" | wc -l
```

### Common Issues:

1. **Port already in use**: 
   ```bash
   # Find what's using port 9101
   lsof -i :9101
   # Change EXPORTER_PORT in the script to another port (e.g., 9102)
   ```

2. **Sensors not detected**:
   ```bash
   # Re-run sensor detection
   sensors-detect --auto
   # Load modules manually
   modprobe coretemp
   modprobe k10temp  # For AMD
   # Check loaded modules
   lsmod | grep temp
   ```

3. **Permission denied**:
   ```bash
   # Ensure running as root for full metrics
   # Check service user
   grep User /etc/systemd/system/proxmox-node-exporter.service
   ```

4. **No temperature data**:
   ```bash
   # Check if lm-sensors is working
   sensors
   # Check for sensor modules
   ls /sys/class/hwmon/
   # Install missing packages
   apt install lm-sensors libsensors5
   ```

5. **Missing VM metrics**:
   ```bash
   # Check if qm/pct commands work
   qm list
   pct list
   # Ensure running on Proxmox host (not inside VM)
   pveversion
   ```

6. **High memory usage**:
   ```bash
   # Check exporter memory
   ps aux | grep node_exporter.py
   # Reduce collection frequency in script
   # Disable unused collectors
   ```

7. **ZFS metrics missing**:
   ```bash
   # Check if ZFS is installed
   zpool list
   # Check ZFS module
   lsmod | grep zfs
   # Load ZFS module
   modprobe zfs
   ```

8. **Network metrics incomplete**:
   ```bash
   # Check interfaces
   ip link show
   # Check psutil version
   pip3 show psutil
   # Upgrade psutil
   pip3 install --upgrade psutil
   ```

9. **Collection errors in logs**:
   ```bash
   # Check specific collector errors
   curl -s http://localhost:9101/metrics | grep collection_errors
   # Increase log verbosity (edit script)
   logging.basicConfig(level=logging.DEBUG)
   ```

10. **Prometheus not scraping**:
    ```bash
    # Test from Prometheus server
    curl http://proxmox-host:9101/metrics
    # Check Prometheus targets
    # http://prometheus:9090/targets
    # Check firewall
    iptables -L -n | grep 9101
    ```

### Debug Mode

Add debug output to the script:
```python
# At the top of the script
import os
os.environ['PYTHONUNBUFFERED'] = '1'
logging.basicConfig(level=logging.DEBUG)

# In collect methods
logger.debug(f"Collecting {collector_name} metrics...")
```

### Performance Issues

If experiencing performance problems:

1. **Profile the exporter**:
   ```python
   import cProfile
   import pstats
   
   # In main block
   profiler = cProfile.Profile()
   profiler.enable()
   # ... run exporter ...
   profiler.disable()
   stats = pstats.Stats(profiler)
   stats.sort_stats('cumulative')
   stats.print_stats(10)
   ```

2. **Monitor exporter metrics**:
   ```bash
   # CPU usage
   top -p $(pgrep -f node_exporter.py)
   
   # Memory usage over time
   while true; do 
     ps aux | grep node_exporter.py | grep -v grep | awk '{print $6}'
     sleep 5
   done
   ```

3. **Reduce collection frequency**:
   - Increase sleep interval between collections
   - Use Prometheus recording rules for expensive queries
   - Cache static information

## Security Note

The exporter runs on port 9101 without authentication. In production:
- Use firewall rules to restrict access
- Consider using a reverse proxy with authentication
- Or use Prometheus node_exporter with custom text collectors

## Port Information

Port **9101** was chosen because:
- 9100 is the default node_exporter port
- 9090 is default Prometheus port  
- 9093 is Alertmanager
- 9094-9099 are commonly used by other exporters
- 9101 is uncommonly used and unlikely to conflict

You can change the port by modifying `EXPORTER_PORT = 9101` in the Python script.

## Performance Tuning

For large environments, consider these optimizations:

### 1. Adjust Collection Interval
```python
# In the script, change the sleep interval
time.sleep(30)  # Instead of 15 seconds
```

### 2. Disable Unused Collectors
If you don't need certain metrics, comment out their collection in `collect_all_metrics()`:
```python
# self.collect_vm_metrics()  # Disable if not using VMs
# self.collect_zfs_metrics()  # Disable if not using ZFS
```

### 3. Use Sampling for Process Metrics
For systems with many processes, sample instead of collecting all:
```python
# Add to process collection
if random.random() > 0.1:  # Sample 10% of processes
    continue
```

### 4. Prometheus Scrape Configuration
Optimize your Prometheus configuration:
```yaml
scrape_configs:
  - job_name: 'proxmox-nodes'
    scrape_interval: 30s     # Match exporter interval
    scrape_timeout: 25s      # Slightly less than interval
    metrics_path: '/metrics'
    static_configs:
      - targets:
        - 'dasha:9101'
        - 'eva:9101'
        - 'helen:9101'
        - 'polly:9101'
    metric_relabel_configs:
      # Drop less important metrics if needed
      - source_labels: [__name__]
        regex: 'node_cpu_usage_seconds_total'
        target_label: __tmp_cpu_mode
        replacement: '${1}'
      # Keep only specific modes
      - source_labels: [__tmp_cpu_mode, mode]
        regex: ';(idle|iowait|system|user)'
        action: keep
```

## High Availability Setup

For production environments, consider running the exporter with supervision:

### Using systemd with automatic restart
```ini
[Service]
Restart=always
RestartSec=10
StartLimitBurst=5
StartLimitInterval=60s
```

### Health Check Script
Create `/opt/proxmox-exporter/health_check.sh`:
```bash
#!/bin/bash
curl -sf http://localhost:9101/metrics > /dev/null
if [ $? -ne 0 ]; then
    systemctl restart proxmox-node-exporter
    logger "Proxmox exporter health check failed, restarted service"
fi
```

Add to crontab:
```bash
*/5 * * * * /opt/proxmox-exporter/health_check.sh
```

## Integration with Existing Monitoring

### If you already have node_exporter
You can run both exporters on different ports and merge metrics in Prometheus:
```yaml
scrape_configs:
  - job_name: 'node'
    static_configs:
      - targets: ['host:9100']  # Standard node_exporter
  - job_name: 'proxmox-extended'
    static_configs:
      - targets: ['host:9101']  # Our comprehensive exporter
```

### Migrate from Netdata
To replace Netdata with this exporter:
1. Export your Netdata dashboards
2. Map Netdata metrics to our metrics (most have similar names)
3. Update your alerting rules
4. Gradually transition by running both in parallel

## Comparison with Other Solutions

| Feature | Our Exporter | node_exporter | Netdata | Telegraf |
|---------|--------------|---------------|---------|----------|
| Proxmox VMs/CTs | ✅ Full | ❌ No | ⚠️ Limited | ⚠️ Plugin |
| Temperature sensors | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| ZFS metrics | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| Disk I/O details | ✅ Full | ✅ Full | ✅ Full | ✅ Full |
| Network details | ✅ Full | ✅ Full | ✅ Full | ✅ Full |
| Process metrics | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| Memory footprint | ~50MB | ~20MB | ~200MB | ~40MB |
| CPU usage | <1% | <0.5% | 2-5% | <1% |
| Native Prometheus | ✅ Yes | ✅ Yes | ⚠️ Export | ⚠️ Export | | while read line; do
        feature=$(echo $line | grep -oP 'feature="\K[^"]+')
        echo -e "  ${GREEN}✓${NC} $feature"
    done
    
    echo -e "\n${GREEN}Metrics available at: http://$(hostname -I | awk '{print $1}'):9101/metrics${NC}"
else
    echo -e "\n${RED}✗ Failed to start exporter${NC}"
    echo "Check logs: journalctl -u proxmox-node-exporter -n 50"
    exit 1
fi

# Show next steps
echo -e "\n${BLUE}Next steps:${NC}"
echo "1. Add to Prometheus configuration:"
echo "   - target: '$(hostname):9101'"
echo "2. Import Grafana dashboards"
echo "3. Set up alerting rules"
echo ""
echo "Useful commands:"
echo "  View logs:    journalctl -u proxmox-node-exporter -f"
echo "  Check GPUs:   curl -s http://localhost:9101/metrics | grep gpu"
echo "  Check temps:  curl -s http://localhost:9101/metrics | grep temp_celsius"
```

Make it executable and run:
```bash
chmod +x deploy_smart_exporter.sh
./deploy_smart_exporter.sh
```

## Deploy to All Hosts at Once

From a management machine with SSH access:

```bash
#!/bin/bash
HOSTS="dasha eva helen polly"
SCRIPT_URL="https://your-repo/node_exporter.py"  # Or use scp

for host in $HOSTS; do
    echo "========================================="
    echo "Deploying to $host..."
    echo "========================================="
    
    ssh root@$host << 'ENDSSH'
    # Install dependencies
    apt update && apt install -y python3-pip lm-sensors sysstat smartmontools nvme-cli
    pip3 install prometheus-client psutil
    
    # Setup sensors
    sensors-detect --auto
    modprobe coretemp
    
    # Create directory
    mkdir -p /opt/proxmox-exporter
    
    # Download script (or use scp from management host)
    # wget -O /opt/proxmox-exporter/node_exporter.py "$SCRIPT_URL"
    
    # Create service
    cat > /etc/systemd/system/proxmox-node-exporter.service << 'EOF'
[Unit]
Description=Comprehensive Proxmox Node Exporter
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/proxmox-exporter/node_exporter.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    
    # Start service
    systemctl daemon-reload
    systemctl enable proxmox-node-exporter
    systemctl start proxmox-node-exporter
    
    # Verify
    sleep 3
    systemctl is-active proxmox-node-exporter
ENDSSH
    
    # Copy the script (if using scp instead of wget)
    scp /path/to/node_exporter.py root@$host:/opt/proxmox-exporter/
    ssh root@$host "chmod +x /opt/proxmox-exporter/node_exporter.py"
    ssh root@$host "systemctl restart proxmox-node-exporter"
    
    # Test the endpoint
    echo "Testing $host metrics endpoint..."
    curl -s http://$host:9101/metrics | head -5 && echo "✓ Success" || echo "✗ Failed"
done
```

### Using Ansible

```yaml
---
- name: Deploy Proxmox Node Exporter
  hosts: proxmox_nodes
  become: yes
  tasks:
    - name: Install dependencies
      apt:
        name:
          - python3-pip
          - lm-sensors
          - sysstat
          - smartmontools
          - nvme-cli
        state: present
        update_cache: yes
    
    - name: Install Python packages
      pip:
        name:
          - prometheus-client
          - psutil
        state: present
    
    - name: Create exporter directory
      file:
        path: /opt/proxmox-exporter
        state: directory
        mode: '0755'
    
    - name: Copy exporter script
      copy:
        src: node_exporter.py
        dest: /opt/proxmox-exporter/node_exporter.py
        mode: '0755'
    
    - name: Create systemd service
      copy:
        content: |
          [Unit]
          Description=Comprehensive Proxmox Node Exporter
          After=network.target
          
          [Service]
          Type=simple
          User=root
          ExecStart=/usr/bin/python3 /opt/proxmox-exporter/node_exporter.py
          Restart=always
          RestartSec=10
          
          [Install]
          WantedBy=multi-user.target
        dest: /etc/systemd/system/proxmox-node-exporter.service
    
    - name: Start and enable service
      systemd:
        name: proxmox-node-exporter
        state: started
        enabled: yes
        daemon_reload: yes
    
    - name: Wait for metrics endpoint
      wait_for:
        port: 9101
        host: "{{ inventory_hostname }}"
        delay: 5
    
    - name: Verify metrics endpoint
      uri:
        url: "http://{{ inventory_hostname }}:9101/metrics"
        method: GET
        status_code: 200
```

## Available Metrics

The exporter automatically detects available features and only collects relevant metrics.

### Always Available (Base Metrics)
- `node` - Node information (hostname, kernel, OS)
- `node_features` - Shows which features are detected and active
- `node_exporter_feature_enabled` - Feature detection status (1=enabled, 0=disabled)
- `node_cpu_*` - CPU metrics (usage, frequency, load)
- `node_memory_*` - Memory metrics (total, free, available, swap)
- `node_filesystem_*` - Filesystem metrics
- `node_disk_*` - Disk I/O metrics
- `node_network_*` - Network I/O and errors
- `node_procs_*` - Process counts
- `node_load1/5/15` - Load averages

### Conditionally Available (Auto-Detected)

#### 🌡️ Temperature Sensors (if lm-sensors detected)
- `node_hwmon_temp_celsius` - Temperature readings
- `node_hwmon_temp_max_celsius` - Maximum thresholds
- `node_hwmon_temp_crit_celsius` - Critical thresholds
- `node_hwmon_fan_rpm` - Fan speeds
- `node_hwmon_power_watt` - Power consumption

#### 🎮 GPU Metrics (if GPU detected)
##### NVIDIA GPUs:
- `node_gpu_info` - GPU information (name, vendor)
- `node_gpu_count` - Number of GPUs by vendor
- `node_gpu_temp_celsius` - GPU temperature
- `node_gpu_utilization_percent` - GPU core utilization
- `node_gpu_memory_total_bytes` - Total GPU memory
- `node_gpu_memory_used_bytes` - Used GPU memory
- `node_gpu_memory_free_bytes` - Free GPU memory
- `node_gpu_power_draw_watts` - Current power draw
- `node_gpu_power_limit_watts` - Power limit
- `node_gpu_clock_graphics_hertz` - Core clock speed
- `node_gpu_clock_memory_hertz` - Memory clock speed
- `node_gpu_fan_speed_percent` - Fan speed percentage
- `node_gpu_pcie_link_gen` - PCIe generation
- `node_gpu_pcie_link_width` - PCIe link width

##### AMD GPUs:
- `node_gpu_temp_celsius` - GPU temperature
- `node_gpu_utilization_percent` - GPU utilization
- `node_gpu_memory_total_bytes` - VRAM total
- `node_gpu_memory_used_bytes` - VRAM used

#### 💾 ZFS Metrics (if ZFS detected)
- `node_zfs_arc_size_bytes` - ARC current size
- `node_zfs_arc_hits_total` - ARC hits
- `node_zfs_arc_misses_total` - ARC misses
- `node_zfs_arc_c_bytes` - ARC target size
- `node_zfs_arc_c_max_bytes` - ARC max size
- `node_zfs_zpool_health` - Pool health (0=online, 1=degraded, 2=faulted)
- `node_zfs_zpool_size_bytes` - Pool size
- `node_zfs_zpool_free_bytes` - Pool free space
- `node_zfs_zpool_allocated_bytes` - Pool allocated
- `node_zfs_zpool_fragmentation_percent` - Pool fragmentation

#### 🖥️ VM/Container Metrics (if Proxmox detected)
- `pve_vm_count` - Number of VMs/Containers by type and status
- `pve_vm_status` - VM/Container running status
- `pve_vm_cpu_usage_percent` - VM CPU usage
- `pve_vm_memory_bytes` - VM memory usage

#### 💿 SMART Metrics (if smartctl detected)
- `node_disk_smart_healthy` - Disk health status
- `node_disk_smart_temperature_celsius` - Disk temperature
- `node_disk_smart_power_on_hours` - Power on hours
- `node_disk_smart_power_cycles` - Power cycle count

#### 🔧 IPMI Metrics (if ipmitool detected)
- `node_ipmi_sensor_value` - IPMI sensor readings
- `node_ipmi_sensor_state` - IPMI sensor states

### Exporter Statistics
- `node_exporter_collection_errors_total` - Errors during collection
- `node_exporter_collection_duration_seconds` - Time spent collecting metrics

## Grafana Dashboard Example

Import this dashboard JSON for comprehensive visualization:

```json
{
  "dashboard": {
    "title": "Proxmox Comprehensive Monitoring",
    "panels": [
      {
        "title": "CPU Package Temperature",
        "targets": [
          {
            "expr": "node_hwmon_temp_celsius{sensor=~\"Package.*\"}",
            "legendFormat": "{{instance}} - {{label}}"
          }
        ],
        "type": "graph"
      },
      {
        "title": "CPU Usage %",
        "targets": [
          {
            "expr": "100 - (avg by (instance) (irate(node_cpu_usage_seconds_total{mode=\"idle\"}[5m])) * 100)",
            "legendFormat": "{{instance}}"
          }
        ],
        "type": "graph"
      },
      {
        "title": "Memory Usage",
        "targets": [
          {
            "expr": "(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100",
            "legendFormat": "{{instance}} Memory %"
          }
        ],
        "type": "graph"
      },
      {
        "title": "Disk I/O",
        "targets": [
          {
            "expr": "rate(node_disk_read_bytes_total[5m])",
            "legendFormat": "{{instance}} {{device}} Read"
          },
          {
            "expr": "rate(node_disk_written_bytes_total[5m])",
            "legendFormat": "{{instance}} {{device}} Write"
          }
        ],
        "type": "graph"
      },
      {
        "title": "Network Traffic",
        "targets": [
          {
            "expr": "rate(node_network_receive_bytes_total{device!=\"lo\"}[5m])",
            "legendFormat": "{{instance}} {{device}} RX"
          },
          {
            "expr": "rate(node_network_transmit_bytes_total{device!=\"lo\"}[5m])",
            "legendFormat": "{{instance}} {{device}} TX"
          }
        ],
        "type": "graph"
      },
      {
        "title": "ZFS ARC Hit Rate",
        "targets": [
          {
            "expr": "rate(node_zfs_arc_hits_total[5m]) / (rate(node_zfs_arc_hits_total[5m]) + rate(node_zfs_arc_misses_total[5m])) * 100",
            "legendFormat": "{{instance}} ARC Hit %"
          }
        ],
        "type": "graph"
      },
      {
        "title": "VM CPU Usage",
        "targets": [
          {
            "expr": "pve_vm_cpu_usage",
            "legendFormat": "{{instance}} - {{name}} ({{vmid}})"
          }
        ],
        "type": "graph"
      },
      {
        "title": "TCP Connections",
        "targets": [
          {
            "expr": "node_network_tcp_connections",
            "legendFormat": "{{instance}} - {{state}}"
          }
        ],
        "type": "graph"
      }
    ]
  }
}
```

## Useful Prometheus Queries

### System Health
```promql
# CPU usage per core
100 - (irate(node_cpu_usage_seconds_total{mode="idle"}[5m]) * 100)

# Memory pressure
node_memory_pressure_ratio

# Disk utilization
node_disk_utilization

# Network errors rate
rate(node_network_receive_errs_total[5m]) + rate(node_network_transmit_errs_total[5m])

# Process load
node_procs_running / node_cpu_count{type="logical"}
```

### Temperature Monitoring
```promql
# Highest core temperature per node
max by (instance) (node_hwmon_temp_celsius{sensor=~"Core.*"})

# NVMe temperature
node_hwmon_temp_celsius{chip=~"nvme.*"}

# Temperature alerts (approaching critical)
node_hwmon_temp_celsius / node_hwmon_temp_crit_celsius > 0.9
```

### Disk Performance
```promql
# Disk IOPS
rate(node_disk_reads_completed_total[5m]) + rate(node_disk_writes_completed_total[5m])

# Disk latency (ms)
rate(node_disk_read_time_seconds_total[5m]) / rate(node_disk_reads_completed_total[5m]) * 1000

# Disk queue depth
node_disk_io_now

# Filesystem usage %
(node_filesystem_size_bytes - node_filesystem_avail_bytes) / node_filesystem_size_bytes * 100
```

### Network Analysis
```promql
# Network packet loss rate
rate(node_network_receive_drop_total[5m]) / rate(node_network_receive_packets_total[5m])

# TCP connection states distribution
node_network_tcp_connections

# Interface saturation (assuming 1Gbps links)
rate(node_network_transmit_bytes_total[5m]) * 8 / 1000000000
```

### VM Monitoring
```promql
# Total VM count by status
sum by (status) (pve_vm_count)

# VM memory overcommit ratio
sum(pve_vm_memory_bytes) / node_memory_MemTotal_bytes

# Top CPU consuming VMs
topk(10, pve_vm_cpu_usage)
```

### ZFS Monitoring
```promql
# ZFS ARC memory usage
node_zfs_arc_size_bytes / node_memory_MemTotal_bytes * 100

# ZFS pool usage
(node_zfs_zpool_size_bytes - node_zfs_zpool_free_bytes) / node_zfs_zpool_size_bytes * 100

# ZFS pool health alerts
node_zfs_zpool_health > 0
```

## Alerting Rules Example

```yaml
groups:
- name: proxmox_alerts
  rules:
  - alert: HighTemperature
    expr: node_hwmon_temp_celsius > 80
    for: 5m
    annotations:
      summary: "High temperature on {{ $labels.instance }}"
      description: "{{ $labels.chip }} {{ $labels.sensor }} is at {{ $value }}°C"
  
  - alert: HighMemoryPressure
    expr: node_memory_pressure_ratio > 0.9
    for: 10m
    annotations:
      summary: "High memory pressure on {{ $labels.instance }}"
  
  - alert: DiskSpaceLow
    expr: (node_filesystem_avail_bytes / node_filesystem_size_bytes) < 0.1
    for: 5m
    annotations:
      summary: "Low disk space on {{ $labels.instance }}"
      description: "{{ $labels.mountpoint }} has less than 10% free"
  
  - alert: HighDiskIO
    expr: node_disk_utilization > 90
    for: 15m
    annotations:
      summary: "High disk I/O on {{ $labels.instance }}"
  
  - alert: NetworkErrors
    expr: rate(node_network_receive_errs_total[5m]) > 10
    for: 5m
    annotations:
      summary: "Network errors on {{ $labels.instance }}"
  
  - alert: ZFSPoolDegraded
    expr: node_zfs_zpool_health > 0
    annotations:
      summary: "ZFS pool degraded on {{ $labels.instance }}"
      description: "Pool {{ $labels.pool }} health status: {{ $value }}"
```

## Troubleshooting

### Check if exporter is running:
```bash
systemctl status proxmox-node-exporter
netstat -tlnp | grep 9101
ps aux | grep node_exporter.py
```

### View logs:
```bash
journalctl -u proxmox-node-exporter -n 50
journalctl -u proxmox-node-exporter -f  # Follow logs
```

### Test metrics endpoint:
```bash
# Basic test
curl -s http://localhost:9101/metrics | head -20

# Check specific metrics
curl -s http://localhost:9101/metrics | grep -E "node_load|temp_celsius|disk_io"

# Count total metrics
curl -s http://localhost:9101/metrics | grep -v "^#" | wc -l
```

### Common Issues:

1. **Port already in use**: 
   ```bash
   # Find what's using port 9101
   lsof -i :9101
   # Change EXPORTER_PORT in the script to another port (e.g., 9102)
   ```

2. **Sensors not detected**:
   ```bash
   # Re-run sensor detection
   sensors-detect --auto
   # Load modules manually
   modprobe coretemp
   modprobe k10temp  # For AMD
   # Check loaded modules
   lsmod | grep temp
   ```

3. **Permission denied**:
   ```bash
   # Ensure running as root for full metrics
   # Check service user
   grep User /etc/systemd/system/proxmox-node-exporter.service
   ```

4. **No temperature data**:
   ```bash
   # Check if lm-sensors is working
   sensors
   # Check for sensor modules
   ls /sys/class/hwmon/
   # Install missing packages
   apt install lm-sensors libsensors5
   ```

5. **Missing VM metrics**:
   ```bash
   # Check if qm/pct commands work
   qm list
   pct list
   # Ensure running on Proxmox host (not inside VM)
   pveversion
   ```

6. **High memory usage**:
   ```bash
   # Check exporter memory
   ps aux | grep node_exporter.py
   # Reduce collection frequency in script
   # Disable unused collectors
   ```

7. **ZFS metrics missing**:
   ```bash
   # Check if ZFS is installed
   zpool list
   # Check ZFS module
   lsmod | grep zfs
   # Load ZFS module
   modprobe zfs
   ```

8. **Network metrics incomplete**:
   ```bash
   # Check interfaces
   ip link show
   # Check psutil version
   pip3 show psutil
   # Upgrade psutil
   pip3 install --upgrade psutil
   ```

9. **Collection errors in logs**:
   ```bash
   # Check specific collector errors
   curl -s http://localhost:9101/metrics | grep collection_errors
   # Increase log verbosity (edit script)
   logging.basicConfig(level=logging.DEBUG)
   ```

10. **Prometheus not scraping**:
    ```bash
    # Test from Prometheus server
    curl http://proxmox-host:9101/metrics
    # Check Prometheus targets
    # http://prometheus:9090/targets
    # Check firewall
    iptables -L -n | grep 9101
    ```

### Debug Mode

Add debug output to the script:
```python
# At the top of the script
import os
os.environ['PYTHONUNBUFFERED'] = '1'
logging.basicConfig(level=logging.DEBUG)

# In collect methods
logger.debug(f"Collecting {collector_name} metrics...")
```

### Performance Issues

If experiencing performance problems:

1. **Profile the exporter**:
   ```python
   import cProfile
   import pstats
   
   # In main block
   profiler = cProfile.Profile()
   profiler.enable()
   # ... run exporter ...
   profiler.disable()
   stats = pstats.Stats(profiler)
   stats.sort_stats('cumulative')
   stats.print_stats(10)
   ```

2. **Monitor exporter metrics**:
   ```bash
   # CPU usage
   top -p $(pgrep -f node_exporter.py)
   
   # Memory usage over time
   while true; do 
     ps aux | grep node_exporter.py | grep -v grep | awk '{print $6}'
     sleep 5
   done
   ```

3. **Reduce collection frequency**:
   - Increase sleep interval between collections
   - Use Prometheus recording rules for expensive queries
   - Cache static information

## Security Note

The exporter runs on port 9101 without authentication. In production:
- Use firewall rules to restrict access
- Consider using a reverse proxy with authentication
- Or use Prometheus node_exporter with custom text collectors

## Port Information

Port **9101** was chosen because:
- 9100 is the default node_exporter port
- 9090 is default Prometheus port  
- 9093 is Alertmanager
- 9094-9099 are commonly used by other exporters
- 9101 is uncommonly used and unlikely to conflict

You can change the port by modifying `EXPORTER_PORT = 9101` in the Python script.

## Performance Tuning

For large environments, consider these optimizations:

### 1. Adjust Collection Interval
```python
# In the script, change the sleep interval
time.sleep(30)  # Instead of 15 seconds
```

### 2. Disable Unused Collectors
If you don't need certain metrics, comment out their collection in `collect_all_metrics()`:
```python
# self.collect_vm_metrics()  # Disable if not using VMs
# self.collect_zfs_metrics()  # Disable if not using ZFS
```

### 3. Use Sampling for Process Metrics
For systems with many processes, sample instead of collecting all:
```python
# Add to process collection
if random.random() > 0.1:  # Sample 10% of processes
    continue
```

### 4. Prometheus Scrape Configuration
Optimize your Prometheus configuration:
```yaml
scrape_configs:
  - job_name: 'proxmox-nodes'
    scrape_interval: 30s     # Match exporter interval
    scrape_timeout: 25s      # Slightly less than interval
    metrics_path: '/metrics'
    static_configs:
      - targets:
        - 'dasha:9101'
        - 'eva:9101'
        - 'helen:9101'
        - 'polly:9101'
    metric_relabel_configs:
      # Drop less important metrics if needed
      - source_labels: [__name__]
        regex: 'node_cpu_usage_seconds_total'
        target_label: __tmp_cpu_mode
        replacement: '${1}'
      # Keep only specific modes
      - source_labels: [__tmp_cpu_mode, mode]
        regex: ';(idle|iowait|system|user)'
        action: keep
```

## High Availability Setup

For production environments, consider running the exporter with supervision:

### Using systemd with automatic restart
```ini
[Service]
Restart=always
RestartSec=10
StartLimitBurst=5
StartLimitInterval=60s
```

### Health Check Script
Create `/opt/proxmox-exporter/health_check.sh`:
```bash
#!/bin/bash
curl -sf http://localhost:9101/metrics > /dev/null
if [ $? -ne 0 ]; then
    systemctl restart proxmox-node-exporter
    logger "Proxmox exporter health check failed, restarted service"
fi
```

Add to crontab:
```bash
*/5 * * * * /opt/proxmox-exporter/health_check.sh
```

## Integration with Existing Monitoring

### If you already have node_exporter
You can run both exporters on different ports and merge metrics in Prometheus:
```yaml
scrape_configs:
  - job_name: 'node'
    static_configs:
      - targets: ['host:9100']  # Standard node_exporter
  - job_name: 'proxmox-extended'
    static_configs:
      - targets: ['host:9101']  # Our comprehensive exporter
```

### Migrate from Netdata
To replace Netdata with this exporter:
1. Export your Netdata dashboards
2. Map Netdata metrics to our metrics (most have similar names)
3. Update your alerting rules
4. Gradually transition by running both in parallel

## Comparison with Other Solutions

| Feature | Our Exporter | node_exporter | Netdata | Telegraf |
|---------|--------------|---------------|---------|----------|
| Proxmox VMs/CTs | ✅ Full | ❌ No | ⚠️ Limited | ⚠️ Plugin |
| Temperature sensors | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| ZFS metrics | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| Disk I/O details | ✅ Full | ✅ Full | ✅ Full | ✅ Full |
| Network details | ✅ Full | ✅ Full | ✅ Full | ✅ Full |
| Process metrics | ✅ Full | ⚠️ Basic | ✅ Full | ✅ Full |
| Memory footprint | ~50MB | ~20MB | ~200MB | ~40MB |
| CPU usage | <1% | <0.5% | 2-5% | <1% |
| Native Prometheus | ✅ Yes | ✅ Yes | ⚠️ Export | ⚠️ Export |
