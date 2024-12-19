import re
import traceback
from typing import Any, Dict, List, Optional, Tuple

import cohere
from openai import OpenAI
from openai.resources.chat.completions import ChatCompletion
from pydantic import BaseModel

from leettools.common import exceptions
from leettools.common.logging import logger
from leettools.common.logging.event_logger import EventLogger
from leettools.common.utils import file_utils, time_utils, url_utils
from leettools.context_manager import Context
from leettools.core.schemas.api_provider_config import (
    APIEndpointInfo,
    APIFunction,
    APIProviderConfig,
)
from leettools.core.schemas.user import User
from leettools.core.user.user_settings_helper import get_value_from_settings
from leettools.eds.usage.schemas.usage_api_call import (
    API_CALL_ENDPOINT_COMPLETION,
    UsageAPICallCreate,
)


def run_inference_call_direct(
    context: Context,
    user: User,
    api_client: OpenAI,
    api_provider_name: str,
    model_name: str,
    model_options: Dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    need_json: Optional[bool] = True,
    call_target: Optional[str] = None,
    response_pydantic_model: Optional[BaseModel] = None,
    display_logger: Optional[EventLogger] = None,
) -> Tuple[str, ChatCompletion]:
    """
    The function to run the inference call directly.

    We should mostly use this function for inference calls. It will handle the default
    values, handle the return values, and log the usage in the usage store.

    Args:
    -  context: The context object
    -  user: The user object
    -  api_client: The OpenAI-compatible client
    -  api_provider_name: The name of the API provider
    -  model_name: The name of the model
    -  model_options: The options for the model, like temperature, max_tokens, etc.
    -  system_prompt: The system prompt
    -  user_prompt: The user prompt
    -  need_json: Whether the response needs to be converted to JSON
    -  call_target: The target of the call
    -  response_pydantic_model: The response Pydantic model
    -  display_logger: The display logger
    """

    if display_logger is None:
        display_logger = logger()

    usage_store = context.get_usage_store()
    settings = context.settings

    # handle the values of the model options, make it more fault tolerant
    temperature = model_options.get("temperature", None)
    if temperature is None:
        temperature = settings.DEFAULT_TEMPERATURE
    else:
        try:
            temperature = float(temperature)
        except ValueError as e:
            display_logger.warning(f"Error in parsing temperature {temperature}: {e}")
            temperature = settings.DEFAULT_TEMPERATURE

    if temperature < 0 or temperature > 2:
        display_logger.error(
            f"Invalid temperature {temperature} out of range [0, 2]. Using default."
        )
        temperature = settings.DEFAULT_TEMPERATURE

    max_tokens = model_options.get("max_tokens", None)
    if max_tokens is None:
        if settings.DEFAULT_MAX_TOKENS == -1:
            max_tokens = None
        else:
            max_tokens = settings.DEFAULT_MAX_TOKENS
    else:
        if max_tokens == "":
            max_tokens = None
        else:
            try:
                max_tokens = int(max_tokens)
            except ValueError as e:
                display_logger.error(f"Error in parsing max tokens {max_tokens}: {e}")
                max_tokens = None

    use_parsed = False
    if need_json:
        if response_pydantic_model is not None:
            format_dict = {"type": "json_schema"}
            use_parsed = True
        else:
            format_dict = {"type": "json_object"}
    else:
        format_dict = {"type": "text"}

    display_logger.debug(
        f"Final system prompt(first 500 chars): {system_prompt[:8000]}"
    )
    display_logger.debug(f"Final user prompt (first 500 chars): {user_prompt[:8000]}")

    start_timestamp_in_ms = time_utils.cur_timestamp_in_ms()
    completion = None
    try:
        if use_parsed:
            completion = api_client.beta.chat.completions.parse(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_pydantic_model,
            )
        else:
            completion = api_client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                response_format=format_dict,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        display_logger.info(
            f"({completion.usage.total_tokens}) tokens used for ({call_target})."
        )
    finally:
        end_timestamp_in_ms = time_utils.cur_timestamp_in_ms()
        if completion is not None:
            success = True
            total_token_count = completion.usage.total_tokens
            input_token_count = completion.usage.prompt_tokens
            output_token_count = completion.usage.completion_tokens
        else:
            success = False
            total_token_count = 0
            input_token_count = -1
            output_token_count = -1

        usage_api_call = UsageAPICallCreate(
            user_uuid=user.user_uuid,
            api_provider=api_provider_name,
            target_model_name=model_name,
            endpoint=API_CALL_ENDPOINT_COMPLETION,
            success=success,
            total_token_count=total_token_count,
            start_timestamp_in_ms=start_timestamp_in_ms,
            end_timestamp_in_ms=end_timestamp_in_ms,
            is_batch=False,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            call_target=call_target,
            input_token_count=input_token_count,
            output_token_count=output_token_count,
        )
        usage_store.record_api_call(usage_api_call)

    if completion is None:
        raise exceptions.UnexpectedOperationFailureException(
            operation_desc=f"OpenAI completion for {call_target}",
            error="Completion is None.",
        )

    if use_parsed:
        response_str = completion.choices[0].message.parsed.model_dump_json()
    else:
        response_str = completion.choices[0].message.content
        display_logger.debug(f"Response from inference call\n: {response_str}")
        if need_json:
            pattern = r"\n?```json\n?|\n?```\n?"
            response_str = re.sub(pattern, "", response_str)
            display_logger.debug(f"Clean up: {response_str}")
    return response_str, completion


