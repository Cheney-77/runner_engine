# OpenSandbox deployment notes

v3.3 intentionally does not pretend that gVisor/Kata can be switched by a string label on one
worker request when the secure runtime is configured server-side.

Run/configure one OpenSandbox control-plane/runtime group per isolation class, for example:

```text
opensandbox-gvisor -> OCI runtime runsc
opensandbox-kata   -> Kata runtime
```

Then configure the runner:

```python
OpenSandboxBackend({
    "gvisor": {"url": "http://opensandbox-gvisor:8080", "api_key": "..."},
    "kata": {"url": "http://opensandbox-kata:8080", "api_key": "..."},
})
```

`config/policies.json` chooses the cluster. CPU, memory and deny-by-default network policy are sent
in each sandbox create request.

Keep OpenSandbox and the worker endpoint on a private network. `secureAccess` is requested and any
headers returned by the OpenSandbox endpoint API are forwarded by the runner.
