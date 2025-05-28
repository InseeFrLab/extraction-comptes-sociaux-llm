marker_single output_page.pdf \
    --force_ocr  \
    --use_llm   \
        --llm_service marker.services.openai.OpenAIService   \
        --openai_base_url http://0.0.0.0:1324/v1/ \
        --openai_model gemma3:27b  \
        --openai_api_key "" \
    --timeout 99999 \
    --output_dir . \
    --output_format json