# Contributing to Smart Adaptive Proxmox Node Exporter

We welcome contributions! This document provides guidelines for contributing to the project.

## 🚀 Quick Start for Contributors

1. **Fork** the repository
2. **Clone** your fork: `git clone https://github.com/yourusername/smart-proxmox-exporter.git`
3. **Create** a feature branch: `git checkout -b feature/amazing-feature`
4. **Test** your changes on a Proxmox environment
5. **Commit** with clear messages: `git commit -m "Add support for XYZ hardware"`
6. **Push** to your branch: `git push origin feature/amazing-feature`
7. **Open** a Pull Request

## 🔧 Development Setup

### Prerequisites
- Python 3.7+
- Proxmox VE test environment (or VM)
- Basic understanding of Prometheus metrics

### Local Development
```bash
# Clone the repository
git clone https://github.com/lazarev-cloud/smart-proxmox-exporter.git
cd smart-proxmox-exporter

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install prometheus-client psutil

# Run the exporter
python3 proxmox-node-exporter.py
```

## 📝 Code Guidelines

### Python Code Style
- Follow PEP 8
- Use meaningful variable names
- Add docstrings for all functions and classes
- Handle exceptions gracefully
- Log important events and errors

### Example Function Structure
```python
def collect_new_hardware_metrics(self):
    """Collect metrics from new hardware type
    
    This function detects and collects metrics from XYZ hardware.
    It should handle cases where the hardware is not present.
    """
    if not self.features['new_hardware']:
        return
    
    try:
        with self.collection_duration.labels(collector='new_hardware').time():
            # Collection logic here
            pass
    except Exception as e:
        logger.error(f"Error collecting new hardware metrics: {e}")
        self.collection_errors.labels(collector='new_hardware').inc()
```

## 🎯 Contribution Areas

### High Priority
- **New Hardware Support**: GPU vendors, sensor types, storage systems
- **Performance Optimizations**: Reduce collection time and memory usage
- **Documentation**: Examples, troubleshooting, integration guides
- **Testing**: Unit tests, integration tests

### Hardware Support Requests
We're actively looking for contributions to support:
- Intel Arc GPUs
- More IPMI sensor types
- NVMe-specific metrics
- Raspberry Pi GPIO sensors
- Custom hardware sensors

### Software Integration
- Docker container metrics
- Kubernetes cluster information
- Additional hypervisor support
- Cloud provider metadata

## 🧪 Testing

### Manual Testing
1. Test on different Proxmox versions (6.x, 7.x, 8.x)
2. Test with different hardware configurations
3. Verify metrics accuracy with `curl http://localhost:9101/metrics`
4. Check Grafana dashboard rendering

### Test Checklist
- [ ] Exporter starts without errors
- [ ] Metrics endpoint responds (200 OK)
- [ ] Feature detection works correctly
- [ ] No memory leaks during long runs
- [ ] Prometheus can scrape successfully
- [ ] Grafana dashboard displays correctly

## 📊 Adding New Metrics

### Metric Naming Convention
Follow Prometheus naming conventions:
- Use `node_` prefix for node-level metrics
- Use `pve_` prefix for Proxmox-specific metrics
- Use descriptive names: `node_gpu_temperature_celsius`
- Include units in metric names where applicable

### Example: Adding New Hardware Support
```python
# 1. Add feature detection
def _detect_features(self):
    # ... existing code ...
    
    # Check for new hardware
    if self._check_new_hardware():
        self.features['new_hardware'] = True
        logger.info("✓ New hardware detected")

# 2. Initialize metrics
def _init_new_hardware_metrics(self):
    self.new_hardware_metric = Gauge(
        'node_new_hardware_value',
        'Description of the metric',
        ['label1', 'label2'],
        registry=self.registry
    )

# 3. Collect metrics
def collect_new_hardware_metrics(self):
    # Implementation here
    pass

# 4. Add to main collection loop
def collect_all_metrics(self):
    # ... existing collectors ...
    
    if self.features['new_hardware']:
        self.collect_new_hardware_metrics()
```

## 🐛 Bug Reports

### Good Bug Report Template
```markdown
**Environment:**
- Proxmox VE version: 
- Python version: 
- Hardware: 
- OS: 

**Expected Behavior:**
What should happen

**Actual Behavior:**
What actually happens

**Steps to Reproduce:**
1. Step one
2. Step two
3. ...

**Logs:**
```
journalctl -u proxmox-node-exporter -n 100
```

**Additional Context:**
Any other relevant information
```

## 💡 Feature Requests

### Good Feature Request Template
```markdown
**Is your feature request related to a problem?**
A clear description of the problem.

**Describe the solution you'd like**
What you want to happen.

**Describe alternatives you've considered**
Other solutions you've thought about.

**Hardware/Software Details**
- Hardware type: 
- Available commands/APIs: 
- Sample output: 

**Additional context**
Screenshots, documentation links, etc.
```

## 🎖️ Recognition

Contributors will be:
- Mentioned in release notes
- Added to the project's contributor list

## 📜 Code of Conduct

- Be respectful and inclusive
- Focus on constructive feedback
- Help newcomers learn
- Maintain a welcoming environment

## ❓ Questions?

- Open a GitHub Discussion for general questions
- Check existing issues and documentation first

## 📋 Pull Request Checklist

Before submitting a PR, ensure:
- [ ] Code follows project style guidelines
- [ ] All tests pass (or work on your system while no tests yet)
- [ ] Documentation is updated
- [ ] Commit messages are clear
- [ ] No sensitive information is included
- [ ] Feature detection works on systems without the hardware
- [ ] Error handling is implemented
- [ ] Logging is appropriate

Thank you for contributing! 🚀
