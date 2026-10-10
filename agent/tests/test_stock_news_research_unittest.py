import asyncio
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import app_database as db
import stock_news_research_service as news


class StockNewsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(db, "DB_PATH", Path(temp.name) / 'test.sqlite3').start()
        patch.object(db, "_INITIALIZED", False).start()
        patch.object(news, "_ACTIVE", set()).start()
        patch.object(news, "_SLOTS", threading.BoundedSemaphore(2)).start()
        patch.object(news, "_now", return_value=datetime(2026, 10, 9, 12, tzinfo=timezone.utc)).start()
        db.ensure_database()
        from premarket_news_service import ensure_premarket_news_tables
        ensure_premarket_news_tables()

    def insert(self, ident='n1', symbol='CSCO', stamp='2026-10-08T10:00:00+00:00', title='Company updates outlook', description='The company reported revenue growth.', related='[]', raw='{}', url='https://example.com/news'):
        with db.connection() as conn:
            conn.execute("""INSERT INTO premarket_news_items
                (news_id,symbol,published_utc,title_original,description_original,related_symbols_json,raw_json,article_url,fetched_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (ident, symbol, stamp, title, description, related, raw, url, stamp, stamp))
            conn.commit()

    def output(self, data):
        return {'summary_cn': '公司公告增长，仍需核验后续兑现。', 'sentiment': 'positive',
            'summary_news_ids': [data['items'][0]['id']],
            'support': [{'text': '报道提到营收增长', 'news_ids': [data['items'][0]['id']]}], 'risks': [],
            'articles': [{'id': item['id'], 'title_cn': '公司更新指引', 'summary_cn': '来源摘要提到营收增长。', 'sentiment': 'positive', 'reason': '业务增长', 'relevance': 'direct'} for item in data['items']]}

    def test_read_is_cache_only_and_does_not_write_or_request(self):
        self.insert()
        import requests
        with patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('external request')), patch.object(news, 'cache_set') as store:
            data = news.read_news('CSCO')
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['analysis']['status'], 'unreviewed')
        store.assert_not_called()

    def test_latest_ten_not_importance_and_no_old_or_future_items(self):
        for i in range(1, 14):
            self.insert(str(i), stamp=f'2026-09-{i + 10:02d}T00:00:00+00:00', title=f'Headline {i}', url=f'https://example.com/{i}')
        self.insert('old', stamp='2026-08-01T00:00:00Z', title='Old', url='https://example.com/old')
        self.insert('future', stamp='2026-10-10T00:00:00Z', title='Future', url='https://example.com/future')
        data = news.read_news('CSCO')
        self.assertEqual([i['id'] for i in data['items']], [str(i) for i in range(13, 3, -1)])

    def test_related_subject_included_but_sector_alternative_not_inferred(self):
        self.insert(symbol='MSFT', related='["CSCO"]')
        self.insert('other', symbol='NVDA', title='Other', url='https://example.com/other')
        self.assertEqual(news.read_news('CSCO')['count'], 1)

    def test_duplicate_title_and_url_collapsed(self):
        self.insert()
        self.insert('n2', title='Company updates outlook!', url='https://example.com/news?utm=2')
        self.assertEqual(news.read_news('CSCO')['count'], 1)

    def test_unsafe_url_removed_and_title_only_preserved(self):
        self.insert(description='', url='javascript:alert(1)')
        item = news.read_news('CSCO')['items'][0]
        self.assertIsNone(item['url'])
        self.assertEqual(item['content_basis'], 'title_only')
        self.assertEqual(item['sentiment'], 'unreviewed')

    def test_date_requires_timezone(self):
        self.insert(stamp='2026-10-08T10:00:00')
        self.assertEqual(news.read_news('CSCO')['count'], 0)

    def test_unknown_gildata_time_keeps_received_label_and_filters_old_report(self):
        raw = json.dumps({'gildata_provenance': {'queue_time_basis': 'received_at', 'reported_time': '2026-10-08 09:00:00'}})
        self.insert(raw=raw)
        self.insert('oldreport', title='Old received today', raw=json.dumps({'gildata_provenance': {'queue_time_basis': 'received_at', 'reported_time': '2025-01-01 00:00:00'}}), url='https://example.com/old')
        data = news.read_news('CSCO')
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['items'][0]['time_basis'], 'received_at')

    def test_legacy_digest_reused_without_treating_reasoning_as_article(self):
        db.cache_set('news_digest:v3:CSCO:10', {'items': [{'title': 'Source headline', 'published_utc': '2026-10-08T00:00:00Z', 'reasoning': 'Not actual article body'}]})
        item = news.read_news('CSCO')['items'][0]
        self.assertEqual(item['content_basis'], 'title_only')
        self.assertEqual(item['source_summary'], '')

    def test_fingerprint_changes_with_content_or_model(self):
        self.insert()
        initial = news.read_news('CSCO')['fingerprint']
        with patch.dict(os.environ, {'LANGCHAIN_MODEL_NAME': 'other-model'}):
            self.assertNotEqual(initial, news.read_news('CSCO')['fingerprint'])
        with db.connection() as conn:
            conn.execute("UPDATE premarket_news_items SET description_original='Changed source abstract'")
            conn.commit()
        self.assertNotEqual(initial, news.read_news('CSCO')['fingerprint'])

    def test_future_legacy_cache_preserves_abstract_without_changing_sentiment(self):
        db.cache_set('news_digest:v3:CSCO:10', {'items': [{'title': 'Headline', 'description': 'Actual source abstract', 'published_utc': '2026-10-08T00:00:00Z'}]})
        item = news.read_news('CSCO')['items'][0]
        self.assertEqual(item['content_basis'], 'source_abstract')
        self.assertEqual(item['sentiment'], 'unreviewed')

    def test_summary_basis_is_derived_from_input_not_model_claim(self):
        self.insert()
        data = news.read_news('CSCO')
        output = self.output(data)
        output['summary_basis'] = 'title_only'
        checked = news._validate(output, data['items'])
        self.assertEqual(checked['summary_basis'], 'source_abstract')

    def test_empty_news_does_not_call_model(self):
        with patch.object(news, '_call_model') as model:
            self.assertEqual(news.start_summary('CSCO', threading.BoundedSemaphore(1))['status'], 'no_news')
            model.assert_not_called()

    def test_titles_only_are_summarized_as_titles(self):
        self.insert(description='')
        data = news.read_news('CSCO')
        slots = threading.BoundedSemaphore(1)
        with patch.object(news.threading, 'Thread') as thread, patch.object(news, '_call_model', return_value=(self.output(data), 'deepseek-flash')) as model:
            self.assertTrue(news.start_summary('CSCO', slots)['started'])
            thread.call_args.kwargs['target']()
        analysis = news.read_news('CSCO')['analysis']
        self.assertEqual(analysis['status'], 'completed')
        self.assertEqual(analysis['summary_basis'], 'title_only')
        self.assertEqual(model.call_count, 1)

    def test_worker_dedup_cache_and_no_source_changes(self):
        self.insert()
        data = news.read_news('CSCO')
        slots = threading.BoundedSemaphore(1)
        with patch.object(news.threading, 'Thread') as thread, patch.object(news, '_call_model', return_value=(self.output(data), 'deepseek-flash')) as model:
            self.assertTrue(news.start_summary('CSCO', slots)['started'])
            self.assertFalse(news.start_summary('CSCO', slots)['started'])
            thread.call_args.kwargs['target']()
            self.assertEqual(news.read_news('CSCO')['analysis']['status'], 'completed')
            self.assertFalse(news.start_summary('CSCO', slots)['started'])
            self.assertEqual(model.call_count, 1)
        self.assertTrue(slots.acquire(blocking=False))
        with db.connection() as conn:
            self.assertEqual(conn.execute('SELECT sentiment FROM premarket_news_items').fetchone()[0], 'neutral')

    def test_new_article_cannot_inherit_old_digest(self):
        self.insert()
        first = news.read_news('CSCO')
        db.cache_set('stock_news_research:v2:' + first['fingerprint'], {'status': 'completed', **self.output(first)})
        self.insert('new', title='Another headline', url='https://example.com/new', stamp='2026-10-09T10:00:00Z')
        self.assertEqual(news.read_news('CSCO')['analysis']['status'], 'unreviewed')

    def test_failed_worker_redacts_error_and_releases_slots(self):
        self.insert()
        slots = threading.BoundedSemaphore(1)
        with patch.object(news.threading, 'Thread') as thread, patch.object(news, '_call_model', side_effect=RuntimeError('private-key-secret')):
            news.start_summary('CSCO', slots)
            thread.call_args.kwargs['target']()
        data = news.read_news('CSCO')
        self.assertEqual(data['analysis']['status'], 'failed')
        self.assertNotIn('private-key-secret', json.dumps(data))
        self.assertEqual(data['count'], 1)
        self.assertTrue(slots.acquire(blocking=False))

    def test_busy_does_not_consume_own_slot(self):
        self.insert()
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        self.assertEqual(news.start_summary('CSCO', slots)['status'], 'busy')
        self.assertTrue(news._SLOTS.acquire(blocking=False))
        self.assertTrue(news._SLOTS.acquire(blocking=False))

    def test_invalid_citations_are_rejected(self):
        self.insert()
        data = news.read_news('CSCO')
        output = self.output(data)
        output['summary_news_ids'] = ['fabricated']
        with self.assertRaises(ValueError):
            news._validate(output, data['items'])

    def test_title_only_and_mixed_summary_basis(self):
        self.insert()
        self.insert('titleonly', title='Title only', description='', url='https://example.com/titleonly')
        data = news.read_news('CSCO')
        output = self.output(data)
        output['summary_news_ids'] = ['titleonly']
        self.assertEqual(news._validate(output, data['items'])['summary_basis'], 'title_only')
        output['summary_news_ids'] = ['titleonly', 'n1']
        self.assertEqual(news._validate(output, data['items'])['summary_basis'], 'mixed')

    def test_long_summary_is_not_displayed_as_one_sentence(self):
        self.insert()
        data = news.read_news('CSCO')
        output = self.output(data)
        output['summary_cn'] = '长' * 161
        with self.assertRaises(ValueError):
            news._validate(output, data['items'])

    def test_model_uses_existing_factory_and_bounded_input(self):
        self.insert()
        data = news.read_news('CSCO')
        model = Mock(model_name='deepseek-flash')
        model.invoke.return_value.content = json.dumps(self.output(data))
        with patch('src.providers.llm.build_llm', return_value=model):
            result, name = news._call_model('CSCO', data['items'])
        self.assertEqual(name, 'deepseek-flash')
        self.assertEqual(model.invoke.call_args.kwargs['timeout'], 45)
        self.assertEqual(model.invoke.call_args.kwargs['max_tokens'], 600)
        self.assertIn('仅有标题时也可以概括标题', model.invoke.call_args.args[0][1]['content'])
        self.assertIn('不可信资料', model.invoke.call_args.args[0][0]['content'])
        self.assertEqual(result['sentiment'], 'positive')

    def test_routes_authenticated_and_read_does_not_submit(self):
        import api_server as api
        routes = {r.path: r for r in api.app.routes if hasattr(r, 'path')}
        path = '/single-stock-overnight/{symbol}/news-research'
        self.assertTrue(routes[path].dependencies)
        self.assertTrue(routes[path + '/summary'].dependencies)
        with patch.object(news, 'read_news', return_value={'symbol': 'CSCO'}), patch.object(news, 'start_summary') as start:
            self.assertEqual(asyncio.run(api.single_stock_news_research('CSCO')), {'symbol': 'CSCO'})
            start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
