package com.dsc.nifi;

import io.grpc.StatusRuntimeException;
import org.apache.nifi.annotation.behavior.InputRequirement;
import org.apache.nifi.annotation.documentation.CapabilityDescription;
import org.apache.nifi.annotation.documentation.Tags;
import org.apache.nifi.annotation.lifecycle.OnScheduled;
import org.apache.nifi.annotation.lifecycle.OnStopped;
import org.apache.nifi.components.PropertyDescriptor;
import org.apache.nifi.components.PropertyValue;
import org.apache.nifi.expression.ExpressionLanguageScope;
import org.apache.nifi.flowfile.FlowFile;
import org.apache.nifi.processor.AbstractProcessor;
import org.apache.nifi.processor.ProcessContext;
import org.apache.nifi.processor.exception.ProcessException;
import org.apache.nifi.processor.ProcessSession;
import org.apache.nifi.processor.Relationship;
import org.apache.nifi.processor.util.StandardValidators;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;

@Tags({"python", "operator", "sandbox", "runner"})
@CapabilityDescription("Executes an immutable Python operator through Managed Python Runner v3.3")
@InputRequirement(InputRequirement.Requirement.INPUT_REQUIRED)
public class ManagedPythonTransform extends AbstractProcessor {

    private static final long MAX_INLINE_BYTES = 8L * 1024L * 1024L;

    static final PropertyDescriptor RUNTIME_SERVICE = new PropertyDescriptor.Builder()
            .name("runtime-service")
            .displayName("Managed Python Runtime Service")
            .required(true)
            .identifiesControllerService(ManagedPythonRuntimeService.class)
            .build();

    static final PropertyDescriptor TENANT_ID = new PropertyDescriptor.Builder()
            .name("tenant-id")
            .displayName("Tenant ID")
            .required(true)
            .addValidator(StandardValidators.NON_EMPTY_VALIDATOR)
            .build();

    static final PropertyDescriptor PROJECT_ID = new PropertyDescriptor.Builder()
            .name("project-id")
            .displayName("Project ID")
            .required(true)
            .addValidator(StandardValidators.NON_EMPTY_VALIDATOR)
            .build();

    static final PropertyDescriptor RELEASE_ID = new PropertyDescriptor.Builder()
            .name("release-id")
            .displayName("Operator Release ID")
            .required(true)
            .addValidator(StandardValidators.NON_EMPTY_VALIDATOR)
            .build();

    static final PropertyDescriptor INPUT_ATTRIBUTES = new PropertyDescriptor.Builder()
            .name("input-attributes")
            .displayName("Input Attribute Allow-list")
            .description("Comma-separated FlowFile attributes allowed to leave NiFi. "
                    + "The Runner independently applies the published operator allow-list.")
            .required(false)
            .defaultValue("")
            .build();

    static final PropertyDescriptor TIMEOUT_MS = new PropertyDescriptor.Builder()
            .name("timeout-ms")
            .displayName("Timeout (ms)")
            .required(true)
            .defaultValue("30000")
            .addValidator(StandardValidators.POSITIVE_INTEGER_VALIDATOR)
            .build();

    public static final Relationship SUCCESS = new Relationship.Builder()
            .name("success")
            .description("Operator completed successfully")
            .build();

    public static final Relationship FAILURE = new Relationship.Builder()
            .name("failure")
            .description("Operator completed with a non-retryable error")
            .build();

    public static final Relationship RETRY = new Relationship.Builder()
            .name("retry")
            .description("Runner reported a retryable infrastructure error")
            .build();

    private final AtomicReference<ManagedPythonRuntimeService.LeaseSession> lease =
            new AtomicReference<>();

    @Override
    protected List<PropertyDescriptor> getSupportedPropertyDescriptors() {
        return List.of(
                RUNTIME_SERVICE,
                TENANT_ID,
                PROJECT_ID,
                RELEASE_ID,
                INPUT_ATTRIBUTES,
                TIMEOUT_MS);
    }

    @Override
    public Set<Relationship> getRelationships() {
        return Set.of(SUCCESS, FAILURE, RETRY);
    }

