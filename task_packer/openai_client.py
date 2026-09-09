"""Jedno miejsce komunikacji z OpenAI Responses API."""

from __future__ import annotations

import base64
import getpass
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

from .costs import MAX_PROJECT_COST_USD, MODEL_PRICES, TokenUsage, usage_cost
from .storage import atomic_write_text


DEFAULT_MODEL = "gpt-6-astra"
MODEL_CONTEXT_WINDOWS = {"gpt-6-astra": 1_050_000}
ATTACHMENT_INPUT_RESERVE_USD = 1.00


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
        self.model = model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
        if self.model not in MODEL_PRICES:
            raise RuntimeError(
                f"Model {self.model!r} has no price entry, so the budget cannot be enforced."
            )
        usage = starting_usage or {}
        self.input_tokens = int(usage.get("input_tokens", 0))
        self.cached_input_tokens = int(usage.get("cached_input_tokens", 0))
        self.output_tokens = int(usage.get("output_tokens", 0))
        self.cost_usd = float(usage.get("cost_usd", 0.0))
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

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        details = getattr(usage, "input_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        self.input_tokens += input_tokens
        self.cached_input_tokens += cached
        self.output_tokens += output_tokens
        self.cost_usd += usage_cost(
            self.model,
            TokenUsage(
                input_tokens=input_tokens,
                cached_input_tokens=cached,
                output_tokens=output_tokens,
            ),
        )

    def usage_dict(self) -> dict[str, float | int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 8),
        }

    def _ensure_budget(self, max_tokens: int, input_bound: int | None = None) -> None:
        """Do not start a request that could exceed the $0.50 cap in the worst case."""

        prices = MODEL_PRICES[self.model]
        context = MODEL_CONTEXT_WINDOWS[self.model]
        possible_output = min(max_tokens, context)
        possible_input = max(0, context - possible_output) if input_bound is None else input_bound
        if possible_input + possible_output > context:
            raise RuntimeError('Materials exceed the conservative context limit. Split or shorten the sources.')
        input_cost = (
            ATTACHMENT_INPUT_RESERVE_USD
            if input_bound is None
            else possible_input * prices["input"] / 1_000_000
        )
        worst_request_cost = input_cost + possible_output * prices["output"] / 1_000_000
        if self.cost_usd + worst_request_cost > MAX_PROJECT_COST_USD:
            raise RuntimeError(
                f"The next request was not started because it could exceed the "
                f"${MAX_PROJECT_COST_USD:.2f} budget. Spent so far: "
                f"${self.cost_usd:.4f}. Progress is saved."
            )

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
        if self._client is None:
            api_key = os.environ.get("OPENAI_API_KEY") or saved_api_key()
            if not api_key:
                raise RuntimeError("OpenAI API key is missing. Run ./run.sh --setup; your own materials and saved drafts can be edited without a key.")
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError("The openai package is missing. Run: pip install -r requirements.txt") from error
            self._client = OpenAI(api_key=api_key)
        # UTF-8 bytes bound text token count conservatively; leave room for framing.
        # Media tokenization is external, so retain the context ceiling for attachments.
        input_bound = None if attachments else len((system + user).encode('utf-8')) + 4096
        self._ensure_budget(max_tokens, input_bound)
        event = {'model': self.model, 'started_at': time.time(), 'max_output_tokens': max_tokens,
                 'input_bound': input_bound, 'status': 'started'}
        if json_schema is not None:
            event['response_schema'] = schema_name
        if not hasattr(self, 'events'):
            self.events = []
        self.events.append(event)
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
            'reasoning': {'effort': 'low'},
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
        try:
            response = self._client.responses.create(**request)
        except Exception as error:
            event.update(status='failed', error_type=type(error).__name__)
            code = getattr(error, "status_code", None)
            if code == 429:
                message = "The OpenAI account rate or budget limit was reached (429)"
            else:
                message = f"OpenAI interrupted generation: {error}"
            raise RuntimeError(
                f"{message}. Progress before this request is saved; running again "
                "will repeat only the current item."
            ) from error

        self._record_usage(response)
        event.update(status='received', response_id=getattr(response, 'id', None),
                     cumulative_usage=self.usage_dict())
        text = getattr(response, "output_text", None)
        if not text:
            event['status'] = 'empty'
            raise RuntimeError(
                "The model returned no text. Progress before this request is saved."
            )
        event['status'] = 'completed'
        return str(text).strip()
