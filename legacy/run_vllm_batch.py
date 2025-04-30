from vllm import LLM
import PIL

llm = LLM(model="llava-hf/llava-1.5-7b-hf")

# Refer to the HuggingFace repo for the correct format to use
prompt = "USER: <image>\nGive as much as possible informations on given tables\nASSISTANT:"

# Load the image using PIL.Image
image = PIL.Image.open("image.png")

# Single prompt inference
outputs = llm.generate({
    "prompt": prompt,
    "multi_modal_data": {"image": image},
})

for o in outputs:
    generated_text = o.outputs[0].text
    print("#"*20 + generated_text + "#"*20)