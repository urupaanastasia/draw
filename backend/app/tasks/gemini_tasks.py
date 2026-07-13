import asyncio
import base64
import os
import urllib.parse
from io import BytesIO
from typing import Any, Dict, List, Optional, Union

import requests
from google import genai
from google.genai import types
from PIL import Image

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.redis import redis_service
from app.tasks.tasks import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    AsyncAITask,
    GenericPromptTask,
)

# ==============================================================================
# КОНФІГУРАЦІЯ
# ==============================================================================

DEFAULT_MODEL = "gemini-2.0-flash"
DEFAULT_IMAGE_GEN_MODEL = "gemini-2.0-flash"

CLAUDE_FALLBACK_MODEL = "claude-3-5-sonnet-20241022"
CEREBRAS_FALLBACK_MODEL = "llama3.1-8b"

DEBUG_IMAGE_DIR = "debug_images"
os.makedirs(DEBUG_IMAGE_DIR, exist_ok=True)

# Ключові слова стилю: милі, пухкі, закруглені мультяшні 3D моделі (Pixar-style)
STYLE_KEYWORDS = (
    "cute chubby rounded 3D model, soft smooth plastic/clay texture, "
    "stylized cartoonish design, no sharp edges, vibrant pastel colors, "
    "claymation style, pixar-like"
)

# Пояснення для моделей, як поєднувати малюнок від руки та твій текст
COMBINE_INSTRUCTION = (
    "You will receive a hand-drawn sketch and, optionally, a short text label written by the user. "
    "If a text label is provided, it names the MAIN OBJECT that must be generated (e.g. 'cat', 'house', 'car') — "
    "the text always takes priority in deciding WHAT the object is. "
    "The sketch itself should only influence the object's shape, pose, proportions, and any obvious base colors — "
    "it does NOT override the text label. "
    "If no text label is provided, infer the object purely from the sketch."
)


def build_system_prompt() -> str:
    """Системний промпт для Gemini/Claude: аналіз малюнка + тексту -> опис 3D-моделі."""
    return (
        "You are an AI that converts hand-drawn sketches into descriptions of adorable 3D toy-like models. "
        f"{COMBINE_INSTRUCTION} "
        "Analyze the sketch (and text label, if present) and describe it as a stunning, highly-detailed "
        f"3D model concept in the following mandatory visual style: {STYLE_KEYWORDS}, "
        "isolated on a clean solid white background, game asset, isometric view. "
        "Your response must be ONLY a single descriptive English prompt suitable for an image generator — "
        "no preamble, no explanation, nothing else. "
        "Example output: A cute chubby rounded 3D model of a cat, soft smooth clay texture, stylized cartoonish "
        "design, no sharp edges, vibrant pastel colors, isolated on a clean solid white background, game asset, "
        "isometric view."
    )


def build_fallback_prompt(prompt: str) -> str:
    """Крок 4 (фінальний): жорстко прописаний локальний шаблон, якщо всі API впали."""
    subject = prompt.strip() if prompt and prompt.strip() else "cute round object"
    return (
        f"A cute, chubby, rounded 3D low-poly model of a {subject}, soft smooth plastic texture, "
        f"vibrant colors, isolated on solid white background, game asset, isometric view, {STYLE_KEYWORDS}"
    )


# ==============================================================================
# GEMINI CLIENT / BASE TASK
# ==============================================================================

async def get_gemini_client():
    client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    return client


class AsyncGeminiTask(AsyncAITask):
    """Base class for Gemini Celery tasks that use async functions."""
    _client = None

    @property
    async def client(self):
        if self._client is None:
            self._client = await get_gemini_client()
        return self._client


