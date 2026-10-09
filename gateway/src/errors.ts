/**
 * Consistent, client-safe error envelope shared with the Python service.
 */
import type { ErrorEnvelopeBody } from "./schemas.js";

export interface ErrorDetail {
  path: string;
  message: string;
}

export function envelope(
  code: string,
  message: string,
  requestId: string | null,
  options: { retryable?: boolean; details?: ErrorDetail[] } = {},
): ErrorEnvelopeBody {
  return {
    error: {
      code,
      message,
      request_id: requestId,
      retryable: options.retryable ?? false,
      details: options.details ?? [],
    },
  };
}

export function isErrorEnvelope(value: unknown): value is ErrorEnvelopeBody {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const error: unknown = value.error;
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    typeof error.code === "string" &&
    "message" in error &&
    typeof error.message === "string"
  );
}
