"""The Google Generative AI Conversation integration."""

from __future__ import annotations

from functools import partial
import logging
import mimetypes
from pathlib import Path
from typing import Literal

from google.api_core.exceptions import ClientError
import google.ai.generativelanguage as glm
import google.generativeai as genai
import google.generativeai.types as genai_types
import voluptuous as vol

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, MATCH_ALL
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    TemplateError,
)
from homeassistant.helpers import config_validation as cv, intent, template
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import ulid

from .const import (
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_PROMPT,
    CONF_TEMPERATURE,
    CONF_TOP_K,
    CONF_TOP_P,
    DEFAULT_CHAT_MODEL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
SERVICE_GENERATE_CONTENT = "generate_content"
CONF_IMAGE_FILENAME = "image_filename"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Google Generative AI Conversation."""


    async def generate_content(call: ServiceCall) -> ServiceResponse:
        """Generate content from text and optionally images."""
        prompt_parts = [call.data[CONF_PROMPT]]
        image_filenames = call.data[CONF_IMAGE_FILENAME]
        for image_filename in image_filenames:
            if not hass.config.is_allowed_path(image_filename):
                raise HomeAssistantError(
                    f"Cannot read `{image_filename}`, no access to path; "
                    "`allowlist_external_dirs` may need to be adjusted in "
                    "`configuration.yaml`"
                )
            if not Path(image_filename).exists():
                raise HomeAssistantError(f"`{image_filename}` does not exist")
            mime_type, _ = mimetypes.guess_type(image_filename)
            if mime_type is None or not mime_type.startswith("image"):
                raise HomeAssistantError(f"`{image_filename}` is not an image")
            prompt_parts.append(
                {
                    "mime_type": mime_type,
                    "data": await hass.async_add_executor_job(
                        Path(image_filename).read_bytes
                    ),
                }
            )

        model_name = "gemini-pro-vision" if image_filenames else "gemini-pro"
        model = genai.GenerativeModel(model_name=model_name)

        try:
            response = await hass.async_add_executor_job(model.generate_content_async(prompt_parts))
        except (
            ClientError,
            ValueError,
            genai_types.BlockedPromptException,
            genai_types.StopCandidateException,
        ) as err:
            raise HomeAssistantError(f"Error generating content: {err}") from err

        return {"text": response.text}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_CONTENT,
        generate_content,
        schema=vol.Schema(
            {
                vol.Required(CONF_PROMPT): cv.string,
                vol.Optional(CONF_IMAGE_FILENAME, default=[]): vol.All(
                    cv.ensure_list, [cv.string]
                ),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Google Generative AI Conversation from a config entry."""
    genai.configure(api_key=entry.data[CONF_API_KEY])

    try:
        await hass.async_add_executor_job(
            partial(
                genai.get_model, entry.options.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL)
            )
        )
    except ClientError as err:
        if err.reason == "API_KEY_INVALID":
            _LOGGER.error("Invalid API key: %s", err)
            return False
        raise ConfigEntryNotReady(err) from err

    conversation.async_set_agent(hass, entry, GoogleGenerativeAIAgent(hass, entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload GoogleGenerativeAI."""
    genai.configure(api_key=None)
    conversation.async_unset_agent(hass, entry)
    return True


class GoogleGenerativeAIAgent(conversation.AbstractConversationAgent):
    """Google Generative AI conversation agent."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the agent."""
        self.hass = hass
        self.entry = entry
        self.history: dict[str, list[genai_types.ContentType]] = {}

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process a sentence."""

        control_light = glm.Tool(
            function_declarations=[
            glm.FunctionDeclaration(
                name="control_light",
                description="""
                    Turn a light on or off.

                    Args:
                        command: Whether to "turn_on" or "turn_off" the light.

                    Example input: Light on
                    Example output: {"command": "turn_on"}

                    Example input: Light off
                    Example output: {"command": "turn_off"}
                """,
                parameters=glm.Schema(
                    type=glm.Type.OBJECT,
                    properties={
                        "command":glm.Schema(type=glm.Type.STRING),
                    },
                    required=["command"]
                )
            )
            ])

        set_heating_and_cooling = glm.Tool(
            function_declarations=[
            glm.FunctionDeclaration(
                name="set_heating_and_cooling",
                description="""
                    Set the heat and cooling temperatures of the thermostat.

                    Args:
                        cool_temp: The cool temperature to set to in °F. Must be set to the heat temp plus 3.
                        heat_temp: The heat temperature to set to in °F. Must be set to the cool temp minus 3.

                    Example input: Set the heat to 74 degrees.
                    Example output: {"cool_temp": 77, "heat_temp": 74}

                    Example input: Set the cooling to 73 degrees.
                    Example output: {"cool_temp": 73, "heat_temp": 69}
                """,
                parameters=glm.Schema(
                    type=glm.Type.OBJECT,
                    properties={
                        "cool_temp":glm.Schema(type=glm.Type.INTEGER),
                        "heat_temp":glm.Schema(type=glm.Type.INTEGER),
                    },
                    required=["cool_temp", "heat_temp"]
                )
            )
            ])

        raw_prompt = self.entry.options.get(CONF_PROMPT, DEFAULT_PROMPT)
        model = genai.GenerativeModel(
            model_name=self.entry.options.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL),
            tools=[
                control_light,
                set_heating_and_cooling,
                ],
            generation_config={
                "temperature": self.entry.options.get(
                    CONF_TEMPERATURE, DEFAULT_TEMPERATURE
                ),
                "top_p": self.entry.options.get(CONF_TOP_P, DEFAULT_TOP_P),
                "top_k": self.entry.options.get(CONF_TOP_K, DEFAULT_TOP_K),
                "max_output_tokens": self.entry.options.get(
                    CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS
                ),
            },
        )
        _LOGGER.debug("Model: %s", model)

        if user_input.conversation_id in self.history:
            conversation_id = user_input.conversation_id
            messages = self.history[conversation_id]
        else:
            conversation_id = ulid.ulid_now()
            messages = [{}, {}]

        try:
            prompt = self._async_generate_prompt(raw_prompt)
        except TemplateError as err:
            _LOGGER.error("Error rendering prompt: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Sorry, I had a problem with my template: {err}",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=conversation_id
            )

        messages[0] = {"role": "user", "parts": prompt}
        messages[1] = {"role": "model", "parts": "Ok"}

        _LOGGER.debug("Input: '%s' with history: %s", user_input.text, messages)

        chat = model.start_chat(history=messages)
        try:
            chat_response = await chat.send_message_async(user_input.text)
        except (
            ClientError,
            ValueError,
            genai_types.BlockedPromptException,
            genai_types.StopCandidateException,
        ) as err:
            _LOGGER.error("Error sending message: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Sorry, I had a problem talking to Google Generative AI: {err}",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=conversation_id
            )

        _LOGGER.debug("Response: %s", chat_response.parts)
        self.history[conversation_id] = chat.history

        fc = chat_response.candidates[0].content.parts[0].function_call
        _LOGGER.debug("Function Call: %s", fc)

        if fc.name == "control_light":
            _LOGGER.debug(f"Setting light to {fc.args["command"]}")
            await self.hass.services.async_call(
                "homeassistant",
                fc.args["command"],
                {"entity_id": "light.1234567890"},
                False,
            )

        if fc.name == "set_heating_and_cooling":
            _LOGGER.debug(f"Setting thermostat to {fc.args["heat_temp"]} °F and {fc.args["cool_temp"]} °F")
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {
                    "target_temp_low": fc.args["heat_temp"],
                    "target_temp_high": fc.args["cool_temp"],
                    "entity_id": "climate.thermostat",

                },
                False,
            )

        chat_response = await chat.send_message_async(
            glm.Content(
            parts=[glm.Part(
                function_response = glm.FunctionResponse(
                name=fc.name,
                response={'result': f"Running {fc.name} with {fc.args}. Command ran successfully!"}))]))

        for content in chat.history:
            part = content.parts[0]
            _LOGGER.info(content.role, "->", type(part).to_dict(part))
            _LOGGER.info('-'*80)

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(chat_response.text)
        return conversation.ConversationResult(
            response=intent_response, conversation_id=conversation_id
        )

    def _async_generate_prompt(self, raw_prompt: str) -> str:
        """Generate a prompt for the user."""
        return template.Template(raw_prompt, self.hass).async_render(
            {
                "ha_name": self.hass.config.location_name,
            },
            parse_result=False,
        )
