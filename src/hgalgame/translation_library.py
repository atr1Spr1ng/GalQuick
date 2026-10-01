"""Portable, content-addressed interval translations. No provider credentials."""
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from hgalgame.translation import ParagraphTranslator, TranslationError


class TranslationLibrary:
    FORMAT = 'hgal-translations-v1'

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def fingerprint(rows):
        rows = ParagraphTranslator.validate_rows(rows, len(rows))
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode('utf-8')).hexdigest()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS intervals (hash TEXT, target TEXT, value TEXT, PRIMARY KEY(hash,target))')
        db.execute('CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY, hash TEXT, target TEXT, time TEXT, reason TEXT, old TEXT, new TEXT)')
        return db

    def get(self, rows, target):
        if not rows or not self.path.exists():
            return None
        with closing(self._open()) as db:
            record = db.execute('SELECT value FROM intervals WHERE hash=? AND target=?',
                                (self.fingerprint(rows), target)).fetchone()
        if record:
            try:
                return ParagraphTranslator.validate_rows(json.loads(record[0]), len(rows))
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _put(db, digest, target, value, reason, overwrite):
        old = db.execute('SELECT value FROM intervals WHERE hash=? AND target=?', (digest, target)).fetchone()
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if old and (not overwrite or old[0] == encoded):
            return False
        db.execute('INSERT OR REPLACE INTO intervals VALUES (?,?,?)', (digest, target, encoded))
        db.execute('INSERT INTO revisions(hash,target,time,reason,old,new) VALUES (?,?,?,?,?,?)',
                   (digest, target, datetime.now(timezone.utc).isoformat(), reason, old[0] if old else None, encoded))
        return True

    def save(self, rows, target, value, *, reason='api', overwrite=False):
        value = ParagraphTranslator.validate_rows(value, len(rows))
        with closing(self._open()) as db, db:
            digest = self.fingerprint(rows)
            old = db.execute('SELECT value FROM intervals WHERE hash=? AND target=?', (digest, target)).fetchone()
            if old:
                try:
                    ParagraphTranslator.validate_rows(json.loads(old[0]), len(rows))
                except (ValueError, TypeError):
                    overwrite = True  # An unusable imported candidate cannot block a valid result.
            return self._put(db, digest, target, value, reason, overwrite)

    def history(self, rows, target):
        with closing(self._open()) as db:
            records = db.execute('SELECT time,reason,old,new FROM revisions WHERE hash=? AND target=? ORDER BY id DESC',
                                 (self.fingerprint(rows), target)).fetchall()
        return [dict(time=t, reason=r, before=json.loads(o) if o else None, after=json.loads(n)) for t,r,o,n in records]

    def export_pack(self, path: Path, target: str):
        if path.suffix.lower() != '.json' or path.resolve() == self.path.resolve():
            raise TranslationError('译文包必须保存为独立的 .json 文件。')
        with closing(self._open()) as db:
            records = db.execute('SELECT hash,value FROM intervals WHERE target=? ORDER BY hash', (target,)).fetchall()
        payload = dict(format=self.FORMAT, target=target, entries=[dict(source_hash=h, paragraphs=json.loads(v)) for h,v in records])
        # No original text, paths, endpoint, model, API key or local history in this format.
        import os
        import tempfile
        descriptor, name = tempfile.mkstemp(prefix='.hgal-pack-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
        return len(records)

    def import_pack(self, path: Path, target: str):
        with path.open('rb') as source:
            raw = source.read(20_000_001)
        if len(raw) > 20_000_000:
            raise TranslationError('译文包超过 20 MB，未导入。')
        try:
            pack = json.loads(raw)
            if not isinstance(pack, dict) or pack.get('format') != self.FORMAT or pack.get('target') != target:
                raise ValueError()
            entries = pack['entries']
            if not isinstance(entries, list) or len(entries) > 10000:
                raise ValueError()
            validated, seen = [], set()
            for entry in entries:
                digest, rows = entry['source_hash'], entry['paragraphs']
                if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest) or digest in seen:
                    raise ValueError()
                if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
                    raise ValueError()
                value = ParagraphTranslator.validate_rows(rows, len(rows))
                validated.append((digest, value))
                seen.add(digest)
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise TranslationError('译文包格式、语言或条目不正确；未导入任何内容。') from None
        imported = 0
        with closing(self._open()) as db, db:
            for digest, value in validated:
                imported += self._put(db, digest, target, value, 'import', False)
        return dict(imported=imported, skipped=len(validated)-imported)
