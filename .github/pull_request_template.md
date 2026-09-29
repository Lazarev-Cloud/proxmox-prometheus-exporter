## Summary

<!-- What does this change and why? -->

## Testing

<!-- How was it tested? Unit tests, `--once` on a real Proxmox host, ... -->

## Checklist

- [ ] `make check` passes (lint, types, tests, generated files)
- [ ] New metrics are declared with `MetricGroup`, documented (`make docs`) and follow Prometheus naming
- [ ] New collectors degrade gracefully when the hardware/software is absent
- [ ] External commands go through `Runner` with a timeout; untrusted values are validated before use in argv
- [ ] CHANGELOG.md updated for user-visible changes
