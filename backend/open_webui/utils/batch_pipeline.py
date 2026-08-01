import json
import os
import time
from typing import List, Dict, Any, Optional
from open_webui.utils.concept_wiki import global_concept_wiki

class BatchPipeline:
    """
    Manages OpenAI & Anthropic Batch API workflow for offline 50% cost-reduced
    high-volume concept extraction.
    """

    @staticmethod
    def build_extraction_prompt(passage_content: str, book_title: str, toc_path: List[str]) -> str:
        toc_str = " > ".join(toc_path)
        return (
            f"You are an expert domain knowledge extractor. Analyze the following book paragraph from '{book_title}' (Section: '{toc_str}').\n"
            f"Paragraph Text:\n\"\"\"{passage_content}\"\"\"\n\n"
            f"Extract all technical terms, proper nouns, domain concepts, and acronyms mentioned in this paragraph.\n"
            f"Respond ONLY with a valid JSON object matching this schema:\n"
            f"{{\n"
            f'  "concepts": [\n'
            f'    {{\n'
            f'      "canonical_name": "Standard Concept Name",\n'
            f'      "aliases": ["alias1", "abbreviation"],\n'
            f'      "definition": "Brief 1-sentence definition based on paragraph context"\n'
            f'    }}\n'
            f'  ]\n'
            f"}}\n"
        )

    @staticmethod
    def create_openai_batch_jsonl(passages: List[Dict[str, Any]], output_filepath: str, model: str = "gpt-4o-mini") -> str:
        """
        Creates a .jsonl file matching OpenAI Batch API format.
        Each line is a JSON request object.
        """
        os.makedirs(os.path.dirname(output_filepath), exist_ok=True)
        with open(output_filepath, "w", encoding="utf-8") as f:
            for passage in passages:
                passage_id = passage["passage_id"]
                content = passage["content"]
                book_title = passage.get("book_title", "")
                toc_path = passage.get("toc_path", [])

                prompt = BatchPipeline.build_extraction_prompt(content, book_title, toc_path)

                request_item = {
                    "custom_id": passage_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "You are a precise JSON concept extraction assistant."},
                            {"role": "user", "content": prompt}
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.1
                    }
                }
                f.write(json.dumps(request_item, ensure_ascii=False) + "\n")

        return output_filepath

    @staticmethod
    def ingest_openai_batch_results(result_jsonl_filepath: str, passages_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """
        Parses completed OpenAI Batch result JSONL and populates global_concept_wiki.
        """
        if not os.path.exists(result_jsonl_filepath):
            raise FileNotFoundError(f"Batch result file not found: {result_jsonl_filepath}")

        success_count = 0
        extracted_concepts_count = 0

        with open(result_jsonl_filepath, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    custom_id = data.get("custom_id")  # passage_id
                    response_body = data.get("response", {}).get("body", {})
                    choices = response_body.get("choices", [])

                    if choices and custom_id in passages_map:
                        passage_info = passages_map[custom_id]
                        content_str = choices[0].get("message", {}).get("content", "")

                        parsed = json.loads(content_str)
                        concepts_list = parsed.get("concepts", [])

                        for c in concepts_list:
                            c_name = c.get("canonical_name")
                            if c_name:
                                global_concept_wiki.register_concept(
                                    canonical_name=c_name,
                                    aliases=c.get("aliases", []),
                                    definition=c.get("definition", ""),
                                    passage_id=custom_id,
                                    book_title=passage_info.get("book_title", "")
                                )
                                extracted_concepts_count += 1
                        success_count += 1
                except Exception:
                    continue

        return {
            "processed_passages": success_count,
            "total_concepts_registered": len(global_concept_wiki.concepts),
            "total_extractions": extracted_concepts_count
        }
