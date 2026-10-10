"""Jedno miejsce komunikacji z OpenAI Responses API."""

from __future__ import annotations

import base64
import getpass
import json
import math
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

from .costs import TokenUsage, usage_cost
from .settings import API_BASE_URL, Settings, model_spec
from .storage import atomic_write_text


def api_key_path() -> Path:
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    )
    return config_home / "solve4-task-packer/openai-api-key"


def saved_api_key() -> str:
    path = api_key_path()
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        return ""


def configure_api_key() -> None:
    from .console import heading

    heading("OpenAI configuration", "The key is required only to generate or revise materials.")
    if os.environ.get("OPENAI_API_KEY"):
        print("Ready. The program will use OPENAI_API_KEY from the environment.")
        return
    key = getpass.getpass(
        "OpenAI API key (Enter keeps the saved key): "
    ).strip() or saved_api_key()
    if not key:
        raise RuntimeError(
            "OpenAI API key is missing. Run ./run.sh --setup again and paste the key."
        )
    path = api_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old_mask = os.umask(0o077)
    try:
        atomic_write_text(path, key + "\n")
        path.chmod(0o600)
    finally:
        os.umask(old_mask)
    print(f"Ready. Private key saved in: {path}")


class OpenAIClient:
    """GPT client using the OpenAI Responses API with persistent cost tracking."""

    def __init__(
        self,
        model: str | None = None,
        starting_usage: dict[str, float | int] | None = None,
    ):
        self.settings = Settings.from_environment(model)
        self.model = self.settings.model
        self.spec = model_spec(self.model)
        usage = starting_usage or {}
        self.input_tokens = int(usage.get("input_tokens", 0))
        self.cached_input_tokens = int(usage.get("cached_input_tokens", 0))
        self.cache_write_tokens = int(usage.get("cache_write_tokens", 0))
        self.output_tokens = int(usage.get("output_tokens", 0))
        self.cost_usd = float(usage.get("cost_usd", 0.0))
        self.unconfirmed_cost_usd = float(usage.get("unconfirmed_cost_usd", 0.0))
        if (any(type(usage.get(key, 0)) is not int or usage.get(key, 0) < 0
                for key in ('input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens'))
                or any(not math.isfinite(value) or value < 0
                       for value in (self.cost_usd, self.unconfirmed_cost_usd))):
            raise ValueError("Saved API usage must contain finite nonnegative cost and integer token counts.")
        self._client = None
        self.events: list[dict] = []

    @staticmethod
    def _data_url(path: Path) -> str:
        data = path.read_bytes()
        suffix = path.suffix.lower()
        # Detect supported image formats from their bytes, independent of the OS
        # MIME registry (which may not know WEBP or uppercase extensions).
        mime = None
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime = "image/webp"
        if suffix in {".png", ".jpg", ".jpeg", ".webp"} and mime is None:
            raise RuntimeError(f"File {path} has no recognized PNG/JPEG/WEBP header. Save it again as an image.")
        if suffix == ".pdf":
            mime = "application/pdf"
        mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _content(self, user: str, attachments: list[Path]) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": user}]
        for path in attachments:
            suffix = path.suffix.lower()
            if suffix == ".pdf":
                content.append(
                    {
                        "type": "input_file",
                        "filename": path.name,
                        "file_data": self._data_url(path),
                        # Low detail substantially reduces cost; the PDF text is
                        # still sent in full through the API.
                        "detail": "low",
                    }
                )
            elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
                content.append(
                    {
                        "type": "input_image",
                        "image_url": self._data_url(path),
                        "detail": "low",
                    }
                )
            else:
                content.append(
                    {
                        "type": "input_file",
                        "filename": path.name,
                        "file_data": self._data_url(path),
                    }
                )
        return content

    @staticmethod
    def _field(value: Any, name: str, default: Any = None) -> Any:
        return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)

    def _record_usage(self, response: Any) -> bool:
        usage = self._field(response, "usage")
        if usage is None or any(self._field(usage, key) is None for key in ('input_tokens', 'output_tokens')):
            return False
        input_tokens = int(self._field(usage, "input_tokens", 0) or 0)
        output_tokens = int(self._field(usage, "output_tokens", 0) or 0)
        details = self._field(usage, "input_tokens_details")
        cached = int(self._field(details, "cached_tokens", 0) or 0)
        writes = int(self._field(details, "cache_write_tokens", 0) or 0)
        self.input_tokens += input_tokens
        self.cached_input_tokens += cached
        self.cache_write_tokens += writes
        self.output_tokens += output_tokens
        self.cost_usd += usage_cost(
            self.model,
            TokenUsage(
                input_tokens=input_tokens,
                cached_input_tokens=cached,
                output_tokens=output_tokens,
                cache_write_tokens=writes,
            ),
        )
        return True

    def usage_dict(self) -> dict[str, float | int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "unconfirmed_cost_usd": self.unconfirmed_cost_usd,
        }

    def _ensure_budget(self, max_tokens: int, input_bound: int | None = None) -> float:
        """Bound one request, including media, cache writes and long-context rates."""

        if type(max_tokens) is not int or not 1 <= max_tokens <= self.spec.max_output_tokens:
            raise ValueError(f"max_tokens must be an integer from 1 to {self.spec.max_output_tokens:,} for {self.model}.")
        context = self.spec.context_window
        possible_output = max_tokens
        possible_input = max(0, context - possible_output) if input_bound is None else input_bound
        if possible_input + possible_output > context:
            raise RuntimeError('Materials exceed the conservative context limit. Split or shorten the sources.')
        prices = self.spec.prices(possible_input)
        input_cost = possible_input * max(prices['input'], prices['cache_write']) / 1_000_000
        worst_request_cost = input_cost + possible_output * prices["output"] / 1_000_000
        cap = self.settings.budget_usd
        if cap is not None and self.cost_usd + self.unconfirmed_cost_usd + worst_request_cost > cap:
            raise RuntimeError(
                f"The next request was not started because it could exceed the "
                f"${cap:g} project budget. Spent so far: ${self.cost_usd:.4f}; "
                f"unconfirmed requests reserved: ${self.unconfirmed_cost_usd:.4f}; "
                f"next request bound: ${worst_request_cost:.4f}. Progress is saved. "
                "Increase or unset PACKER_BUDGET_USD to resume."
            )
        return worst_request_cost

    def generate(
        self,
        system: str,
        user: str,
        max_tokens: int = 6000,
        attachments: list[Path] | None = None,
    ) -> str:
        return self._generate_response(system, user, max_tokens, attachments, None, None)

    def generate_json(
        self,
        system: str,
        user: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int = 6000,
        attachments: list[Path] | None = None,
    ) -> str:
        """Generate an object conforming to a strict Responses API JSON Schema."""

        return self._generate_response(
            system, user, max_tokens, attachments, schema, schema_name
        )

    def _generate_response(
        self,
        system: str,
        user: str,
        max_tokens: int,
        attachments: list[Path] | None,
        json_schema: dict[str, Any] | None,
        schema_name: str | None,
    ) -> str:
        # UTF-8 bytes bound text token count conservatively; leave room for framing.
        # Media tokenization is external, so retain the context ceiling for attachments.
        schema_text = json.dumps(json_schema, ensure_ascii=False) if json_schema is not None else ''
        text_bound = len((system + user + schema_text + (schema_name or '')).encode('utf-8')) + 4096
        if attachments and not self.spec.image_input:
            raise ValueError(f"{self.model} does not support image/PDF inputs.")
        if json_schema is not None and not self.spec.structured_output:
            raise ValueError(f"{self.model} does not support structured output.")
        input_bound = None if attachments else text_bound
        worst_request_cost = self._ensure_budget(max_tokens, input_bound)
        if text_bound + max_tokens > self.spec.context_window:
            raise RuntimeError('Materials exceed the conservative context limit. Split or shorten the sources.')
        event = {'model': self.model, 'started_at': time.time(), 'max_output_tokens': max_tokens,
                 'input_bound': input_bound if input_bound is not None else self.spec.context_window - max_tokens,
                 'request_cost_bound_usd': worst_request_cost,
                 'project_budget_usd': self.settings.budget_usd, 'status': 'started'}
        if json_schema is not None:
            event['response_schema'] = schema_name
        request: dict[str, Any] = {
            'model': self.model,
            'instructions': system,
            'input': [
                {
                    'role': 'user',
                    'content': self._content(user, attachments or []),
                }
            ],
            'max_output_tokens': max_tokens,
            'reasoning': {'effort': self.spec.reasoning_effort},
            'service_tier': 'default',
            'truncation': 'disabled',
        }
        if json_schema is not None:
            request['text'] = {
                'format': {
                    'type': 'json_schema',
                    'name': schema_name or 'response',
                    'schema': json_schema,
                    'strict': True,
                }
            }
        if self._client is None:
            configured_url = os.environ.get('OPENAI_BASE_URL', API_BASE_URL).rstrip('/')
            if configured_url != API_BASE_URL:
                raise ValueError('OPENAI_BASE_URL must be https://api.openai.com/v1; other endpoint prices are not configured.')
            api_key = os.environ.get("OPENAI_API_KEY") or saved_api_key()
            if not api_key:
                raise RuntimeError("OpenAI API key is missing. Run ./run.sh --setup; your own materials and saved drafts can be edited without a key.")
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError("The openai package is missing. Run: pip install -r requirements.txt") from error
            # Hidden SDK retries could spend more than the one-call preflight.
            self._client = OpenAI(api_key=api_key, base_url=API_BASE_URL, max_retries=0)
        self.events.append(event)
        try:
            response = self._client.responses.create(**request)
        except BaseException as error:
            # An interrupted request can have been billed. Preserve returned
            # usage when present; otherwise keep a separate conservative reserve.
            reported = self._record_usage(error)
            if not reported:
                for candidate in (getattr(error, 'body', None), getattr(error, 'response', None)):
                    if self._record_usage(candidate):
                        reported = True
                        break
            if not reported:
                self.unconfirmed_cost_usd += worst_request_cost
            event.update(status='failed', error_type=type(error).__name__)
            event.update(usage_confirmed=reported, cumulative_usage=self.usage_dict())
            if not isinstance(error, Exception):
                raise
            code = getattr(error, "status_code", None)
            if code == 429:
                message = "The OpenAI account rate or budget limit was reached (429)"
            else:
                message = f"OpenAI interrupted generation: {error}"
            raise RuntimeError(
                f"{message}. Progress before this request is saved; running again "
                "will repeat only the current item."
            ) from error

        reported = self._record_usage(response)
        if not reported:
            self.unconfirmed_cost_usd += worst_request_cost
        event.update(status='received', response_id=getattr(response, 'id', None),
                     usage_confirmed=reported, cumulative_usage=self.usage_dict())
        if getattr(response, 'status', None) == 'failed':
            event['status'] = 'failed'
            raise RuntimeError('OpenAI returned a failed response. Usage and progress are saved.')
        text = getattr(response, "output_text", None)
        if not text or not str(text).strip():
            event['status'] = 'empty'
            raise RuntimeError(
                "The model returned no text. Progress before this request is saved."
            )
        event['status'] = 'completed'
        return str(text).strip()
