"""Pattern selection for Hugging Face model downloads.

Loaded by path rather than imported as `open_webui.retrieval.model_download`,
so the suite keeps running without `huggingface_hub`, torch or the rest of the
application import chain -- the module under test is pure decision logic and
must stay that way.
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

module_path = Path(__file__).resolve().parents[1] / 'open_webui' / 'retrieval' / 'model_download.py'
spec = importlib.util.spec_from_file_location('model_download', module_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f'Unable to load {module_path}')

model_download = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model_download)
matches_any = model_download.matches_any
resolve_repo_files = model_download.resolve_repo_files
select_ignore_patterns = model_download.select_ignore_patterns


# The published file listing of a cross-encoder that ships three copies of one
# set of weights.  Used to assert exactly one copy survives.
RERANKER_REPO_FILES = [
    '.gitattributes',
    'README.md',
    'config.json',
    'model.safetensors',
    'onnx/model.onnx',
    'pytorch_model.bin',
    'sentencepiece.bpe.model',
    'special_tokens_map.json',
    'tokenizer.json',
    'tokenizer_config.json',
]

# A bi-encoder layout with every optional serving format the Hub carries.
EMBEDDING_REPO_FILES = [
    '.gitattributes',
    '1_Pooling/config.json',
    'README.md',
    'config.json',
    'config_sentence_transformers.json',
    'model.safetensors',
    'modules.json',
    'onnx/model.onnx',
    'onnx/model_qint8_avx512.onnx',
    'openvino/openvino_model.bin',
    'openvino/openvino_model.xml',
    'pytorch_model.bin',
    'rust_model.ot',
    'sentence_bert_config.json',
    'special_tokens_map.json',
    'tf_model.h5',
    'tokenizer.json',
    'tokenizer_config.json',
    'vocab.txt',
]


def kept(repo_files, patterns):
    return [path for path in repo_files if not matches_any(path, patterns)]


class SafetensorsPreferenceTests(unittest.TestCase):
    def test_pickle_weights_are_skipped_when_a_safetensors_twin_exists(self):
        patterns = select_ignore_patterns(RERANKER_REPO_FILES)

        survivors = kept(RERANKER_REPO_FILES, patterns)
        self.assertIn('model.safetensors', survivors)
        self.assertNotIn('pytorch_model.bin', survivors)

    def test_pickle_only_repository_still_downloads_its_weights(self):
        repo_files = [
            'config.json',
            'onnx/model.onnx',
            'onnx/model.onnx_data',
            'pytorch_model.bin',
            'sentencepiece.bpe.model',
            'tokenizer.json',
        ]

        survivors = kept(repo_files, select_ignore_patterns(repo_files))

        self.assertIn('pytorch_model.bin', survivors)
        self.assertNotIn('onnx/model.onnx', survivors)
        self.assertNotIn('onnx/model.onnx_data', survivors)

    def test_safetensors_only_repository_needs_no_pickle_pattern(self):
        repo_files = [
            'config.json',
            'model.safetensors',
            'tokenizer.json',
        ]

        patterns = select_ignore_patterns(repo_files)

        self.assertEqual(kept(repo_files, patterns), repo_files)
        self.assertFalse([pattern for pattern in patterns if pattern.endswith('.bin')])

    def test_preference_is_decided_per_directory(self):
        # A module subdirectory can ship only the pickle build even when the
        # repository root ships both, so a repository-wide verdict would drop
        # weights that are the only copy there.
        repo_files = [
            'config.json',
            'model.safetensors',
            'pytorch_model.bin',
            '2_Dense/config.json',
            '2_Dense/pytorch_model.bin',
        ]

        survivors = kept(repo_files, select_ignore_patterns(repo_files))

        self.assertNotIn('pytorch_model.bin', survivors)
        self.assertIn('2_Dense/pytorch_model.bin', survivors)

    def test_sharded_pickle_weights_and_their_index_are_skipped_together(self):
        repo_files = [
            'config.json',
            'model-00001-of-00002.safetensors',
            'model-00002-of-00002.safetensors',
            'model.safetensors.index.json',
            'pytorch_model-00001-of-00002.bin',
            'pytorch_model-00002-of-00002.bin',
            'pytorch_model.bin.index.json',
        ]

        survivors = kept(repo_files, select_ignore_patterns(repo_files))

        self.assertEqual(
            survivors,
            [
                'config.json',
                'model-00001-of-00002.safetensors',
                'model-00002-of-00002.safetensors',
                'model.safetensors.index.json',
            ],
        )

    def test_other_runtimes_dot_bin_files_are_not_mistaken_for_torch_weights(self):
        # `openvino_model.bin` is a weight file for a different runtime, not a
        # duplicate of the torch one: a blanket `*.bin` rule would conflate them.
        patterns = select_ignore_patterns(EMBEDDING_REPO_FILES, backends=('openvino',))

        survivors = kept(EMBEDDING_REPO_FILES, patterns)
        self.assertIn('openvino/openvino_model.bin', survivors)


class FrameworkArtifactTests(unittest.TestCase):
    def test_alien_serving_formats_are_always_skipped(self):
        survivors = kept(EMBEDDING_REPO_FILES, select_ignore_patterns(EMBEDDING_REPO_FILES))

        for skipped in (
            'onnx/model.onnx',
            'onnx/model_qint8_avx512.onnx',
            'openvino/openvino_model.bin',
            'openvino/openvino_model.xml',
            'rust_model.ot',
            'tf_model.h5',
            'pytorch_model.bin',
        ):
            self.assertNotIn(skipped, survivors)

        for required in (
            '1_Pooling/config.json',
            'config.json',
            'config_sentence_transformers.json',
            'model.safetensors',
            'modules.json',
            'sentence_bert_config.json',
            'tokenizer.json',
            'tokenizer_config.json',
            'vocab.txt',
        ):
            self.assertIn(required, survivors)

    def test_one_weight_copy_survives_for_a_three_copy_repository(self):
        survivors = kept(RERANKER_REPO_FILES, select_ignore_patterns(RERANKER_REPO_FILES))

        self.assertEqual(
            survivors,
            [
                '.gitattributes',
                'README.md',
                'config.json',
                'model.safetensors',
                'sentencepiece.bpe.model',
                'special_tokens_map.json',
                'tokenizer.json',
                'tokenizer_config.json',
            ],
        )

    def test_onnx_is_kept_when_the_onnx_backend_is_configured(self):
        patterns = select_ignore_patterns(EMBEDDING_REPO_FILES, backends=('onnx', 'torch'))

        survivors = kept(EMBEDDING_REPO_FILES, patterns)
        self.assertIn('onnx/model.onnx', survivors)
        self.assertNotIn('tf_model.h5', survivors)

    def test_blank_backend_is_treated_as_torch(self):
        self.assertEqual(
            select_ignore_patterns(RERANKER_REPO_FILES, backends=('', '   ')),
            select_ignore_patterns(RERANKER_REPO_FILES, backends=('torch',)),
        )

    def test_unrecognised_layout_downloads_everything(self):
        # Nothing here is a weight file this application knows how to load, so
        # filtering it would be a guess: fetch the repository whole instead.
        repo_files = ['config.json', 'tf_model.h5', 'tokenizer.json']

        self.assertEqual(select_ignore_patterns(repo_files), [])


class MissingListingTests(unittest.TestCase):
    def test_without_a_listing_both_weight_formats_are_fetched(self):
        patterns = select_ignore_patterns(None)

        survivors = kept(RERANKER_REPO_FILES, patterns)
        self.assertIn('pytorch_model.bin', survivors)
        self.assertIn('model.safetensors', survivors)
        self.assertNotIn('onnx/model.onnx', survivors)

    def test_local_files_only_applies_the_pickle_patterns_blind(self):
        # No download happens on a `local_files_only` call, so the patterns can
        # only narrow the completeness check over the cached snapshot -- and
        # must, or a cache populated online with the safetensors preference
        # would read as incomplete offline.
        patterns = select_ignore_patterns(None, local_files_only=True)

        for skipped in ('pytorch_model.bin', 'pytorch_model.bin.index.json', '2_Dense/pytorch_model.bin'):
            self.assertTrue(matches_any(skipped, patterns), skipped)
        self.assertFalse(matches_any('model.safetensors', patterns))


class RepoListingTests(unittest.TestCase):
    def _install_fake_hub(self, hf_api_factory):
        previous = sys.modules.get('huggingface_hub')

        def restore():
            if previous is None:
                sys.modules.pop('huggingface_hub', None)
            else:
                sys.modules['huggingface_hub'] = previous

        self.addCleanup(restore)
        fake = types.ModuleType('huggingface_hub')
        fake.HfApi = hf_api_factory
        sys.modules['huggingface_hub'] = fake

    def test_offline_never_attempts_a_listing(self):
        # Asserted through a flag rather than a raise: `resolve_repo_files`
        # swallows exceptions, so a raising stub would pass either way.
        constructed = []
        self._install_fake_hub(lambda: constructed.append(True))

        self.assertIsNone(resolve_repo_files('org/model', revision='main', local_files_only=True))
        self.assertEqual(constructed, [])

    def test_a_failed_listing_degrades_to_none(self):
        class FailingApi:
            def list_repo_files(self, **kwargs):
                raise RuntimeError('hub unreachable')

        self._install_fake_hub(FailingApi)

        self.assertIsNone(resolve_repo_files('org/model', revision='abc123'))

    def test_a_successful_listing_is_returned(self):
        seen = {}

        class Api:
            def list_repo_files(self, **kwargs):
                seen.update(kwargs)
                return iter(RERANKER_REPO_FILES)

        self._install_fake_hub(Api)

        self.assertEqual(resolve_repo_files('org/model', revision='abc123'), RERANKER_REPO_FILES)
        self.assertEqual(seen, {'repo_id': 'org/model', 'revision': 'abc123'})


if __name__ == '__main__':
    unittest.main()
