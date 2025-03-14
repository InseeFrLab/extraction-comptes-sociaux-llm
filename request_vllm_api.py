import base64

import requests
from openai import OpenAI

from vllm.utils import FlexibleArgumentParser

# Modify OpenAI's API key and API base to use vLLM's API server.
openai_api_key = "EMPTY"
openai_api_base = "http://localhost:8000/v1"

client = OpenAI(
    # defaults to os.environ.get("OPENAI_API_KEY")
    api_key=openai_api_key,
    base_url=openai_api_base,
)

models = client.models.list()
model = models.data[0].id


def encode_base64_content_from_url(content_url: str) -> str:
    """Encode a content retrieved from a remote url to base64 format."""

    with requests.get(content_url) as response:
        response.raise_for_status()
        result = base64.b64encode(response.content).decode('utf-8')

    return result

# Single-image input inference
def run_single_image(image_url) -> None:

    ## Use image url in the payload
    chat_completion_from_url = client.chat.completions.create(
        messages=[{
            "role":
            "user",
            "content": [
                {
                    "type": "text",
                    "text": "What's in this image?"
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_url
                    },
                },
            ],
        }],
        model=model,
        max_completion_tokens=64,
    )

    result = chat_completion_from_url.choices[0].message.content
    print("Chat completion output from image url:", result)

    ## Use base64 encoded image in the payload
    image_base64 = encode_base64_content_from_url(image_url)
    chat_completion_from_base64 = client.chat.completions.create(
        messages=[{
            "role":
            "user",
            "content": [
                {
                    "type": "text",
                    "text": "What's in this image?"
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_base64}"
                    },
                },
            ],
        }],
        model=model,
        max_completion_tokens=64,
    )

    result = chat_completion_from_base64.choices[0].message.content
    print("Chat completion output from base64 encoded image:", result)


example_function_map = {
    "single-image": run_single_image,
}


def main(args) -> None:
    chat_type = args.chat_type
    example_function_map[chat_type]()


if __name__ == "__main__":
    parser = FlexibleArgumentParser(
        description='Demo on using OpenAI client for online serving with '
        'multimodal language models served with vLLM.')
    parser.add_argument('--chat-type',
                        '-c',
                        type=str,
                        default="single-image",
                        choices=list(example_function_map.keys()),
                        help='Conversation type with multimodal data.')
    args = parser.parse_args()
    main(args)