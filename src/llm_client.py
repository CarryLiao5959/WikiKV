"""LLM client — wraps calls to an OpenAI-compatible chat-completion API."""

import json
import logging
import re
import time
import requests

import config

_llm_logger = logging.getLogger("ingest.llm")


def call_llm(
    system_prompt: str,
    user_prompt: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    expect_json: bool = False,
    timeout: int = 300,
    model: str | None = None,
    enable_thinking: bool = False,
) -> str:
    """调用 LLM API，返回文本响应。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    use_model = model or config.LLM_MODEL
    use_api_base = config.LLM_API_BASE

    if model and model != config.LLM_MODEL:
        _model_api_map = {
            config.LLM_PREMIUM_MODEL: config.LLM_PREMIUM_API_BASE,
            config.LLM_FAST_MODEL: config.LLM_FAST_API_BASE,
            config.LLM_STEP1_MODEL: config.LLM_STEP1_API_BASE,
            config.LLM_STEP2_MODEL: config.LLM_STEP2_API_BASE,
            config.LLM_QUERY_MODEL: config.LLM_QUERY_API_BASE,
            config.LLM_LINT_MODEL: config.LLM_LINT_API_BASE,
        }
        use_api_base = _model_api_map.get(model, config.LLM_API_BASE)

    headers = config.get_llm_headers()
    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": temperature if temperature is not None else config.LLM_TEMPERATURE,
        "max_tokens": max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }
    if expect_json:
        payload["response_format"] = {"type": "json_object"}

    url = f"{use_api_base}/chat/completions"

    _llm_logger.info(f"🤖 LLM 调用 | model={use_model} | sys_prompt={len(system_prompt)} chars | user_prompt={len(user_prompt)} chars | temp={temperature or config.LLM_TEMPERATURE} | max_tokens={max_tokens or config.LLM_MAX_TOKENS}")
    _llm_logger.info(f"   system_prompt 预览: {system_prompt[:200]}...")
    _llm_logger.info(f"   user_prompt 预览: {user_prompt[:300]}...")

    MAX_RETRIES_NORMAL = 3
    MAX_RETRIES_EXHAUSTED = 6
    _EXHAUSTED_CODES = {997}  # 服务端资源耗尽状态码

    def _is_exhausted_error(exc: Exception, resp_obj) -> bool:
        """判断是否为资源耗尽类错误（997 No available instances）。"""
        msg = str(exc).lower()
        if "no available instances" in msg or "get another instance failed" in msg:
            return True
        if resp_obj is not None and resp_obj.status_code in _EXHAUSTED_CODES:
            return True
        return False

    t_start = time.time()
    last_resp = None
    attempt = 0
    max_retries = MAX_RETRIES_NORMAL
    while attempt < max_retries:
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            last_resp = resp
            resp.raise_for_status()

            if resp.status_code != 200:
                raise RuntimeError(f"LLM 返回非200状态码: {resp.status_code}, body={resp.text[:200]}")

            if not resp.text or not resp.text.strip():
                raise RuntimeError(f"LLM 返回空响应 (status={resp.status_code})")

            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            if not content.strip() and reasoning.strip():
                source = "reasoning_content" if msg.get("reasoning_content") else "reasoning"
                print(f"  ⚠️ 模型返回 {source} 而非 content，thinking 可能未关闭")
                content = reasoning

            t_elapsed = time.time() - t_start
            usage = data.get("usage", {})
            _llm_logger.info(f"🤖 LLM 响应 | {t_elapsed:.1f}s | resp={len(content)} chars | prompt_tokens={usage.get('prompt_tokens', '?')} | completion_tokens={usage.get('completion_tokens', '?')} | total_tokens={usage.get('total_tokens', '?')}")
            _llm_logger.info(f"   响应预览: {content[:300]}...")

            return content.strip()
        except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError, RuntimeError) as e:
            exhausted = _is_exhausted_error(e, last_resp)

            if exhausted and max_retries < MAX_RETRIES_EXHAUSTED:
                max_retries = MAX_RETRIES_EXHAUSTED

            if attempt < max_retries - 1:
                if exhausted:
                    wait = min(15 * (2 ** min(attempt, 3)), 180)
                    print(f"  LLM 调用失败 (attempt {attempt+1}/{max_retries}): {e}，{wait}s 后重试...")
                    _llm_logger.warning(f"🤖 LLM 资源耗尽 (attempt {attempt+1}/{max_retries}): {e}，{wait}s 后重试")
                else:
                    wait = 2 ** (attempt + 1)
                    print(f"  LLM 调用失败 (attempt {attempt+1}/{max_retries}): {e}，{wait}s 后重试...")
                    _llm_logger.warning(f"🤖 LLM 调用失败 (attempt {attempt+1}/{max_retries}): {e}，{wait}s 后重试")
                time.sleep(wait)
            else:
                try:
                    print(f"  响应状态码: {last_resp.status_code}")
                    print(f"  响应内容: {last_resp.text[:500]}")
                    _llm_logger.error(f"🤖 LLM 调用最终失败: status={last_resp.status_code}, body={last_resp.text[:500]}")
                except Exception:
                    pass
                raise RuntimeError(f"LLM 调用失败: {e}") from e
            attempt += 1


def call_llm_json(system_prompt: str, user_prompt: str, model: str | None = None, temperature: float | None = None) -> dict:
    """调用 LLM 并解析 JSON 响应。"""
    try:
        text = call_llm(system_prompt, user_prompt, expect_json=True, model=model, temperature=temperature)
    except RuntimeError:
        text = call_llm(system_prompt, user_prompt, expect_json=False, model=model, temperature=temperature)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    m = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass

    raise RuntimeError(f"无法解析 LLM 的 JSON 输出:\n{text[:500]}")