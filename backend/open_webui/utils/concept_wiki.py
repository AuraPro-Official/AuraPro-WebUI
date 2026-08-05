import json
import logging
import os
from typing import List, Dict, Any, Optional, Set

from open_webui.utils.epub_concept_db import EpubConceptDB

log = logging.getLogger(__name__)


class ConceptWiki:
    """
    Manages global Concept Wiki Index:
    - Dictionary Seed Vocabulary matching (Trie / exact substring)
    - Concept alias normalization and merging
    - Passage occurrences mapping
    - Persistent storage via standalone SQLite (epub_concept.db)
    """

    def __init__(self):
        self.concepts: Dict[str, Dict[str, Any]] = {}  # concept_id -> Concept Dict
        self.alias_to_concept_id: Dict[str, str] = {}  # normalized alias -> concept_id
        self._concept_counter = 1
        self._db = EpubConceptDB()

    def load_from_db(self):
        """Hydrate in-memory index from SQLite on startup."""
        # Load alias index
        self.alias_to_concept_id = self._db.get_all_aliases()

        # Load all concepts
        db_concepts = self._db.get_all_concepts()
        for c in db_concepts:
            cid = c['concept_id']
            occurrences = self._db.get_occurrences_for_concept(cid)
            self.concepts[cid] = {
                'concept_id': cid,
                'canonical_name': c['canonical_name'],
                'aliases': c['aliases'],
                'definition': c.get('definition', ''),
                'occurrences': occurrences,
            }
            # Parse counter from concept_id to keep numbering consistent
            try:
                num = int(cid.split('_')[1])
                if num >= self._concept_counter:
                    self._concept_counter = num + 1
            except (IndexError, ValueError):
                pass

        log.info(f'ConceptWiki loaded from DB: {len(self.concepts)} concepts, {len(self.alias_to_concept_id)} aliases')

    def load_seed_vocabulary(self, seed_data: List[Dict[str, Any]]) -> int:
        """
        Loads seed vocabulary items (e.g. from JSON file).
        Expected item format:
            {"term": "ResNet", "aliases": ["残差网络", "Residual Network"], "definition": "..."}
        """
        loaded_count = 0
        for item in seed_data:
            term = item.get('term') or item.get('canonical_name')
            if not term:
                continue

            aliases = item.get('aliases', [])
            definition = item.get('definition', '')

            self.register_concept(canonical_name=term, aliases=aliases, definition=definition)
            loaded_count += 1

        return loaded_count

    def load_seed_json_file(self, file_path: str) -> int:
        """Helper to load seed dictionary from JSON file."""
        if not os.path.exists(file_path):
            return 0
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return self.load_seed_vocabulary(data)
                elif isinstance(data, dict):
                    items = [{'term': k, 'aliases': v if isinstance(v, list) else [v]} for k, v in data.items()]
                    return self.load_seed_vocabulary(items)
        except Exception:
            pass
        return 0

    def register_concept(
        self,
        canonical_name: str,
        aliases: Optional[List[str]] = None,
        definition: str = '',
        passage_id: Optional[str] = None,
        book_title: Optional[str] = None,
    ) -> str:
        """
        Registers or updates a concept. If canonical_name or any alias matches
        an existing concept, merges into that existing concept.
        Persists to SQLite automatically.
        """
        norm_name = canonical_name.strip().lower()
        aliases_set = set([a.strip() for a in (aliases or []) if a.strip()])
        aliases_set.add(canonical_name.strip())

        existing_cid = None
        for alias in aliases_set:
            norm_alias = alias.lower()
            if norm_alias in self.alias_to_concept_id:
                existing_cid = self.alias_to_concept_id[norm_alias]
                break

        if existing_cid:
            concept_id = existing_cid
            concept = self.concepts[concept_id]
            # Merge aliases
            existing_aliases = set(concept['aliases'])
            existing_aliases.update(aliases_set)
            concept['aliases'] = list(existing_aliases)
            if definition and not concept['definition']:
                concept['definition'] = definition
        else:
            concept_id = f'CONCEPT_{self._concept_counter:06d}'
            self._concept_counter += 1
            concept = {
                'concept_id': concept_id,
                'canonical_name': canonical_name.strip(),
                'aliases': list(aliases_set),
                'definition': definition,
                'occurrences': [],
            }
            self.concepts[concept_id] = concept

        # Map all aliases to concept_id (in memory)
        for alias in aliases_set:
            self.alias_to_concept_id[alias.lower()] = concept_id

        # Attach passage occurrence if provided
        if passage_id:
            occ_list = self.concepts[concept_id]['occurrences']
            if not any(o['passage_id'] == passage_id for o in occ_list):
                occ_list.append({'passage_id': passage_id, 'book_title': book_title or ''})
                # Persist occurrence to SQLite
                self._db.save_occurrence(concept_id, passage_id, book_title or '')

        # Persist concept to SQLite
        self._db.save_concept(
            concept_id=concept_id,
            canonical_name=concept['canonical_name'],
            aliases=concept['aliases'],
            definition=concept['definition'],
        )

        return concept_id

    def find_concepts_in_text(self, text: str) -> List[Dict[str, Any]]:
        """
        Fast Tier 1 deterministic exact string matching against Wiki_Concept_Index.
        Returns matching Concept objects.
        """
        matched_concepts = []
        text_lower = text.lower()

        # Sort aliases by length descending to match longest phrases first
        sorted_aliases = sorted(self.alias_to_concept_id.keys(), key=len, reverse=True)
        seen_concept_ids: Set[str] = set()

        for alias in sorted_aliases:
            if len(alias) < 2:  # Skip single character aliases to avoid false positives
                continue

            if alias in text_lower:
                cid = self.alias_to_concept_id[alias]
                if cid not in seen_concept_ids:
                    seen_concept_ids.add(cid)
                    matched_concepts.append(self.concepts[cid])

        return matched_concepts

    def get_concept_by_id(self, concept_id: str) -> Optional[Dict[str, Any]]:
        return self.concepts.get(concept_id)

    def export_to_dict(self) -> Dict[str, Any]:
        return {
            'concepts': self.concepts,
            'alias_to_concept_id': self.alias_to_concept_id,
            'concept_counter': self._concept_counter,
        }

    def import_from_dict(self, data: Dict[str, Any]):
        self.concepts = data.get('concepts', {})
        self.alias_to_concept_id = data.get('alias_to_concept_id', {})
        self._concept_counter = data.get('concept_counter', 1)

    def get_db(self) -> EpubConceptDB:
        """Expose the underlying DB instance for direct passage/stats queries."""
        return self._db


# Global singleton instance
global_concept_wiki = ConceptWiki()
