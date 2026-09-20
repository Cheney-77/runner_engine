package com.dsc.nifi;

import com.dsc.nifi.proto.AcquireLeaseRequest;
import com.dsc.nifi.proto.CancelRequest;
import com.dsc.nifi.proto.InvokeRequest;
import com.dsc.nifi.proto.InvokeResponse;
import com.dsc.nifi.proto.LeaseResponse;
import com.dsc.nifi.proto.ManagedPythonRunnerGrpc;
import com.dsc.nifi.proto.ReleaseLeaseRequest;
import com.dsc.nifi.proto.RenewLeaseRequest;
import com.google.protobuf.ByteString;
import io.grpc.ManagedChannel;
import io.grpc.StatusRuntimeException;
import io.grpc.netty.shaded.io.grpc.netty.GrpcSslContexts;
import io.grpc.netty.shaded.io.grpc.netty.NettyChannelBuilder;
import io.grpc.netty.shaded.io.netty.handler.ssl.SslContext;
import org.apache.nifi.annotation.documentation.CapabilityDescription;
import org.apache.nifi.annotation.documentation.Tags;
import org.apache.nifi.components.PropertyDescriptor;
import org.apache.nifi.controller.AbstractControllerService;
import org.apache.nifi.controller.ConfigurationContext;
import org.apache.nifi.processor.util.StandardValidators;
import org.apache.nifi.annotation.lifecycle.OnDisabled;
import org.apache.nifi.annotation.lifecycle.OnEnabled;

import javax.net.ssl.SSLException;
import java.io.File;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

