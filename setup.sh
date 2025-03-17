pip install https://vllm-wheels.s3.us-west-2.amazonaws.com/nightly/vllm-1.0.0.dev-cp38-abi3-manylinux1_x86_64.whl

pip install -U "huggingface_hub[cli]"

huggingface-cli login

pip install git+https://github.com/huggingface/transformers.git

export HF_HOME="/home/onyxia/.cache/huggingface/hub"

sh fetch_model_s3.sh

vllm serve google/gemma-3-27b-it --max_model_len 60000