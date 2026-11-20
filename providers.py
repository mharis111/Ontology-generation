"""Native provider adapters retained from the attached working v3 project."""
from __future__ import annotations
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any
Config = Any

class ProviderError(RuntimeError):

    def __init__(self, message, *, raw_text=None, metadata=None):
        super().__init__(message)
        self.raw_text = raw_text
        self.metadata = metadata or {}

def http_json(url, payload=None, *, headers=None, timeout=300, attempts=3):
    """Bounded retries for transient transport errors; never logs auth headers.

    A retry after a timeout may repeat a request already processed by a server.
    Request metadata and API keys are not written by this function.
    """
    request_headers = {'Accept': 'application/json', **(headers or {})}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        request_headers['Content-Type'] = 'application/json'
    for attempt in range(attempts):
        request = Request(url, data=data, headers=request_headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode('utf-8'))
            if not isinstance(result, dict):
                raise ProviderError('Expected a JSON object from the provider.')
            return result
        except HTTPError as exc:
            status = exc.code
            exc.close()
            transient = status in {408, 429, 500, 502, 503, 504}
            if not transient or attempt == attempts - 1:
                hints = {401: 'Check your API key.', 403: 'Check account permissions.', 404: 'Check the base URL and model name.', 400: 'Check model parameter support; try temperature=None.', 429: 'Check your rate limit or account quota.'}
                raise ProviderError(f"Provider HTTP {status}. {hints.get(status, 'Request failed.')}") from None
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt == attempts - 1:
                raise ProviderError('Cannot reach the provider or the request timed out. Check the server URL, network, and timeout.') from None
        except (ValueError, UnicodeError):
            raise ProviderError('Provider returned an invalid JSON response.') from None
        time.sleep(min(2 ** attempt, 8))

def list_ollama_models(config: Config):
    data = http_json(config.ollama_base_url.rstrip('/') + '/api/tags', timeout=config.timeout_seconds, attempts=config.http_attempts)
    return data.get('models', [])

def preflight(config: Config):
    config.validate()
    if config.provider == 'openai':
        if not os.environ.get('OPENAI_API_KEY', '').strip():
            raise ProviderError("Set OPENAI_API_KEY in the environment or through the notebook's getpass cell.")
        return {'status': 'configured', 'message': 'API key is present; model access is checked on generation.'}
    models = list_ollama_models(config)
    names = {m.get('name', '') for m in models}
    requested = config.model if ':' in config.model else config.model + ':latest'
    if config.model not in names and requested not in names:
        raise ProviderError(f"Ollama model {config.model!r} is not installed. Run 'ollama pull {config.model}' on the Ollama host. Installed: {sorted(names)}")
    return {'status': 'available', 'model': config.model, 'models': [{'name': m.get('name'), 'digest': m.get('digest')} for m in models]}

def generate_text(messages, config: Config, schema):
    """Native Ollama /api/chat or OpenAI /v1/chat/completions; no SDK needed."""
    if config.provider == 'ollama':
        options = {'num_predict': config.max_output_tokens, 'num_ctx': config.ollama_num_ctx}
        if config.temperature is not None:
            options['temperature'] = config.temperature
        if config.ollama_seed is not None:
            options['seed'] = config.ollama_seed
        payload = {'model': config.model, 'messages': messages, 'stream': False, 'options': options}
        if config.ollama_think is not None:
            payload['think'] = config.ollama_think
        if config.response_format in {'json', 'elements'}:
            payload['format'] = schema
        data = http_json(config.ollama_base_url.rstrip('/') + '/api/chat', payload, timeout=config.timeout_seconds, attempts=config.http_attempts)
        raw = data.get('message', {}).get('content', '')
        meta = {k: data[k] for k in ('model', 'done', 'done_reason', 'prompt_eval_count', 'eval_count', 'total_duration') if k in data}
        meta.update({'requested_num_predict': config.max_output_tokens, 'requested_num_ctx': config.ollama_num_ctx, 'content_characters': len(raw) if isinstance(raw, str) else 0, 'thinking_present': bool(data.get('message', {}).get('thinking')), 'response_format': config.response_format, 'requested_think': config.ollama_think})
        if data.get('done') is not True:
            raise ProviderError(f"Ollama did not report a completed response: done={data.get('done')!r}, done_reason={data.get('done_reason')!r}. Inspect the returned provider metadata; this alone does not establish a token-limit failure.", raw_text=raw, metadata=meta)
        if data.get('done_reason') == 'length':
            raise ProviderError(f"Ollama stopped with done_reason='length' (generated tokens={data.get('eval_count', 'unknown')}, requested num_predict={config.max_output_tokens}, prompt tokens={data.get('prompt_eval_count', 'unknown')}, requested num_ctx={config.ollama_num_ctx}). Output was truncated; no ontology was accepted. Increase the output budget within the model/context limits or shorten the requested output. If thinking is enabled, inspect the model's supported thinking controls.", raw_text=raw, metadata=meta)
    else:
        key = os.environ.get('OPENAI_API_KEY', '').strip()
        if not key:
            raise ProviderError('OPENAI_API_KEY is not set.')
        payload = {'model': config.model, 'messages': messages, 'max_completion_tokens': config.max_output_tokens}
        if config.response_format in {'json', 'elements'}:
            payload['response_format'] = {'type': 'json_object'}
        if config.temperature is not None:
            payload['temperature'] = config.temperature
        data = http_json(config.openai_base_url.rstrip('/') + '/chat/completions', payload, headers={'Authorization': 'Bearer ' + key}, timeout=config.timeout_seconds, attempts=config.http_attempts)
        choices = data.get('choices', [])
        if not choices:
            raise ProviderError('OpenAI returned no completion choices.')
        choice = choices[0]
        raw = choice.get('message', {}).get('content', '')
        meta = {'model': data.get('model'), 'usage': data.get('usage'), 'finish_reason': choice.get('finish_reason')}
        if choice.get('finish_reason') != 'stop' or choice.get('message', {}).get('refusal'):
            raise ProviderError(f"OpenAI completion was refused, filtered, or incomplete (finish_reason={choice.get('finish_reason')!r}). Check the model and output-token budget.", raw_text=raw, metadata=meta)
    if not isinstance(raw, str) or not raw.strip():
        raise ProviderError('The model returned no final-answer text. Inspect provider metadata and supported thinking controls/output budgets.', raw_text=raw, metadata=meta)
    return (raw, meta)
