"""Public exception types and serializable batch error information."""

from pydantic import BaseModel


class OpenSACError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


class CapabilityError(OpenSACError):
    """Expected capability failure; providers may supply their own error code."""

    def __init__(self, code: str, message: str, status: int = 502, retryable: bool = False) -> None:
        super().__init__(code, message, status_code=status, retryable=retryable)
        self.status = status


class _TypedError(CapabilityError):
    code = "capability_error"
    default_status = 502
    default_retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(
            self.code,
            message,
            self.default_status,
            self.default_retryable if retryable is None else retryable,
        )


class InvalidRequestError(_TypedError):
    code = "invalid_request"
    default_status = 422


class InvalidSchemaError(InvalidRequestError):
    code = "invalid_schema"


class ConfigurationError(_TypedError):
    code = "configuration_error"
    default_status = 503


class NotConfiguredError(ConfigurationError):
    code = "not_configured"


class ClientClosedError(_TypedError):
    code = "client_closed"
    default_status = 409


class RuntimeClosedError(_TypedError):
    code = "runtime_closed"
    default_status = 409


class RequestTimeoutError(_TypedError):
    code = "request_timeout"
    default_status = 504
    default_retryable = True


class ProviderError(_TypedError):
    code = "provider_error"


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    default_status = 504
    default_retryable = True


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"
    default_retryable = True


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limited"
    default_status = 429
    default_retryable = True


class ProviderResponseError(ProviderError):
    code = "provider_invalid_response"


class ResponseTooLargeError(ProviderError):
    code = "response_too_large"


class ModelRefusalError(ProviderError):
    code = "model_refusal"


class GenerationIncompleteError(ProviderError):
    code = "generation_incomplete"


class StructuredOutputError(ProviderError):
    code = "structured_output_invalid"


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False
