# NiFi adapter

The v3.3 NiFi adapter deliberately has only two public pieces:

- `ManagedPythonTransform`: the Processor users place on a flow.
- `GrpcManagedPythonRuntimeService`: one shared Controller Service that owns the mTLS channel.

Notably absent:

- no `security_profile` property -- policy is attached to the trusted published release;
- no Python environment or pip logic on the NiFi node;
- no plaintext gRPC mode;
- no "send every FlowFile attribute" behavior.

## Lease behavior

The Processor acquires a lease on scheduling and calls `ensureLease` before each invocation. The
Controller Service renews the lease when it has less than one minute remaining and reacquires it if
the Runner reports `LEASE_UNKNOWN` or `LEASE_EXPIRED`.

## Idempotency

The key is stable across NiFi retries:

```text
SHA256(processor-id + release-id + FlowFile uuid)
```

`invocation_id` remains unique per physical attempt and is used for cancellation/observability.

## Attribute boundary

`Input Attribute Allow-list` controls what may leave the NiFi JVM. The Runner applies the release's
published allow-list again. Returned attributes are also filtered on the Runner.

## Build

The Maven files target NiFi 2.0.0 / Java 21 as a reference baseline. Align `nifi.version` and the
Java release with the exact NiFi distribution used by your platform before shipping the NAR.

```bash
cd nifi
mvn clean package
```

The Python test suite in this bundle is executed in the delivery environment. The NiFi module is
provided as source but is not Maven-built here because this environment has no Maven dependency
network access.
