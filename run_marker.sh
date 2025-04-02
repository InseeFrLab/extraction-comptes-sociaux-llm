marker_single <file> \
    --force_ocr  \
    --use_llm   \
        --llm_service marker.services.openai.OpenAIService   \
        --openai_base_url <url> \
        --openai_model <model>  \
        --openai_api_key <api key> \
        
    --timeout 99999
    --output_dir .