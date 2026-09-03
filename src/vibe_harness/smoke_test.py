from openai import OpenAI

from vibe_harness.config import load_settings


def main() -> None:
    settings = load_settings()
    client = OpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
    )
    response = client.chat.completions.create(
        model=settings.model,
        messages=[{"role": "user", "content": "只回复 OK"}],
    )
    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