class GeminiPromptTask(GenericPromptTask, AsyncGeminiTask):
    """Task to stream a text+image prompt with Gemini 2.0 Flash."""

    def prepare_message_params(self, prompt: str, system_prompt: Optional[str] = None,
                               max_tokens: int = DEFAULT_MAX_TOKENS,
                               temperature: float = DEFAULT_TEMPERATURE,
                               additional_params: Optional[Dict[str, Any]] = None,
                               image_base64: Optional[str] = None) -> Dict[str, Any]:
        if not image_base64:
            raise ValueError("Image base64 is required")

        system_prompt = system_prompt or build_system_prompt()

        try:
            image = Image.open(BytesIO(base64.b64decode(image_base64)))
            contents = [system_prompt, image] if not prompt else [system_prompt, f"Text label: {prompt}", image]
        except Exception as e:
            print(f"[ERROR] Error processing input image: {str(e)}")
            raise ValueError(f"Failed to process input image: {str(e)}")

        message_params = {
            "model": DEFAULT_MODEL,
            "contents": contents
        }
        if additional_params:
            message_params.update(additional_params)

        return message_params

    async def send_message(self, client, message_params: Dict[str, Any]) -> Any:
        model_name = message_params.pop("model")
        return await client.aio.models.generate_content(model=model_name, **message_params)

    def extract_content(self, response: Any) -> str:
        return response.text

    def prepare_final_response(self, task_id: str, response: Any, content: str) -> Dict[str, Any]:
        return {
            "status": "success",
            "content": content,
            "model": DEFAULT_MODEL,
            "usage": {
                "input_tokens": getattr(response.usage_metadata, "prompt_token_count", 0),
                "output_tokens": getattr(response.usage_metadata, "candidates_token_count", 0),
                "total_tokens": getattr(response.usage_metadata, "total_token_count", 0)
            },
            "task_id": task_id
        }


# ==============================================================================
# ЛАНЦЮЖОК РЕЗЕРВНИХ ПРОВАЙДЕРІВ (Gemini -> Claude -> Cerebras -> local template)
# ==============================================================================

async def _analyze_with_gemini(image_base64: str, prompt: str) -> str:
    """Крок 1: Google Gemini."""
    client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    system_prompt = build_system_prompt()

    image = Image.open(BytesIO(base64.b64decode(image_base64)))
    contents = [system_prompt, image] if not prompt else [system_prompt, f"Text label: {prompt}", image]

    response = await client.aio.models.generate_content(
        model=DEFAULT_IMAGE_GEN_MODEL,
        contents=contents,
    )

    if not response or not getattr(response, "text", None):
        raise RuntimeError("Gemini returned an empty response")

    return response.text.strip()


async def _analyze_with_claude(image_base64: str, prompt: str) -> str:
    """Крок 2: Anthropic Claude."""
    system_prompt = build_system_prompt()
    user_text = f"Text label: {prompt}" if prompt else "No text label provided, infer the object from the sketch."

    try:
        from app.tasks.claude_tasks import ClaudePromptTask  # type: ignore

        # ВИПРАВЛЕНО: Створюємо екземпляр класу через дужки ()
        claude_task = ClaudePromptTask()
        client = await claude_task.client if hasattr(claude_task, "client") else None
        if client is None:
            raise ImportError("ClaudePromptTask has no usable client")

        message_params = claude_task.prepare_message_params(
            prompt=user_text,
            system_prompt=system_prompt,
            max_tokens=300,
            temperature=DEFAULT_TEMPERATURE,
            image_base64=image_base64,
        )
        response = await claude_task.send_message(client, message_params)
        content = claude_task.extract_content(response)
        if not content or not content.strip():
            raise RuntimeError("ClaudePromptTask returned empty content")
        return content.strip()

    except (ImportError, AttributeError, Exception) as import_err:
        print(f"[INFO] ClaudePromptTask failed or not available ({import_err}), falling back to direct Anthropic SDK.")

        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError as sdk_err:
            raise RuntimeError(f"Anthropic SDK is not installed: {sdk_err}")

        client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

        response = await client.messages.create(
            model=CLAUDE_FALLBACK_MODEL,
            max_tokens=300,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_base64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )

        if not response.content:
            raise RuntimeError("Claude returned an empty response")

        text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        result = " ".join(text_blocks).strip()
        if not result:
            raise RuntimeError("Claude returned no text content")
        return result


