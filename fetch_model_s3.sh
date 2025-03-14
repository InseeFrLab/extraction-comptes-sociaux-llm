#!/bin/bash

MODEL_NAME="mistralai/Ministral-8B-Instruct-2410"
LOCAL_PATH="/home/onyxia/.cache/huggingface/hub"

MC_PATH=s3/projet-models-hf/diffusion/hf_hub/$MODEL_NAME

echo "🔹 Checking model availability on SSPCloud..."
if mc stat "$MC_PATH" >/dev/null 2>&1; then
    echo "✅ Model is available"
else
    echo "❌ $MODEL_NAME is not yet available on SSPCloud, it will be fetched it directly from HuggingFace 🤗."
    exit 1
fi

mc cp -r $MC_PATH/ $LOCAL_PATH/$MODEL_NAME
