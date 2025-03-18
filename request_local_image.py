import base64
from openai import OpenAI
from vllm.utils import FlexibleArgumentParser

# Modify OpenAI's API key and API base to use vLLM's API server.
openai_api_key = "EMPTY"
openai_api_base = "http://localhost:8000/v1"
IMAGE = "lvmh0-1.png"
PROMPT = \
    """I am providing an image that contains a complex table. Please transcribe this table into CSV format by following these detailed steps:

    Identify All Columns:
    Carefully analyze the image to determine every column present. Pay special attention to headers that span multiple rows or merged cells. Make sure to list each distinct column, even if some appear to be combined or nested.

    Generate the Header Row:
    Based on the identified columns, create a single CSV header row. Write out the column names in the order they appear, ensuring no column is omitted.

    Process Each Data Row:
    For every subsequent row in the table, map each cell's content to the corresponding column from your header row. If a cell is empty or a column is missing data, include an empty value (or leave it blank) to maintain the correct number of columns.

    Handle Complex Structures:
    If the table includes merged cells or multiple header rows, resolve these by consolidating them into your single header row. For cells that span multiple columns, duplicate or adjust the content as needed so that each column in your header is filled appropriately.

    Output Requirements:
    Output only the CSV data, starting with the header row, followed by one row for each table row. Use semicolon to separate fields and ensure any text containing commas or special characters is properly quoted.

Please provide the final CSV output without any extra commentary or explanation"""
client = OpenAI(
    api_key=openai_api_key,
    base_url=openai_api_base,
)

models = client.models.list()
model = models.data[0].id


def encode_base64_content_from_file(file_path: str) -> str:
    """Encode the content of a local file to base64 format."""
    with open(file_path, "rb") as file:
        encoded = base64.b64encode(file.read()).decode("utf-8")
    return encoded


# Single-image input inference using a locally stored image
def run_single_image(image_path=IMAGE) -> None:
    """
    Run inference on a local image file.
    
    The image is read from disk, encoded in base64,
    and then passed to the API as a data URL.
    """
    # Encode the local image file to base64.
    image_base64 = encode_base64_content_from_file(image_path)
    # Construct a data URL (assuming the image is in JPEG format).
    data_url = f"data:image/jpeg;base64,{image_base64}"

    # Use the data URL in the payload.
    chat_completion = client.chat.completions.create(
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": PROMPT
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": data_url
                    },
                },
            ],
        }],
        model=model,
        #max_completion_tokens=64,
    )

    result = chat_completion.choices[0].message.content
    print("Chat completion output from local image:", result)


example_function_map = {
    "single-image": run_single_image,
}


def main(args) -> None:
    # Use the provided image path from command-line arguments.
    run_single_image(image_path=args.image_path)


if __name__ == "__main__":
    parser = FlexibleArgumentParser(
        description='Demo on using OpenAI client for online serving with '
                    'multimodal language models served with vLLM using local images.'
    )
    parser.add_argument(
        '--chat-type',
        '-c',
        type=str,
        default="single-image",
        choices=list(example_function_map.keys()),
        help='Conversation type with multimodal data.'
    )
    parser.add_argument(
        '--image-path',
        type=str,
        default=IMAGE,
        help=IMAGE
    )
    args = parser.parse_args()
    main(args)
