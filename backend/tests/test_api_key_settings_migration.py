import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.util import load_python_file


class ApiKeySettingsMigrationTest(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        self.connection = self.engine.connect()
        self.addCleanup(self.connection.close)
        metadata = sa.MetaData()
        self.config = sa.Table(
            'config',
            metadata,
            sa.Column('key', sa.Text(), primary_key=True),
            sa.Column('value', sa.JSON(), nullable=False),
            sa.Column('updated_at', sa.BigInteger()),
        )
        metadata.create_all(self.connection)
        migrations = Path(__file__).resolve().parents[1] / 'open_webui/migrations/versions'
        self.migration = load_python_file(str(migrations), '8a64de10b730_enable_api_keys_with_restrictions.py')

    def apply_upgrade(self):
        with Operations.context(MigrationContext.configure(self.connection)):
            self.migration.upgrade()

    def test_enables_both_existing_switches_without_overwriting_other_configuration(self):
        allowed = '/api/v1/models,/api/v1/chat/completions,/api/v1/audio/transcriptions'
        permissions = {'features': {'api_keys': False, 'notes': True}}
        values_before = {
            'auth.enable_api_keys': False,
            'auth.api_key.endpoint_restrictions': False,
            'auth.api_key.allowed_endpoints': allowed,
            'user.permissions': permissions,
        }
        self.connection.execute(
            self.config.insert(), [{'key': key, 'value': value} for key, value in values_before.items()]
        )

        self.apply_upgrade()

        values = dict(self.connection.execute(sa.select(self.config.c.key, self.config.c.value)).tuples().all())
        self.assertEqual(
            values,
            {**values_before, 'auth.enable_api_keys': True, 'auth.api_key.endpoint_restrictions': True},
        )

    def test_fresh_database_leaves_missing_rows_for_webui_defaults(self):
        self.apply_upgrade()
        self.assertEqual(self.connection.execute(sa.select(self.config)).all(), [])
