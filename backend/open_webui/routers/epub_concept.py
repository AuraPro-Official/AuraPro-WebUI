import hashlib
import os
import shutil
import tempfile
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, UploadFile, File, HTTPException
from pydantic import BaseModel

from open_webui.retrieval.loaders.epub_parser import EPUBParser
from open_webui.utils.concept_wiki import global_concept_wiki
from open_webui.utils.batch_pipeline import BatchPipeline

router = APIRouter(prefix='/api/v1/epub_concept', tags=['epub_concept'])


class SearchQuery(BaseModel):
    query: str
    top_k: Optional[int] = 5


class SearchResponse(BaseModel):
    query: str
    matched_concepts: List[Dict[str, Any]]
    results: List[Dict[str, Any]]


@router.on_event('startup')
async def _startup():
    """Hydrate in-memory concept index from SQLite on server start."""
    global_concept_wiki.load_from_db()


@router.post('/parse_epub')
async def parse_epub_file(file: UploadFile = File(...)):
    """Uploads an EPUB file, parses structure, TOC breadcrumbs, and natural paragraphs.
    All passages are persisted to the standalone epub_concept.db."""
    if not file.filename.endswith('.epub'):
        raise HTTPException(status_code=400, detail='File must be an .epub file.')

    temp_dir = tempfile.mkdtemp()
    temp_path = os.path.join(temp_dir, file.filename)

    try:
        with open(temp_path, 'wb') as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Compute file hash for dedup
        with open(temp_path, 'rb') as fh:
            file_hash = hashlib.sha256(fh.read()).hexdigest()[:16]

        parser = EPUBParser(temp_path)
        parsed_data = parser.parse()

        db = global_concept_wiki.get_db()
        book_title = parsed_data['book_title']
        book_id = book_title  # use title as book_id for simplicity

        # Persist book record
        db.save_book(
            book_id=book_id,
            book_title=book_title,
            total_passages=parsed_data['total_passages'],
            file_hash=file_hash,
        )

        # Persist all passages to SQLite
        for p in parsed_data['passages']:
            p['book_id'] = book_id
        db.save_passages(parsed_data['passages'])

        return {
            'status': 'success',
            'book_title': book_title,
            'total_passages': parsed_data['total_passages'],
            'db_path': db.get_stats()['db_path'],
            'sample_passages': parsed_data['passages'][:3],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Failed to parse EPUB: {str(e)}')
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.post('/seed_vocabulary')
async def upload_seed_vocabulary(items: List[Dict[str, Any]]):
    """Uploads seed domain vocabulary terms into global Concept Wiki (persisted to SQLite)."""
    count = global_concept_wiki.load_seed_vocabulary(items)
    return {
        'status': 'success',
        'loaded_concepts': count,
        'total_active_concepts': len(global_concept_wiki.concepts),
    }


@router.post('/batch/create')
async def create_batch_file(model: str = 'gpt-4o-mini'):
    """Generates OpenAI Batch API .jsonl file from all persisted passages."""
    db = global_concept_wiki.get_db()
    passages_list = db.get_all_passages()
    if not passages_list:
        raise HTTPException(status_code=400, detail='No passages in database. Upload an EPUB first.')

    output_path = os.path.join(tempfile.gettempdir(), 'epub_concept_batch.jsonl')
    BatchPipeline.create_openai_batch_jsonl(passages_list, output_path, model=model)

    return {
        'status': 'success',
        'batch_file_path': output_path,
        'total_requests': len(passages_list),
    }


@router.post('/search', response_model=SearchResponse)
async def search_epub_concepts(payload: SearchQuery):
    """
    Online Query Pipeline:
    1. Tier 1 Fast Exact Concept Lookup via in-memory alias index
    2. Candidate Recall: passage IDs from concept occurrences (SQLite)
    3. Fallback: keyword search directly against SQLite passages table
    4. Verbatim Passage Return with Book Title & TOC Path
    """
    user_query = payload.query.strip()
    if not user_query:
        raise HTTPException(status_code=400, detail='Query string cannot be empty.')

    db = global_concept_wiki.get_db()

    # Tier 1: In-memory Concept Match
    matched_concepts = global_concept_wiki.find_concepts_in_text(user_query)

    recalled_passage_ids = set()
    for c in matched_concepts:
        pids = db.get_passage_ids_for_concept(c['concept_id'])
        recalled_passage_ids.update(pids)

    # Fallback to SQLite keyword search if no concept matched
    if not recalled_passage_ids:
        query_words = [w for w in user_query.split() if len(w) > 1]
        for word in query_words:
            keyword_results = db.search_passages_by_keyword(word, limit=payload.top_k)
            for p in keyword_results:
                recalled_passage_ids.add(p['passage_id'])

    # Retrieve 100% faithful original passages from SQLite
    passage_id_list = list(recalled_passage_ids)[: payload.top_k]
    passages = db.get_passages_by_ids(passage_id_list)

    results = []
    for p in passages:
        results.append(
            {
                'passage_id': p['passage_id'],
                'book_title': p['book_title'],
                'toc_path': p['toc_path'],
                'content': p['content'],  # 100% original text from SQLite
                'parent_context': p.get('parent_context', ''),
            }
        )

    return SearchResponse(
        query=user_query,
        matched_concepts=matched_concepts,
        results=results,
    )


@router.get('/stats')
async def get_database_stats():
    """Returns current database statistics."""
    db = global_concept_wiki.get_db()
    return db.get_stats()


@router.get('/books')
async def list_books():
    """Returns all ingested books."""
    db = global_concept_wiki.get_db()
    return db.get_books()


@router.get('/concepts')
async def list_concepts():
    """Returns all concepts in the wiki for review/editing (Human-in-the-loop)."""
    return global_concept_wiki.get_db().get_all_concepts()