async def _analyze_with_cerebras(prompt: str) -> str:
    """Крок 3: Cerebras (тільки текст)."""
    if not prompt or not prompt.strip():
        raise RuntimeError("Cerebras fallback requires a non-empty text prompt")

    system_prompt = build_system_prompt()
    user_text = (
        f"Text label: {prompt}. There is no sketch available in this step, "
        "base the description purely on this text label."
    )

    try:
        from app.tasks.cerebras_tasks import CerebrasPromptTask  # type: ignore

        # ВИПРАВЛЕНО: Створюємо екземпляр класу через дужки ()
        cerebras_task = CerebrasPromptTask()
        client = await cerebras_task.client if hasattr(cerebras_task, "client") else None
        if client is None:
            raise ImportError("CerebrasPromptTask has no usable client")

        message_params = cerebras_task.prepare_message_params(
            prompt=user_text,
            system_prompt=system_prompt,
            max_tokens=300,
            temperature=DEFAULT_TEMPERATURE,
        )
        response = await cerebras_task.send_message(client, message_params)
        content = cerebras_task.extract_content(response)
        if not content or not content.strip():
            raise RuntimeError("CerebrasPromptTask returned empty content")
        return content.strip()

    except (ImportError, AttributeError, Exception) as import_err:
        print(f"[INFO] CerebrasPromptTask failed or not available ({import_err}), falling back to direct Cerebras SDK.")

        try:
            from cerebras.cloud.sdk import Cerebras  # type: ignore
        except ImportError as sdk_err:
            raise RuntimeError(f"Cerebras SDK is not installed: {sdk_err}")

        client = Cerebras(api_key=settings.CEREBRAS_API_KEY)
        loop = asyncio.get_event_loop()

        def _call():
            return client.chat.completions.create(
                model=CEREBRAS_FALLBACK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                max_tokens=300,
            )

        response = await loop.run_in_executor(None, _call)
        content = response.choices[0].message.content
        if not content or not content.strip():
            raise RuntimeError("Cerebras returned an empty response")
        return content.strip()


# ==============================================================================
# GEMINI IMAGE GENERATION TASK (SSE, з повним ланцюжком fallback-ів)
# ==============================================================================

