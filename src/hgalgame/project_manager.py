"""Small index-only game project manager."""
from __future__ import annotations
import hashlib, json, re
from datetime import datetime, timezone
from pathlib import Path
from hgalgame.detection import EngineDetector
from hgalgame.output import write_json

class GameProjectManager:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.index = self.root / 'projects.json'

    def _load(self):
        if not self.index.exists():
            return {'format':'hgal-project-index-v1','projects':[]}
        data = json.loads(self.index.read_text('utf-8'))
        if data.get('format') != 'hgal-project-index-v1' or not isinstance(data.get('projects'), list):
            raise ValueError('项目索引格式不正确；原索引未被覆盖。')
        for item in data['projects']:
            if not re.fullmatch(r'[a-f0-9]{16}', item.get('id', '')):
                raise ValueError('项目索引包含无效 ID。')
        return data

    @staticmethod
    def fingerprint(game: Path):
        files = []
        for p in sorted(game.iterdir(), key=lambda x:x.name.casefold()):
            if p.is_file() and p.suffix.casefold() in {'.exe','.arc','.ws2','.xp3','.pak','.rpy'}:
                st=p.stat(); files.append((p.name,st.st_size,st.st_mtime_ns))
        return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def add(self, game_dir: Path):
        game = game_dir.resolve()
        self._check_game(game)
        detection = EngineDetector().detect(game)
        old = next((p for p in self.list() if Path(p['game_dir']).resolve()==game), None)
        project_id = old['id'] if old else hashlib.sha256(str(game).casefold().encode()).hexdigest()[:16]
        item=dict(old or {})
        item.update({'id':project_id,'name':game.name,'game_dir':str(game),'engine':detection.engine,
              'confidence':detection.confidence,'evidence':detection.to_dict()['evidence'],
              'fingerprint':self.fingerprint(game),'added_at':item.get('added_at') or datetime.now(timezone.utc).isoformat()})
        item.setdefault('output_dir',str(self.root/project_id/'story_browser'))
        return self._save(item)

    def list(self): return self._load().get('projects',[])

    def get(self, project_id):
        return next(item for item in self.list() if item['id'] == project_id)

    def _save(self, item):
        projects = [p for p in self.list() if p['id'] != item['id']]
        projects.append(item)
        write_json(self.index, {'format':'hgal-project-index-v1', 'projects':sorted(projects,key=lambda p:p['name'].casefold())})
        write_json(self.root/item['id']/'source.json', item)
        return item

    def _check_game(self, game):
        if not game.is_dir():
            raise NotADirectoryError(f'原游戏目录不存在，请重新定位：{game}')
        if self.root == game or game in self.root.parents:
            raise ValueError('项目库目录必须放在原游戏目录之外。')

    def refresh(self, project_id):
        return self.add(Path(self.get(project_id)['game_dir']))

    def relocate(self, project_id, game_dir):
        game=Path(game_dir).resolve()
        self._check_game(game)
        if any(p['id'] != project_id and Path(p['game_dir']).resolve()==game for p in self.list()):
            raise ValueError('这个目录已属于另一个项目。')
        item=self.get(project_id)
        detection=EngineDetector().detect(game)
        item.update(game_dir=str(game),name=game.name,engine=detection.engine,
                    confidence=detection.confidence,evidence=detection.to_dict()['evidence'],
                    fingerprint=self.fingerprint(game),parsed_fingerprint=None)
        return self._save(item)

    def import_existing(self, story_file):
        path=Path(story_file).resolve()
        document=json.loads(path.read_text('utf-8'))
        if document.get('format_version') != 'hgal-story-flow-v2':
            raise ValueError('请选择现有项目的 story_flow.json。')
        game=Path(document['game']['game_dir']).resolve()
        self._check_game(game)
        if path.parent==game or game in path.parent.parents:
            raise ValueError('已有项目输出不能放在原游戏内部。')
        item=self.add(game)
        item.update(output_dir=str(path.parent),parsed_fingerprint=self.fingerprint(game))
        return self._save(item)

    def build(self, project_id):
        from hgalgame.engines.registry import default_engine_adapters
        from hgalgame.frontend.server import StoryBrowser
        from hgalgame.story import EngineAdapterRegistry
        item=self.get(project_id)
        game=Path(item['game_dir'])
        self._check_game(game)
        before=self.fingerprint(game)
        output=Path(item['output_dir']).resolve()
        if output==game or game in output.parents:
            raise ValueError('输出目录必须在原游戏之外。')
        document=StoryBrowser(EngineAdapterRegistry(default_engine_adapters())).build(game,output)
        if before != self.fingerprint(game):
            raise ValueError('解析期间原游戏文件发生变化，请关闭游戏后重新解析。')
        item.update(parsed_fingerprint=before,summary=document['summary'])
        self._save(item)
        return output/'story_flow.json'

    def require_ready(self, project_id):
        item=self.get(project_id)
        game=Path(item['game_dir'])
        self._check_game(game)
        if item.get('parsed_fingerprint') != self.fingerprint(game):
            raise ValueError('尚未解析，或原游戏文件已变化，请先点击「解析剧情」。')
        path=Path(item['output_dir'])/'story_flow.json'
        if not path.is_file():
            raise ValueError('剧情索引不存在，请先解析。')
        return item,path