def get_api_function_list() -> List[str]:
    return [api_function for api_function in APIFunction]


def get_openai_embedder_client_for_user(
    context: Context,
    user: User,
    api_provider_config: Optional[APIProviderConfig] = None,
) -> Tuple[APIProviderConfig, OpenAI]:
    if api_provider_config is None:
        api_provider_config = get_default_embed_api_provider_config(context, user)

    api_key = api_provider_config.api_key
    base_url = api_provider_config.base_url

    trace = traceback.format_stack()
    logger().info(
        f"Creating OpenAI embedding client with base_url: {base_url} "
        f"and api_key: {file_utils.redact_api_key(api_key)}"
    )
    # used to track where the call is coming from
    logger().noop(f"Calling Trace: {trace}")
    return api_provider_config, OpenAI(base_url=base_url, api_key=api_key)


def get_default_inference_api_provider_config(
    context: Context, user: Optional[User] = None
) -> APIProviderConfig:

    if user is None:
        user = User.get_admin_user()

    settings = context.settings
    user_settings = context.get_user_settings_store().get_settings_for_user(user)
    api_key = get_value_from_settings(
        context=context,
        user_settings=user_settings,
        default_env="OPENAI_API_KEY",
        first_key="OPENAI_API_KEY",
        second_key=None,
        allow_empty=False,
    )
    base_url = get_value_from_settings(
        context=context,
        user_settings=user_settings,
        default_env="DEFAULT_OPENAI_BASE_URL",
        first_key="OPENAI_BASE_URL",
        second_key=None,
        allow_empty=False,
    )

    tld = url_utils.get_first_level_domain_from_url(base_url)

    api_provider_config = APIProviderConfig(
        api_provider=tld,
        api_key=api_key,
        base_url=base_url,
        endpoints={
            APIFunction.INFERENCE: APIEndpointInfo(
                path="chat/completions",
                default_model=settings.DEFAULT_OPENAI_MODEL,
                supported_models=["gpt-3.5-turbo", "gpt-4.0", "gpt-4o", "gpt-4o-mini"],
            ),
        },
    )
    return api_provider_config


