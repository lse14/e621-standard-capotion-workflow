from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from anima_caption_format import flat_txt_sha256
from anima_caption_format.normalizer import CaptionDisplayPolicy

from .budget import TokenBudgetError, fit, tokenizer_count_many
from .protocol import TokenBudgetHelloRequest, TokenBudgetPayloadError, parse_hello, parse_process, process_result
from .resource import TokenizerResource, TokenizerResourceError, load_tokenizer_resource


class TokenBudgetWorkerInitializationError(RuntimeError):
    pass


class TokenBudgetWorker:
    def __init__(self, *, tokenizer: object | None = None, resource_loader: Callable[..., TokenizerResource] = load_tokenizer_resource) -> None:
        self._injected_tokenizer = tokenizer
        self._resource_loader = resource_loader
        self.hello: TokenBudgetHelloRequest | None = None
        self.resource: TokenizerResource | None = None
        self.tokenizer = tokenizer

    def initialize(self, payload: object, *, resource_root: Path) -> dict[str, object]:
        if self.hello is not None:
            raise TokenBudgetWorkerInitializationError("Token Budget worker is already initialized")
        try:
            hello = parse_hello(payload)
            resource = self._resource_loader(
                resource_root,
                hello.resource_manifest_relative_path,
                hello.resource_id,
                hello.resource_fingerprint,
                hello.context_limit,
            )
        except (TokenBudgetPayloadError, TokenizerResourceError, TypeError, ValueError) as exc:
            raise TokenBudgetWorkerInitializationError("Token Budget worker initialization failed") from exc
        self.hello = hello
        self.resource = resource
        self.tokenizer = self._injected_tokenizer or resource.tokenizer
        return {"schemaVersion": 1, "payloadType": "token_budget_hello_result", "ready": True, "resourceFingerprint": resource.fingerprint, "contextLimit": resource.context_limit}

    def process_item(
        self,
        sample_id: int,
        lease_id: str,
        annotation: object,
        caption_format: Mapping[str, object],
        max_tokens: int,
    ) -> dict[str, object]:
        if self.tokenizer is None:
            raise TokenBudgetWorkerInitializationError("Token Budget worker is not initialized")
        try:
            result = fit(annotation, caption_format, max_tokens, lambda texts: tokenizer_count_many(self.tokenizer, texts))
            outcome: dict[str, object] = {
                "schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": sample_id, "leaseId": lease_id,
                "status": result.status, "originalTokens": result.original_tokens, "finalTokens": result.final_tokens,
                "removed": result.removed,
            }
            if result.annotation is not None:
                policy = CaptionDisplayPolicy.from_mapping(caption_format)
                outcome["annotation"] = result.annotation
                outcome["flatTextSha256"] = flat_txt_sha256(result.annotation, policy)
            return outcome
        except (TokenBudgetError, ValueError):
            return {"schemaVersion": 1, "payloadType": "token_budget_outcome", "sampleId": sample_id, "leaseId": lease_id, "status": "failed", "code": "token_budget_input_invalid"}

    def process(self, payload: object) -> dict[str, object]:
        if self.hello is None or self.tokenizer is None:
            raise TokenBudgetWorkerInitializationError("Token Budget worker is not initialized")
        request = parse_process(payload)
        outcomes = [self.process_item(item.sample_id, item.lease_id, item.annotation, request.caption_format, self.hello.max_tokens) for item in request.items]
        return process_result(outcomes)