class GeminiImageGenerationTask(GenericPromptTask, AsyncGeminiTask):
    """Task to generate images from a sketch+prompt with SSE streaming support."""

    def prepare_message_params(self, prompt: str, system_prompt: Optional[str] = None,
                               max_tokens: int = DEFAULT_MAX_TOKENS,
                               temperature: float = DEFAULT_TEMPERATURE,
                               additional_params: Optional[Dict[str, Any]] = None,
                               image_base64: Optional[str] = None) -> Dict[str, Any]:
        if not image_base64:
            raise ValueError("Image base64 is required")

        system_prompt = system_prompt or build_system_prompt()

        try:
            image = Image.open(BytesIO(base64.b64decode(image_base64)))
            contents = [system_prompt, image] if not prompt else [system_prompt, f"Text label: {prompt}", image]
        except Exception as e:
            print(f"[ERROR] Error processing input image: {str(e)}")
            raise ValueError(f"Failed to process input image: {str(e)}")

        message_params = {
            "model": DEFAULT_IMAGE_GEN_MODEL,
            "contents": contents
        }

        if additional_params:
            message_params.update(additional_params)

        return message_params

    async def _run_async(self, task_id: str, image_base64: str, prompt: str = "",
                         system_prompt: Optional[str] = None,
                         max_tokens: int = DEFAULT_MAX_TOKENS,
                         temperature: float = DEFAULT_TEMPERATURE,
                         additional_params: Optional[Dict[str, Any]] = None):
        final_prompt: Optional[str] = None
        used_source: Optional[str] = None

        try:
            redis_service.publish_start_event(task_id)
        except Exception as redis_err:
            print(f"[WARN] Failed to publish start event: {redis_err}")

        try:
            # --- Крок 1: Google Gemini ---
            try:
                final_prompt = await _analyze_with_gemini(image_base64, prompt)
                used_source = "gemini"
                print(f"[INFO] Step 1 OK (Gemini): {final_prompt}")

            except Exception as gemini_err:
                print(f"[WARN] Step 1 failed (Gemini): {gemini_err}. Trying Claude...")

                # --- Крок 2: Claude (Anthropic) ---
                try:
                    final_prompt = await _analyze_with_claude(image_base64, prompt)
                    used_source = "claude"
                    print(f"[INFO] Step 2 OK (Claude fallback): {final_prompt}")

                except Exception as claude_err:
                    print(f"[WARN] Step 2 failed (Claude): {claude_err}. Trying Cerebras...")

                    # --- Крок 3: Cerebras (тільки текст) ---
                    try:
                        final_prompt = await _analyze_with_cerebras(prompt)
                        used_source = "cerebras"
                        print(f"[INFO] Step 3 OK (Cerebras fallback): {final_prompt}")

                    except Exception as cerebras_err:
                        print(f"[WARN] Step 3 failed (Cerebras): {cerebras_err}. Using local template.")

                        # --- Крок 4: локальний жорсткий fallback ---
                        final_prompt = build_fallback_prompt(prompt)
                        used_source = "local_template"
                        print(f"[INFO] Step 4 (local fallback template): {final_prompt}")

            print(f"[FINAL PROMPT FOR POLLINATIONS] (source={used_source}): {final_prompt}")

            # Відправка фінального тексту до генератора зображень Pollinations
            encoded_prompt = urllib.parse.quote(final_prompt)
            image_url = f"https://image.pollinations.ai/p/{encoded_prompt}?width=512&height=512&seed=42&enhanced=true"

            try:
                img_res = requests.get(image_url, timeout=20)
                if img_res.status_code == 200 and img_res.content:
                    final_image_base64 = base64.b64encode(img_res.content).decode('utf-8')
                else:
                    print(f"[WARN] Pollinations returned status {img_res.status_code}. Using original sketch.")
                    final_image_base64 = image_base64
            except Exception as poll_err:
                print(f"[WARN] Pollinations request failed: {poll_err}. Using original sketch.")
                final_image_base64 = image_base64

            final_response = {
                "status": "success",
                "task_id": task_id,
                "content": final_prompt,
                "model": used_source or DEFAULT_IMAGE_GEN_MODEL,
                "result": {
                    "image_base64": final_image_base64,
                    "prompt": final_prompt,
                    "source": used_source,
                },
                "images": [{
                    "image_id": f"{task_id}_0",
                    "image_base64": final_image_base64,
                    "width": 512,
                    "height": 512
                }]
            }

            try:
                redis_service.publish_complete_event(task_id, final_response)
                if hasattr(redis_service, 'store_response'):
                    redis_service.store_response(task_id, final_response)
            except Exception as redis_err:
                print(f"[WARN] Failed to publish/store final response in Redis: {redis_err}")

            return final_response

        except Exception as e:
            error_response = {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "task_id": task_id
            }
            try:
                redis_service.publish_error_event(task_id, e)
                if hasattr(redis_service, 'store_response'):
                    redis_service.store_response(task_id, error_response)
            except Exception:
                pass
            return error_response

    def run(self, task_id: str, image_base64: str, prompt: str = "",
            system_prompt: Optional[str] = None,
            max_tokens: int = DEFAULT_MAX_TOKENS,
            temperature: float = DEFAULT_TEMPERATURE,
            additional_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        result = loop.run_until_complete(
            self._run_async(
                task_id=task_id,
                image_base64=image_base64,
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                additional_params=additional_params
            )
        )
        return result

    async def send_message(self, client, message_params: Dict[str, Any]) -> Any:
        model_name = message_params.pop("model")
        return await client.aio.models.generate_content(model=model_name, **message_params)

    def prepare_final_response(self, task_id: str, response: Any, content: str) -> Dict[str, Any]:
        image_results = []
        for idx, part in enumerate(response.candidates[0].content.parts):
            if part.inline_data is not None:
                image_bytes = part.inline_data.data
                image_path = os.path.join(DEBUG_IMAGE_DIR, f"{task_id}_{idx}.jpg")
                try:
                    img = Image.open(BytesIO(image_bytes))
                    img.save(image_path)
                    width, height = img.size
                except Exception as e:
                    print(f"[ERROR] Failed to save image: {str(e)}")
                    width, height = 500, 500

                image_base64 = base64.b64encode(image_bytes).decode('utf-8')
                image_results.append({
                    "image_id": f"{task_id}_{idx}",
                    "image_base64": image_base64,
                    "saved_path": image_path,
                    "width": width,
                    "height": height
                })

        return {
            "status": "success",
            "content": content,
            "model": DEFAULT_IMAGE_GEN_MODEL,
            "images": image_results,
            "usage": {
                "input_tokens": getattr(response.usage_metadata, "prompt_token_count", 0),
                "output_tokens": getattr(response.usage_metadata, "candidates_token_count", 0),
                "total_tokens": getattr(response.usage_metadata, "total_token_count", 0)
            },
            "task_id": task_id
        }


# Коректна реєстрація тасок у Celery
GeminiPromptTask = celery_app.register_task(GeminiPromptTask())
GeminiImageGenerationTask = celery_app.register_task(GeminiImageGenerationTask())
