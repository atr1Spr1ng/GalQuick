"""Small, data-only IPC adapter for the AdvHD Lua 5.3 system UI."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path


LUA = r'''
local token = "@TOKEN@"
local page, generation, sequence, ready = '', 0, 0, false
local settle = 0
local function report(state)
    local f = io.open('hgal-bridge-status.txt', 'w')
    if f then
        f:write(token..'|'..generation..'|'..sequence..'|'..state..'|'..page)
        f:close()
    end
end
function HGalControl(tag)
    page = tag
    generation = generation + 1
    ready = false
end
local originalSelect = initSelect
function initSelect(count)
    local result = originalSelect(count)
    if page ~= '' then
        g_MenuMsgWin:hideSelect(1)
        ready = true
        settle = 30
        report('ready')
    end
    return result
end
local originalInit = initSystemScreen
function initSystemScreen(...)
    local result = originalInit(...)
    local menu = g_MenuMsgWin
    local update = menu.MenuEffect
    local keyDown = menu.MenuKeyDown
    menu.MenuKeyDown = function(self, ...)
        if page ~= '' then return end
        return keyDown(self, ...)
    end
    local ticks = 0
    menu.MenuEffect = function(self, ...)
        local result = update(self, ...)
        ticks = ticks + 1
        if settle > 0 then settle = settle - 1 end
        if ticks % 6 == 0 and ready and settle == 0 then
            local ok = pcall(function()
                report('ready')
                local f = io.open('hgal-bridge-request.txt', 'r')
                local request = f and f:read(256) or ''
                if f then f:close() end
                local t, seq, gen, expected, item = request:match(
                    '^([a-f0-9]+)|(%d+)|(%d+)|([%w_]+)|(%d+)$')
                seq, gen, item = tonumber(seq), tonumber(gen), tonumber(item)
                if t == token and seq and seq > sequence and gen == generation
                    and expected == page and item < self.SelectCount then
                    sequence = seq
                    ready = false
                    report('accepted')
                    page = ''
                    cfunc.LegacyGame__lua_SelectItem(item)
                    self:closeSelect(item + 1)
                end
            end)
            if not ok then ready = false; report('error') end
        end
        return result
    end
    report('attached')
    return result
end
'''


def wrap_system_ui(original: bytes, token: str) -> bytes:
    if not re.fullmatch(r'[a-f0-9]{32}', token):
        raise ValueError('invalid bridge session token')
    if not original.startswith(b'\x1bLua\x53'):
        raise ValueError('bridge requires an unmodified Lua 5.3 system UI')
    escaped = ''.join('\\%03d' % value for value in original)
    return ('assert(load("' + escaped + '", "@OriginalSystemUI", "b"))()\n'
            + LUA.replace('@TOKEN@', token)).encode('ascii')


class AdvHdBridge:
    def __init__(self, directory: Path, token: str):
        if not re.fullmatch(r'[a-f0-9]{32}', token):
            raise ValueError('invalid bridge session token')
        self.directory, self.token, self.sequence = directory, token, 0

    def status(self) -> dict | None:
        try:
            raw = (self.directory / 'hgal-bridge-status.txt').read_text('ascii')
            token, gen, seq, state, page = raw.split('|')
            if token != self.token or state not in {'attached', 'ready', 'accepted', 'error'}:
                return None
            if not gen.isdecimal() or not seq.isdecimal() or not re.fullmatch(r'\w*', page):
                return None
            return dict(generation=int(gen), sequence=int(seq), state=state, page=page)
        except (OSError, UnicodeError, ValueError):
            return None  # A concurrent Lua write may briefly be incomplete.

    def wait(self, predicate, *, timeout=10.0, alive=lambda: True) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not alive():
                raise RuntimeError('AdvHD stopped while waiting for its bridge')
            status = self.status()
            if status and status['state'] == 'error':
                raise RuntimeError('AdvHD script bridge reported an error')
            if status and predicate(status):
                return status
            time.sleep(.05)
        raise TimeoutError('AdvHD bridge response timed out; no keyboard fallback was sent')

    def select(self, page: str, item: int, *, alive=lambda: True) -> dict:
        if not re.fullmatch(r'\w+', page) or not isinstance(item, int) or item < 0:
            raise ValueError('invalid bridge choice')
        status = self.wait(lambda s: s['state'] == 'ready' and s['page'] == page, alive=alive)
        self.sequence = max(self.sequence, status['sequence']) + 1
        request = f'{self.token}|{self.sequence}|{status["generation"]}|{page}|{item}'
        path = self.directory / 'hgal-bridge-request.txt'
        temporary = path.with_suffix('.tmp')
        try:
            temporary.write_text(request, encoding='ascii')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.wait(lambda s: s['sequence'] == self.sequence, alive=alive)