@Tags({"python", "runner", "grpc", "sandbox"})
@CapabilityDescription("mTLS gRPC client for Managed Python Runner v3.3")
public class GrpcManagedPythonRuntimeService extends AbstractControllerService
        implements ManagedPythonRuntimeService {

    static final PropertyDescriptor TARGET = new PropertyDescriptor.Builder()
            .name("runner-target")
            .displayName("Runner Gateway")
            .description("gRPC target, for example runner.internal:9443")
            .required(true)
            .addValidator(StandardValidators.NON_EMPTY_VALIDATOR)
            .build();

    static final PropertyDescriptor CA_CERT = new PropertyDescriptor.Builder()
            .name("runner-ca-cert")
            .displayName("Runner CA PEM")
            .description("CA certificate used to verify the Runner Gateway")
            .required(true)
            .addValidator(StandardValidators.NON_EMPTY_VALIDATOR)
            .build();

    static final PropertyDescriptor CLIENT_CERT = new PropertyDescriptor.Builder()
            .name("runner-client-cert")
            .displayName("Client Certificate PEM")
            .description("mTLS client certificate identifying this NiFi cluster")
            .required(true)
            .addValidator(StandardValidators.NON_EMPTY_VALIDATOR)
            .build();

    static final PropertyDescriptor CLIENT_KEY = new PropertyDescriptor.Builder()
            .name("runner-client-key")
            .displayName("Client Private Key PEM")
            .description("mTLS private key. Restrict filesystem permissions on this file.")
            .required(true)
            .sensitive(true)
            .addValidator(StandardValidators.NON_EMPTY_VALIDATOR)
            .build();

    private volatile ManagedChannel channel;
    private volatile ManagedPythonRunnerGrpc.ManagedPythonRunnerBlockingStub stub;

    @Override
    protected List<PropertyDescriptor> getSupportedPropertyDescriptors() {
        return List.of(TARGET, CA_CERT, CLIENT_CERT, CLIENT_KEY);
    }

    @OnEnabled
    public void onEnabled(final ConfigurationContext context) throws SSLException {
        final SslContext sslContext = GrpcSslContexts.forClient()
                .trustManager(new File(context.getProperty(CA_CERT).getValue()))
                .keyManager(
                        new File(context.getProperty(CLIENT_CERT).getValue()),
                        new File(context.getProperty(CLIENT_KEY).getValue()))
                .build();

        channel = NettyChannelBuilder
                .forTarget(context.getProperty(TARGET).getValue())
                .sslContext(sslContext)
                .maxInboundMessageSize(10 * 1024 * 1024)
                .build();

        stub = ManagedPythonRunnerGrpc.newBlockingStub(channel);
    }

    @OnDisabled
    public void onDisabled() {
        final ManagedChannel current = channel;
        stub = null;
        channel = null;
        if (current != null) {
            current.shutdown();
            try {
                if (!current.awaitTermination(5, TimeUnit.SECONDS)) {
                    current.shutdownNow();
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                current.shutdownNow();
            }
        }
    }

    @Override
    public LeaseSession acquireLease(
            final String tenantId,
            final String projectId,
            final String processorId,
            final String releaseId) {

        final LeaseResponse response = requireStub()
                .acquireLease(AcquireLeaseRequest.newBuilder()
                        .setTenantId(tenantId)
                        .setProjectId(projectId)
                        .setProcessorId(processorId)
                        .setReleaseId(releaseId)
                        .build());

        return new LeaseSession(
                tenantId,
                projectId,
                processorId,
                releaseId,
                response.getLeaseId(),
                response.getExpiresAtEpochMs());
    }

    @Override
    public LeaseSession ensureLease(final LeaseSession lease) {
        final long renewBefore = System.currentTimeMillis() + 60_000;
        if (lease.expiresAtEpochMs() > renewBefore) {
            return lease;
        }

        try {
            final LeaseResponse response = requireStub()
                    .renewLease(RenewLeaseRequest.newBuilder()
                            .setTenantId(lease.tenantId())
                            .setLeaseId(lease.leaseId())
                            .build());
            return new LeaseSession(
                    lease.tenantId(),
                    lease.projectId(),
                    lease.processorId(),
                    lease.releaseId(),
                    response.getLeaseId(),
                    response.getExpiresAtEpochMs());
        } catch (StatusRuntimeException e) {
            final String detail = e.getStatus().getDescription();
            if (detail != null
                    && (detail.startsWith("LEASE_UNKNOWN:")
                    || detail.startsWith("LEASE_EXPIRED:"))) {
                return acquireLease(
                        lease.tenantId(),
                        lease.projectId(),
                        lease.processorId(),
                        lease.releaseId());
            }
            throw e;
        }
    }

    @Override
    public InvocationResult invoke(
            final LeaseSession lease,
            final String invocationId,
            final String idempotencyKey,
            final byte[] content,
            final Map<String, String> attributes,
            final Map<String, String> parameters,
            final int timeoutMs) {

        final InvokeResponse response = requireStub()
                .invoke(InvokeRequest.newBuilder()
                        .setTenantId(lease.tenantId())
                        .setLeaseId(lease.leaseId())
                        .setReleaseId(lease.releaseId())
                        .setInvocationId(invocationId)
                        .setIdempotencyKey(idempotencyKey)
                        .setContent(ByteString.copyFrom(content))
                        .putAllAttributes(attributes)
                        .putAllParameters(parameters)
                        .setTimeoutMs(timeoutMs)
                        .build());

        return new InvocationResult(
                response.getStatus(),
                response.getRelationship(),
                response.getContent().toByteArray(),
                Map.copyOf(response.getAttributesMap()),
                response.getRetryable(),
                response.getErrorCode(),
                response.getErrorMessage());
    }

    @Override
    public boolean cancel(final LeaseSession lease, final String invocationId) {
        return requireStub().cancel(CancelRequest.newBuilder()
                        .setTenantId(lease.tenantId())
                        .setLeaseId(lease.leaseId())
                        .setInvocationId(invocationId)
                        .build())
                .getCancelled();
    }

    @Override
    public void releaseLease(final LeaseSession lease) {
        requireStub().releaseLease(ReleaseLeaseRequest.newBuilder()
                .setTenantId(lease.tenantId())
                .setLeaseId(lease.leaseId())
                .build());
    }

    private ManagedPythonRunnerGrpc.ManagedPythonRunnerBlockingStub requireStub() {
        final ManagedPythonRunnerGrpc.ManagedPythonRunnerBlockingStub current = stub;
        if (current == null) {
            throw new IllegalStateException("Runner service is not enabled");
        }
        return current;
    }
}
