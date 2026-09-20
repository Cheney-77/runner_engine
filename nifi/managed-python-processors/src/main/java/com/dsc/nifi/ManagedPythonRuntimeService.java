package com.dsc.nifi;

import org.apache.nifi.controller.ControllerService;

import java.util.Map;

public interface ManagedPythonRuntimeService extends ControllerService {

    record LeaseSession(
            String tenantId,
            String projectId,
            String processorId,
            String releaseId,
            String leaseId,
            long expiresAtEpochMs) {
    }

    record InvocationResult(
            String status,
            String relationship,
            byte[] content,
            Map<String, String> attributes,
            boolean retryable,
            String errorCode,
            String errorMessage) {
    }

    LeaseSession acquireLease(
            String tenantId,
            String projectId,
            String processorId,
            String releaseId);

    LeaseSession ensureLease(LeaseSession lease);

    InvocationResult invoke(
            LeaseSession lease,
            String invocationId,
            String idempotencyKey,
            byte[] content,
            Map<String, String> attributes,
            Map<String, String> parameters,
            int timeoutMs);

    boolean cancel(LeaseSession lease, String invocationId);

    void releaseLease(LeaseSession lease);
}