    @Override
    protected PropertyDescriptor getSupportedDynamicPropertyDescriptor(final String name) {
        if (!name.startsWith("Parameter.") || name.length() <= "Parameter.".length()) {
            return null;
        }
        return new PropertyDescriptor.Builder()
                .name(name)
                .displayName(name)
                .description("Operator parameter " + name.substring("Parameter.".length()))
                .dynamic(true)
                .required(false)
                .addValidator(StandardValidators.NON_EMPTY_VALIDATOR)
                .expressionLanguageSupported(ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
                .build();
    }

    @OnScheduled
    public void onScheduled(final ProcessContext context) {
        final ManagedPythonRuntimeService runtime =
                context.getProperty(RUNTIME_SERVICE).asControllerService(ManagedPythonRuntimeService.class);
        lease.set(runtime.acquireLease(
                context.getProperty(TENANT_ID).getValue(),
                context.getProperty(PROJECT_ID).getValue(),
                getIdentifier(),
                context.getProperty(RELEASE_ID).getValue()));
    }

    @OnStopped
    public void onStopped(final ProcessContext context) {
        final ManagedPythonRuntimeService.LeaseSession current = lease.getAndSet(null);
        if (current == null) {
            return;
        }
        final ManagedPythonRuntimeService runtime =
                context.getProperty(RUNTIME_SERVICE).asControllerService(ManagedPythonRuntimeService.class);
        try {
            runtime.releaseLease(current);
        } catch (RuntimeException e) {
            getLogger().warn("Could not release Managed Python lease {}", current.leaseId(), e);
        }
    }

    @Override
    public void onTrigger(final ProcessContext context, final ProcessSession session)
            throws ProcessException {

        FlowFile flowFile = session.get();
        if (flowFile == null) {
            return;
        }

        if (flowFile.getSize() > MAX_INLINE_BYTES) {
            flowFile = session.putAttribute(flowFile, "managed.python.error.code", "INLINE_CONTENT_TOO_LARGE");
            session.transfer(flowFile, FAILURE);
            return;
        }

        final ManagedPythonRuntimeService runtime =
                context.getProperty(RUNTIME_SERVICE).asControllerService(ManagedPythonRuntimeService.class);

        final ManagedPythonRuntimeService.LeaseSession current;
        try {
            current = ensureLease(runtime);
        } catch (RuntimeException e) {
            throw new ProcessException("Could not acquire/renew Managed Python lease", e);
        }

        final byte[] content = readContent(session, flowFile);
        final Map<String, String> attributes = allowedAttributes(context, flowFile);
        final Map<String, String> parameters = parameters(context, flowFile);
        final String invocationId = UUID.randomUUID().toString();
        final String idempotencyKey = idempotencyKey(flowFile, current.releaseId());

        final ManagedPythonRuntimeService.InvocationResult result;
        try {
            result = runtime.invoke(
                    current,
                    invocationId,
                    idempotencyKey,
                    content,
                    attributes,
                    parameters,
                    context.getProperty(TIMEOUT_MS).asInteger());
        } catch (StatusRuntimeException e) {
            // Throwing rolls the session back. The next attempt uses the same FlowFile UUID and
            // therefore the same idempotency key.
            throw new ProcessException("Runner gRPC invocation failed: " + e.getStatus(), e);
        }

        if (!"SUCCEEDED".equals(result.status())) {
            flowFile = session.putAttribute(flowFile, "managed.python.error.code", result.errorCode());
            flowFile = session.putAttribute(flowFile, "managed.python.error.message", result.errorMessage());
            session.transfer(flowFile, result.retryable() ? RETRY : FAILURE);
            return;
        }

        final byte[] output = result.content();
        flowFile = session.write(flowFile, out -> out.write(output));
        if (!result.attributes().isEmpty()) {
            flowFile = session.putAllAttributes(flowFile, result.attributes());
        }
        session.transfer(flowFile, SUCCESS);
    }

    private synchronized ManagedPythonRuntimeService.LeaseSession ensureLease(
            final ManagedPythonRuntimeService runtime) {

        final ManagedPythonRuntimeService.LeaseSession current = lease.get();
        if (current == null) {
            throw new IllegalStateException("Processor has no lease");
        }
        final ManagedPythonRuntimeService.LeaseSession valid = runtime.ensureLease(current);
        lease.set(valid);
        return valid;
    }

    private static byte[] readContent(final ProcessSession session, final FlowFile flowFile) {
        final ByteArrayOutputStream buffer = new ByteArrayOutputStream((int) flowFile.getSize());
        session.read(flowFile, in -> {
            final byte[] chunk = new byte[64 * 1024];
            int read;
            while ((read = in.read(chunk)) >= 0) {
                buffer.write(chunk, 0, read);
            }
        });
        return buffer.toByteArray();
    }

    private static Map<String, String> allowedAttributes(
            final ProcessContext context,
            final FlowFile flowFile) {

        final String configured = context.getProperty(INPUT_ATTRIBUTES).getValue();
        if (configured == null || configured.isBlank()) {
            return Map.of();
        }
        final Map<String, String> result = new LinkedHashMap<>();
        for (String item : configured.split(",")) {
            final String name = item.trim();
            if (name.isEmpty()) {
                continue;
            }
            final String value = flowFile.getAttribute(name);
            if (value != null) {
                result.put(name, value);
            }
        }
        return result;
    }

    private static Map<String, String> parameters(
            final ProcessContext context,
            final FlowFile flowFile) {

        final Map<String, String> result = new LinkedHashMap<>();
        for (Map.Entry<PropertyDescriptor, String> entry : context.getProperties().entrySet()) {
            final PropertyDescriptor descriptor = entry.getKey();
            if (!descriptor.isDynamic() || !descriptor.getName().startsWith("Parameter.")) {
                continue;
            }
            final PropertyValue value = context.getProperty(descriptor);
            if (value == null || !value.isSet()) {
                continue;
            }
            result.put(
                    descriptor.getName().substring("Parameter.".length()),
                    value.evaluateAttributeExpressions(flowFile).getValue());
        }
        return result;
    }

    private String idempotencyKey(final FlowFile flowFile, final String releaseId) {
        final String flowFileUuid = flowFile.getAttribute("uuid");
        final String raw = getIdentifier() + "\n" + releaseId + "\n" + flowFileUuid;
        try {
            final byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(raw.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException e) {
            throw new ProcessException("SHA-256 is not available", e);
        }
    }
}