def get_default_embed_api_provider_config(
    context: Context, user: Optional[User] = None
) -> APIProviderConfig:

    if user is None:
        user = User.get_admin_user()

    settings = context.settings
    user_settings = context.get_user_settings_store().get_settings_for_user(user)
    api_key = get_value_from_settings(
        context=context,
        user_settings=user_settings,
        default_env="OPENAI_API_KEY",
        first_key="OPENAI_API_KEY",
        second_key=None,
        allow_empty=True,
    )
    base_url = get_value_from_settings(
        context=context,
        user_settings=user_settings,
        default_env="DEFAULT_OPENAI_BASE_URL",
        first_key="OPENAI_BASE_URL",
        second_key=None,
        allow_empty=False,
    )

    tld = url_utils.get_first_level_domain_from_url(base_url)

    return APIProviderConfig(
        api_provider=tld,
        api_key=api_key,
        base_url=base_url,
        endpoints={
            APIFunction.EMBED: APIEndpointInfo(
                path="embeddings",
                default_model=settings.DEFAULT_EMBEDDING_OPENAI_MODEL,
                supported_models=[settings.DEFAULT_EMBEDDING_OPENAI_MODEL],
            ),
        },
    )


def get_default_rerank_api_provider_config(
    context: Context, user: Optional[User] = None
) -> APIProviderConfig:

    if user is None:
        user = User.get_admin_user()

    settings = context.settings
    user_settings = context.get_user_settings_store().get_settings_for_user(user)

    api_key = get_value_from_settings(
        context=context,
        user_settings=user_settings,
        default_env="COHERE_API_KEY",
        first_key="COHERE_API_KEY",
        second_key=None,
        allow_empty=False,
    )
    base_url = get_value_from_settings(
        context=context,
        user_settings=user_settings,
        default_env="COHERE_BASE_URL",
        first_key="COHERE_BASE_URL",
        second_key=None,
        allow_empty=True,
    )

    if base_url is None:
        raise exceptions.ConfigValueException(
            "COHERE_BASE_URL",
            "COHERE_BASE_URL is not set. Please set COHERE_BASE_URL in the environment or user settings.",
        )

    tld = url_utils.get_first_level_domain_from_url(base_url)

    return APIProviderConfig(
        api_provider=tld,
        api_key=api_key,
        base_url=base_url,
        endpoints={
            APIFunction.RERANK: APIEndpointInfo(
                path="rerank",
                default_model=settings.DEFAULT_RERANK_MODEL,
                supported_models=[settings.DEFAULT_RERANK_MODEL],
            ),
        },
    )


def get_openai_client_for_user(
    context: Context, user: User, api_provider_config: APIProviderConfig
) -> OpenAI:

    if api_provider_config is None:
        logger().info(
            f"No API provider config provided. Checking user settings of {user.username} "
        )
        api_provider_config = get_default_inference_api_provider_config(context, user)

    api_key = api_provider_config.api_key
    base_url = api_provider_config.base_url

    trace = traceback.format_stack()
    logger().info(
        f"Creating OpenAI client with base_url: {base_url} "
        f"and api_key: {api_key[:5]}******{api_key[-5:]}"
    )
    logger().noop(f"Calling Trace: {trace}")
    return OpenAI(base_url=base_url, api_key=api_key)


def get_rerank_client_for_user(
    context: Context, user: User, api_provider_config: APIProviderConfig
) -> cohere.Client:
    """
    TODO: define a universal rerank client interface
    """

    if api_provider_config is None:
        api_provider_config = get_default_rerank_api_provider_config(context, user)

    api_key = api_provider_config.api_key
    base_url = api_provider_config.base_url

    if api_provider_config.base_url is not None:
        cohere_client = cohere.Client(api_key=api_key, base_url=base_url)
        logger().info(
            f"Creating Cohere client with API key: {api_key[:5]}******{api_key[-5:]} "
            f"and base_url {base_url}"
        )
    else:
        cohere_client = cohere.Client(api_key=api_key)
        logger().info(
            f"Creating Cohere client with API key: {api_key[:5]}******{api_key[-5:]} "
            "and default base_url"
        )
    return cohere_client