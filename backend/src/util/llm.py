import os

from dotenv import load_dotenv

load_dotenv()


def get_llm():
    """Chat model chosen by LLM_MODEL as "provider:model", e.g. "openai:gpt-4o-mini".

    Any provider supported by LangChain's init_chat_model works once its package
    (langchain-openai, langchain-anthropic, langchain-google-genai, ...) is installed
    and its API key is set in .env.
    """
    from langchain.chat_models import init_chat_model

    return init_chat_model(os.getenv("LLM_MODEL", "openai:gpt-4o-mini"), temperature=0.3)


def firm_name() -> str:
    return os.getenv("FIRM_NAME", "our real estate team")


def assistant_name() -> str:
    return os.getenv("ASSISTANT_NAME", "Riya")
