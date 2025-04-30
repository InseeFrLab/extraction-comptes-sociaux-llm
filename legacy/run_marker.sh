marker_single lvmh.png \
    --force_ocr  \
    --use_llm   \
        --llm_service marker.services.openai.OpenAIService   \
        --openai_base_url https://projet-extraction-tableaux-vllm.user.lab.sspcloud.fr/v1 \
        --openai_model google/gemma-3-27b-it  \
        --openai_api_key "" \
    --timeout 99999 \
    --output_dir . \
    --output_format json